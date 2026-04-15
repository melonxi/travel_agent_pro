# LLM 错误分类修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让裸 `APIError`（讯飞等兼容网关常见）被准确归类为 TRANSIENT/RATE_LIMITED/BAD_REQUEST，而不是误报为 PROTOCOL_ERROR。

**Architecture:** 在 `llm/errors.py` 新增 `classify_opaque_api_error()` 纯函数，通过结构化状态码 → gateway 码 → 关键词 → 保守兜底的优先级分类；两个 provider 的 `_classify_error` fallthrough 统一调用它。

**Tech Stack:** Python 3.12, pytest, openai SDK, anthropic SDK

---

## File Structure

| 操作 | 文件 | 职责 |
|---|---|---|
| Modify | `backend/llm/errors.py` | 新增规则常量 + `classify_opaque_api_error` 函数 |
| Modify | `backend/llm/openai_provider.py:82-90` | fallthrough 改调新函数 |
| Modify | `backend/llm/anthropic_provider.py:96-104` | fallthrough 改调新函数 |
| Create | `backend/tests/test_classify_opaque_api_error.py` | 单元测试 |
| Create | `backend/tests/fixtures/opaque_api_errors/xunfei_busy.json` | 回归 fixture |
| Create | `backend/tests/fixtures/opaque_api_errors/xunfei_400_invalid_messages.json` | 回归 fixture |
| Modify | `backend/tests/test_openai_provider.py:250-254` | 更新 fallthrough 行为断言 |

---

### Task 1: 新增 `classify_opaque_api_error` 函数 + 单元测试

**Files:**
- Modify: `backend/llm/errors.py`（在文件末尾追加）
- Create: `backend/tests/test_classify_opaque_api_error.py`

- [ ] **Step 1: 创建失败的测试文件**

创建 `backend/tests/test_classify_opaque_api_error.py`：

```python
# backend/tests/test_classify_opaque_api_error.py
import pytest
from llm.errors import LLMErrorCode, classify_opaque_api_error


class _FakeAPIError(Exception):
    """模拟裸 openai.APIError，不依赖 openai SDK。"""
    def __init__(self, message: str, *, body=None, status_code=None):
        super().__init__(message)
        self.body = body
        self.status_code = status_code


# ── 结构化状态码 ──

def test_status_code_attribute_400():
    exc = _FakeAPIError("bad", status_code=400)
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


def test_status_code_attribute_429():
    exc = _FakeAPIError("rate limited", status_code=429)
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True


def test_status_code_attribute_500():
    exc = _FakeAPIError("internal", status_code=500)
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_status_code_in_body_dict():
    exc = _FakeAPIError("error", body={"status_code": 503})
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_status_code_regex_from_text():
    exc = _FakeAPIError(
        "Xunfei request failed with status code: 400, param validation error"
    )
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


def test_status_code_500_regex():
    exc = _FakeAPIError("upstream status code: 502 Bad Gateway")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


# ── Gateway 特定码 ──

def test_xunfei_gateway_code_10012():
    exc = _FakeAPIError(
        "Xunfei request failed with code: 10012, "
        "message: EngineInternalError:The system is busy"
    )
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


# ── 关键词匹配（英文） ──

def test_keyword_system_is_busy():
    exc = _FakeAPIError("EngineInternalError:The system is busy, please try again later.")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_keyword_overloaded():
    exc = _FakeAPIError("model is overloaded, try later")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_keyword_temporarily_unavailable():
    exc = _FakeAPIError("service temporarily unavailable")
    err = classify_opaque_api_error(exc, provider="anthropic", model="claude-sonnet-4-20250514")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_keyword_rate_limit():
    exc = _FakeAPIError("rate limit exceeded, please slow down")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True


def test_keyword_too_many_requests():
    exc = _FakeAPIError("too many requests")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True


def test_keyword_quota_exceeded():
    exc = _FakeAPIError("quota exceeded for this model")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True


def test_keyword_invalid_request():
    exc = _FakeAPIError("invalid request: missing required field 'model'")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


def test_keyword_param_validation():
    exc = _FakeAPIError("param validation error, Value error")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


# ── 关键词匹配（中文） ──

def test_cn_keyword_繁忙():
    exc = _FakeAPIError("服务繁忙，请稍后再试")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_cn_keyword_请稍后():
    exc = _FakeAPIError("请稍后重试")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


# ── 兜底 ──

def test_unknown_falls_back_to_transient_non_retryable():
    exc = _FakeAPIError("something completely unexpected happened")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is False


def test_empty_message_falls_back():
    exc = _FakeAPIError("")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is False


# ── 字段透传 ──

def test_failure_phase_preserved():
    exc = _FakeAPIError("unknown")
    err = classify_opaque_api_error(
        exc, provider="openai", model="gpt-4o", failure_phase="streaming"
    )
    assert err.failure_phase == "streaming"


def test_provider_and_model_set():
    exc = _FakeAPIError("unknown")
    err = classify_opaque_api_error(
        exc, provider="anthropic", model="claude-sonnet-4-20250514"
    )
    assert err.provider == "anthropic"
    assert err.model == "claude-sonnet-4-20250514"


def test_raw_error_populated():
    exc = _FakeAPIError("some error")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.raw_error == repr(exc)


def test_never_returns_protocol_error():
    for msg in ["xyz", "!!!", "", "unknown format", "???", "\x00\x01"]:
        exc = _FakeAPIError(msg)
        err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
        assert err.code != LLMErrorCode.PROTOCOL_ERROR, f"PROTOCOL_ERROR for: {msg!r}"


# ── body 提取 ──

def test_body_bytes_extracted():
    exc = _FakeAPIError("error", body=b"system is busy")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_body_dict_message_extracted():
    exc = _FakeAPIError("error", body={"message": "rate limit exceeded"})
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True
```

- [ ] **Step 2: 运行测试确认全部失败**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify/backend && python -m pytest tests/test_classify_opaque_api_error.py -v 2>&1 | head -50`
Expected: 全部 FAILED（`ImportError: cannot import name 'classify_opaque_api_error'`）

- [ ] **Step 3: 在 `errors.py` 末尾实现函数**

在 `backend/llm/errors.py` 末尾（line 92 之后）追加：

```python
import re as _re


_STATUS_CODE_RE = _re.compile(r"status code[:\s]+(\d{3})", _re.IGNORECASE)

_TRANSIENT_KEYWORDS = (
    "busy",
    "繁忙",
    "try again",
    "请稍后",
    "temporarily unavailable",
    "engine internal",
    "system is busy",
    "overloaded",
)
_RATE_LIMITED_KEYWORDS = ("rate limit", "too many requests", "quota exceeded")
_BAD_REQUEST_KEYWORDS = ("invalid request", "malformed", "param validation")

_GATEWAY_TRANSIENT_CODES = ("10012",)


def _extract_status_code(exc: Exception) -> int | None:
    sc = getattr(exc, "status_code", None)
    if isinstance(sc, int):
        return sc
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        for key in ("status_code", "statusCode"):
            val = body.get(key)
            if isinstance(val, int):
                return val
    m = _STATUS_CODE_RE.search(str(exc))
    if m:
        return int(m.group(1))
    return None


def _extract_body_text(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(body, dict):
        try:
            import json
            return json.dumps(body, ensure_ascii=False)
        except Exception:
            return str(body)
    return str(body)


def classify_opaque_api_error(
    exc: Exception,
    *,
    provider: str,
    model: str,
    failure_phase: str = "connection",
) -> LLMError:
    text = str(exc).lower()
    body_text = _extract_body_text(exc).lower()
    haystack = f"{text} {body_text}"

    status = _extract_status_code(exc)
    if status is not None:
        return classify_by_http_status(
            status,
            provider=provider,
            model=model,
            raw_error=repr(exc),
        )

    for code in _GATEWAY_TRANSIENT_CODES:
        if code in haystack:
            return LLMError(
                code=LLMErrorCode.TRANSIENT,
                message=str(exc),
                retryable=True,
                provider=provider,
                model=model,
                failure_phase=failure_phase,
                raw_error=repr(exc),
            )

    if any(kw in haystack for kw in _TRANSIENT_KEYWORDS):
        return LLMError(
            code=LLMErrorCode.TRANSIENT,
            message=str(exc),
            retryable=True,
            provider=provider,
            model=model,
            failure_phase=failure_phase,
            raw_error=repr(exc),
        )
    if any(kw in haystack for kw in _RATE_LIMITED_KEYWORDS):
        return LLMError(
            code=LLMErrorCode.RATE_LIMITED,
            message=str(exc),
            retryable=True,
            provider=provider,
            model=model,
            failure_phase=failure_phase,
            raw_error=repr(exc),
        )
    if any(kw in haystack for kw in _BAD_REQUEST_KEYWORDS):
        return LLMError(
            code=LLMErrorCode.BAD_REQUEST,
            message=str(exc),
            retryable=False,
            provider=provider,
            model=model,
            failure_phase=failure_phase,
            raw_error=repr(exc),
        )

    return LLMError(
        code=LLMErrorCode.TRANSIENT,
        message=str(exc),
        retryable=False,
        provider=provider,
        model=model,
        failure_phase=failure_phase,
        raw_error=repr(exc),
    )
```

- [ ] **Step 4: 运行测试确认全部通过**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify/backend && python -m pytest tests/test_classify_opaque_api_error.py -v`
Expected: 全部 PASSED

- [ ] **Step 5: 运行已有测试确认无回归**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify/backend && python -m pytest tests/test_llm_errors.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify
git add backend/llm/errors.py backend/tests/test_classify_opaque_api_error.py
git commit -m "feat(llm): add classify_opaque_api_error for bare APIError classification

Extracts classification signals from opaque exceptions via structured
status codes, gateway codes, and keyword matching. Falls back to
TRANSIENT+retryable=False (opencode-style conservative default)."
```

---

### Task 2: 添加真实日志回归 fixture

**Files:**
- Create: `backend/tests/fixtures/opaque_api_errors/xunfei_busy.json`
- Create: `backend/tests/fixtures/opaque_api_errors/xunfei_400_invalid_messages.json`
- Modify: `backend/tests/test_classify_opaque_api_error.py`（追加 fixture 测试）

- [ ] **Step 1: 创建 fixture 目录和文件**

创建 `backend/tests/fixtures/opaque_api_errors/xunfei_busy.json`：

```json
{
  "description": "讯飞 EngineInternalError - system is busy (2026-04-15 澳门会话)",
  "error_message": "Xunfei request failed with code: 10012, message: EngineInternalError:The system is busy, please try again later.",
  "expected_code": "LLM_TRANSIENT_ERROR",
  "expected_retryable": true
}
```

创建 `backend/tests/fixtures/opaque_api_errors/xunfei_400_invalid_messages.json`：

```json
{
  "description": "讯飞 400 参数校验 - Invalid messages (2026-04-15 香港会话)",
  "error_message": "Xunfei request failed with status code: 400, Inference failed: request param validation error, Value error, Invalid messages at index 10.",
  "expected_code": "LLM_BAD_REQUEST",
  "expected_retryable": false
}
```

- [ ] **Step 2: 在测试文件末尾追加 fixture 驱动的测试**

在 `backend/tests/test_classify_opaque_api_error.py` 末尾追加：

```python
# ── Fixture-driven regression tests ──

import json
from pathlib import Path

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "opaque_api_errors"


def _load_fixtures():
    if not _FIXTURE_DIR.is_dir():
        return []
    items = []
    for p in sorted(_FIXTURE_DIR.glob("*.json")):
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        items.append(pytest.param(data, id=p.stem))
    return items


@pytest.mark.parametrize("fixture", _load_fixtures())
def test_fixture_regression(fixture):
    exc = _FakeAPIError(fixture["error_message"])
    err = classify_opaque_api_error(exc, provider="openai", model="test-model")
    assert err.code.value == fixture["expected_code"], (
        f"Expected {fixture['expected_code']}, got {err.code.value} "
        f"for: {fixture['description']}"
    )
    assert err.retryable is fixture["expected_retryable"], (
        f"Expected retryable={fixture['expected_retryable']}, "
        f"got {err.retryable} for: {fixture['description']}"
    )
```

- [ ] **Step 3: 运行 fixture 测试确认通过**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify/backend && python -m pytest tests/test_classify_opaque_api_error.py::test_fixture_regression -v`
Expected: 2 passed (xunfei_busy, xunfei_400_invalid_messages)

- [ ] **Step 4: Commit**

```bash
cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify
git add backend/tests/fixtures/opaque_api_errors/ backend/tests/test_classify_opaque_api_error.py
git commit -m "test(llm): add real-log regression fixtures for opaque API errors

Two fixtures from production incidents: Xunfei busy (10012) and
Xunfei 400 invalid messages. New fixtures auto-discovered by
parametrized test."
```

---

### Task 3: OpenAI provider fallthrough 集成

**Files:**
- Modify: `backend/llm/openai_provider.py:82-90`
- Modify: `backend/tests/test_openai_provider.py:250-254`（更新断言 + 新增测试）

- [ ] **Step 1: 更新 `test_classify_error_unknown_exception` 断言**

`backend/tests/test_openai_provider.py:250-254` 当前内容：

```python
def test_classify_error_unknown_exception(provider):
    exc = RuntimeError("something weird")
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.PROTOCOL_ERROR
    assert result.retryable is False
```

改为：

```python
def test_classify_error_unknown_exception(provider):
    exc = RuntimeError("something weird")
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is False
```

- [ ] **Step 2: 在 `test_openai_provider.py` 末尾追加新测试**

```python
def test_classify_error_opaque_api_error_busy(provider):
    import openai
    exc = openai.APIError(
        message="Xunfei request failed with code: 10012, "
                "message: EngineInternalError:The system is busy",
        request=MagicMock(),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_api_status_error_unchanged(provider):
    """强类型分支行为回归：APIStatusError 仍走 classify_by_http_status。"""
    import openai
    exc = openai.APIStatusError(
        message="bad request",
        response=MagicMock(status_code=400),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.BAD_REQUEST
    assert result.retryable is False
```

- [ ] **Step 3: 运行测试确认 `test_classify_error_unknown_exception` 失败（断言变了但代码没改）**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify/backend && python -m pytest tests/test_openai_provider.py::test_classify_error_unknown_exception tests/test_openai_provider.py::test_classify_error_opaque_api_error_busy -v`
Expected: `test_classify_error_unknown_exception` FAILED（当前代码仍返回 PROTOCOL_ERROR），`test_classify_error_opaque_api_error_busy` FAILED

- [ ] **Step 4: 修改 `openai_provider.py` fallthrough**

`backend/llm/openai_provider.py:82-90` 当前内容：

```python
        return LLMError(
            code=LLMErrorCode.PROTOCOL_ERROR,
            message=str(exc),
            retryable=False,
            provider="openai",
            model=self.model,
            failure_phase=failure_phase,
            raw_error=repr(exc),
        )
```

两处修改：

**修改 1**：`openai_provider.py:41` 现有 import 行

```python
# 原：
from llm.errors import LLMError, LLMErrorCode, classify_by_http_status
# 改为：
from llm.errors import LLMError, LLMErrorCode, classify_by_http_status, classify_opaque_api_error
```

**修改 2**：`openai_provider.py:82-90` fallthrough 块

```python
        return classify_opaque_api_error(
            exc,
            provider="openai",
            model=self.model,
            failure_phase=failure_phase,
        )
```

- [ ] **Step 5: 运行全部 openai_provider 测试确认通过**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify/backend && python -m pytest tests/test_openai_provider.py -v`
Expected: 全部 PASSED

- [ ] **Step 6: Commit**

```bash
cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify
git add backend/llm/openai_provider.py backend/tests/test_openai_provider.py
git commit -m "fix(llm): openai provider fallthrough uses classify_opaque_api_error

Bare APIError (common from Xunfei/compatible gateways) now classified
via keyword/status extraction instead of hardcoded PROTOCOL_ERROR."
```

---

### Task 4: Anthropic provider fallthrough 集成

**Files:**
- Modify: `backend/llm/anthropic_provider.py:96-104`
- Create: `backend/tests/test_anthropic_provider_classify.py`

- [ ] **Step 1: 创建测试文件**

创建 `backend/tests/test_anthropic_provider_classify.py`：

```python
# backend/tests/test_anthropic_provider_classify.py
from unittest.mock import MagicMock

import pytest

from llm.errors import LLMError, LLMErrorCode


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from llm.anthropic_provider import AnthropicProvider
    return AnthropicProvider(model="claude-sonnet-4-20250514")


def test_classify_error_unknown_exception(provider):
    exc = RuntimeError("something weird")
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is False


def test_classify_error_opaque_api_error_busy(provider):
    import anthropic
    exc = anthropic.APIError(
        message="service is busy, please try again later",
        request=MagicMock(),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_api_status_error_unchanged(provider):
    """强类型分支行为回归。"""
    import anthropic
    exc = anthropic.APIStatusError(
        message="overloaded",
        response=MagicMock(status_code=529),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_connection_error(provider):
    import anthropic
    exc = anthropic.APIConnectionError(request=MagicMock())
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_json_decode_unchanged(provider):
    import json
    exc = json.JSONDecodeError("bad json", "", 0)
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.PROTOCOL_ERROR
    assert result.failure_phase == "parsing"


def test_provider_name(provider):
    assert provider.provider_name == "anthropic"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify/backend && python -m pytest tests/test_anthropic_provider_classify.py::test_classify_error_unknown_exception tests/test_anthropic_provider_classify.py::test_classify_error_opaque_api_error_busy -v`
Expected: `test_classify_error_unknown_exception` FAILED（当前返回 PROTOCOL_ERROR），`test_classify_error_opaque_api_error_busy` FAILED

- [ ] **Step 3: 修改 `anthropic_provider.py` fallthrough**

`backend/llm/anthropic_provider.py:96-104` 当前内容：

```python
        return LLMError(
            code=LLMErrorCode.PROTOCOL_ERROR,
            message=str(exc),
            retryable=False,
            provider="anthropic",
            model=self.model,
            failure_phase=failure_phase,
            raw_error=repr(exc),
        )
```

两处修改：

**修改 1**：`anthropic_provider.py:45` 现有 import 行

```python
# 原：
from llm.errors import LLMError, LLMErrorCode, classify_by_http_status
# 改为：
from llm.errors import LLMError, LLMErrorCode, classify_by_http_status, classify_opaque_api_error
```

**修改 2**：`anthropic_provider.py:96-104` fallthrough 块

```python
        return classify_opaque_api_error(
            exc,
            provider="anthropic",
            model=self.model,
            failure_phase=failure_phase,
        )
```

- [ ] **Step 4: 运行全部 anthropic 测试确认通过**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify/backend && python -m pytest tests/test_anthropic_provider_classify.py -v`
Expected: 全部 PASSED

- [ ] **Step 5: Commit**

```bash
cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify
git add backend/llm/anthropic_provider.py backend/tests/test_anthropic_provider_classify.py
git commit -m "fix(llm): anthropic provider fallthrough uses classify_opaque_api_error

Symmetric fix for Anthropic provider. Bare exceptions now classified
via the same keyword/status extraction utility."
```

---

### Task 5: 全量回归 + 文档更新

**Files:**
- Modify: `PROJECT_OVERVIEW.md`（更新 llm/errors.py 描述）
- Modify: `docs/TODO.md`（标记第 2 条为已完成）

- [ ] **Step 1: 运行全部 llm 相关测试确认无回归**

Run: `cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify/backend && python -m pytest tests/test_llm_errors.py tests/test_openai_provider.py tests/test_classify_opaque_api_error.py tests/test_anthropic_provider_classify.py -v`
Expected: 全部 PASSED

- [ ] **Step 2: 更新 `docs/TODO.md` 第 2 条**

在 `docs/TODO.md` 的 `## 2. openai_provider 错误分类` 标题前加 `[DONE] ` 前缀，在该节末尾添加完成记录：

```markdown
## 2. [DONE] openai_provider 错误分类：从 APIError 中恢复真实 HTTP 状态码
```

在该节 `### 目标` 后追加：

```markdown
### 完成记录

- 完成日期：2026-04-15
- 分支：`fix/llm-error-classify`
- 改动：`llm/errors.py` 新增 `classify_opaque_api_error()`，两个 provider fallthrough 改调该函数
- 测试：`test_classify_opaque_api_error.py`（28+ 用例）、`test_anthropic_provider_classify.py`（6 用例）、`test_openai_provider.py` 已有用例更新
```

- [ ] **Step 3: 更新 `PROJECT_OVERVIEW.md` 中 llm/errors.py 描述**

找到 `PROJECT_OVERVIEW.md` 中描述 `llm/errors.py` 的段落，追加一句关于 `classify_opaque_api_error` 的说明：

```
`classify_opaque_api_error()` — 从裸 APIError 的文本/body 中抽取分类信号（结构化状态码 → gateway 码 → 关键词 → 保守兜底），两个 provider 的 fallthrough 统一走此函数。
```

- [ ] **Step 4: Commit**

```bash
cd /Users/zhaoxiwei/独立开发者的自我修养/travel_agent_pro/.worktrees/llm-error-classify
git add docs/TODO.md PROJECT_OVERVIEW.md
git commit -m "docs: mark TODO#2 done, update PROJECT_OVERVIEW for classify_opaque_api_error"
```
