# LLM 韧性体系实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 LLM 调用引入错误归一化、停止生成、安全继续三层韧性能力，按三个 PR 递进交付。

**Architecture:** Provider 层把 SDK 异常转为统一的 `LLMError`，AgentLoop 追踪 `IterationProgress` 并响应取消信号，SSE endpoint 把结构化错误传递给前端。前端通过 AbortController + cancel API 实现停止，通过 continue API 实现安全续写。

**Tech Stack:** Python 3.11+ / FastAPI / aiosqlite / React / TypeScript

**Spec:** `docs/superpowers/specs/2026-04-13-llm-resilience-design.md`

## Live bug notes

- 2026-04-13: 历史 `backend/data/sessions.db` 可能仍是旧 `sessions` schema，缺少 `last_run_id` / `last_run_status` / `last_run_error` 列。旧会话在 SSE 结束阶段执行 `session_store.update(...)` 时会触发 `sqlite3.OperationalError: no such column: last_run_id`。修复方式是在 `Database.initialize()` 启动时补齐缺失列，保证旧库自动迁移。

---

## PR1：LLM 错误归一化

### Task 1: LLMError 异常体系

**Files:**
- Create: `backend/llm/errors.py`
- Test: `backend/tests/test_llm_errors.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_llm_errors.py
import pytest
from llm.errors import LLMError, LLMErrorCode, classify_by_http_status


def test_llm_error_code_values():
    assert LLMErrorCode.TRANSIENT.value == "LLM_TRANSIENT_ERROR"
    assert LLMErrorCode.RATE_LIMITED.value == "LLM_RATE_LIMITED"
    assert LLMErrorCode.BAD_REQUEST.value == "LLM_BAD_REQUEST"
    assert LLMErrorCode.STREAM_INTERRUPTED.value == "LLM_STREAM_INTERRUPTED"
    assert LLMErrorCode.PROTOCOL_ERROR.value == "LLM_PROTOCOL_ERROR"


def test_llm_error_attributes():
    err = LLMError(
        code=LLMErrorCode.TRANSIENT,
        message="service unavailable",
        retryable=True,
        provider="openai",
        model="gpt-4o",
        failure_phase="connection",
        http_status=503,
        raw_error="503 Service Unavailable",
    )
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True
    assert err.provider == "openai"
    assert err.model == "gpt-4o"
    assert err.failure_phase == "connection"
    assert err.http_status == 503
    assert err.partial_output is False
    assert str(err) == "service unavailable"


def test_llm_error_defaults():
    err = LLMError(
        code=LLMErrorCode.PROTOCOL_ERROR,
        message="unknown",
        retryable=False,
        provider="anthropic",
        model="claude-sonnet-4-20250514",
    )
    assert err.failure_phase is None
    assert err.partial_output is False
    assert err.http_status is None
    assert err.retry_after is None
    assert err.raw_error == ""


def test_classify_by_http_status_429():
    err = classify_by_http_status(429, provider="openai", model="gpt-4o", raw_error="rate limited")
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True


def test_classify_by_http_status_503():
    err = classify_by_http_status(503, provider="openai", model="gpt-4o", raw_error="unavailable")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_classify_by_http_status_500():
    err = classify_by_http_status(500, provider="openai", model="gpt-4o", raw_error="internal error")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_classify_by_http_status_400():
    err = classify_by_http_status(400, provider="openai", model="gpt-4o", raw_error="bad request")
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


def test_classify_by_http_status_422():
    err = classify_by_http_status(422, provider="openai", model="gpt-4o", raw_error="unprocessable")
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


def test_classify_by_http_status_unknown():
    err = classify_by_http_status(418, provider="openai", model="gpt-4o", raw_error="teapot")
    assert err.code == LLMErrorCode.PROTOCOL_ERROR
    assert err.retryable is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_llm_errors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'llm.errors'`

- [ ] **Step 3: 实现 LLMError 和 classify_by_http_status**

```python
# backend/llm/errors.py
from __future__ import annotations

from enum import Enum


class LLMErrorCode(str, Enum):
    TRANSIENT = "LLM_TRANSIENT_ERROR"
    RATE_LIMITED = "LLM_RATE_LIMITED"
    BAD_REQUEST = "LLM_BAD_REQUEST"
    STREAM_INTERRUPTED = "LLM_STREAM_INTERRUPTED"
    PROTOCOL_ERROR = "LLM_PROTOCOL_ERROR"


class LLMError(Exception):
    def __init__(
        self,
        code: LLMErrorCode,
        message: str,
        *,
        retryable: bool,
        provider: str,
        model: str,
        failure_phase: str | None = None,
        partial_output: bool = False,
        http_status: int | None = None,
        retry_after: float | None = None,
        raw_error: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.provider = provider
        self.model = model
        self.failure_phase = failure_phase
        self.partial_output = partial_output
        self.http_status = http_status
        self.retry_after = retry_after
        self.raw_error = raw_error


def classify_by_http_status(
    status_code: int,
    *,
    provider: str,
    model: str,
    raw_error: str = "",
) -> LLMError:
    if status_code == 429:
        return LLMError(
            code=LLMErrorCode.RATE_LIMITED,
            message="Rate limited by LLM provider",
            retryable=True,
            provider=provider,
            model=model,
            failure_phase="connection",
            http_status=status_code,
            raw_error=raw_error,
        )
    if 500 <= status_code < 600:
        return LLMError(
            code=LLMErrorCode.TRANSIENT,
            message="LLM provider returned server error",
            retryable=True,
            provider=provider,
            model=model,
            failure_phase="connection",
            http_status=status_code,
            raw_error=raw_error,
        )
    if status_code in (400, 422):
        return LLMError(
            code=LLMErrorCode.BAD_REQUEST,
            message="LLM provider rejected request",
            retryable=False,
            provider=provider,
            model=model,
            failure_phase="connection",
            http_status=status_code,
            raw_error=raw_error,
        )
    return LLMError(
        code=LLMErrorCode.PROTOCOL_ERROR,
        message=f"Unexpected HTTP {status_code} from LLM provider",
        retryable=False,
        provider=provider,
        model=model,
        failure_phase="connection",
        http_status=status_code,
        raw_error=raw_error,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_llm_errors.py -v`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add backend/llm/errors.py backend/tests/test_llm_errors.py
git commit -m "feat(llm): add LLMError exception hierarchy and classify_by_http_status"
```

---

### Task 2: AnthropicProvider 错误归一化 + 连接重试

**Files:**
- Modify: `backend/llm/anthropic_provider.py:23-33` (新增 provider_name) 和 `:260-340` (chat 方法)
- Test: `backend/tests/test_anthropic_provider.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_anthropic_provider.py` 末尾追加：

```python
from llm.errors import LLMError, LLMErrorCode


def test_classify_error_api_status_503(provider):
    import anthropic
    exc = anthropic.APIStatusError(
        message="overloaded",
        response=MagicMock(status_code=503),
        body=None,
    )
    result = provider._classify_error(exc)
    assert isinstance(result, LLMError)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True
    assert result.failure_phase == "connection"


def test_classify_error_api_status_429(provider):
    import anthropic
    exc = anthropic.APIStatusError(
        message="rate limited",
        response=MagicMock(status_code=429),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.RATE_LIMITED
    assert result.retryable is True


def test_classify_error_api_status_400(provider):
    import anthropic
    exc = anthropic.APIStatusError(
        message="bad request",
        response=MagicMock(status_code=400),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.BAD_REQUEST
    assert result.retryable is False


def test_classify_error_connection_error(provider):
    import anthropic
    exc = anthropic.APIConnectionError(request=MagicMock())
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True
    assert result.failure_phase == "connection"


def test_classify_error_unknown_exception(provider):
    exc = RuntimeError("something weird")
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.PROTOCOL_ERROR
    assert result.retryable is False


def test_provider_name(provider):
    assert provider.provider_name == "anthropic"


@pytest.mark.asyncio
async def test_chat_raises_llm_error_on_api_failure():
    import anthropic

    with patch("llm.anthropic_provider.AsyncAnthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.create = AsyncMock(
            side_effect=anthropic.APIStatusError(
                message="overloaded",
                response=MagicMock(status_code=503),
                body=None,
            )
        )
        test_provider = AnthropicProvider(model="claude-sonnet-4-20250514")
        with pytest.raises(LLMError) as exc_info:
            async for _ in test_provider.chat(
                [Message(role=Role.USER, content="hi")],
                tools=[{"name": "t", "description": "d", "parameters": {}}],
            ):
                pass
        assert exc_info.value.code == LLMErrorCode.TRANSIENT
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_anthropic_provider.py::test_classify_error_api_status_503 tests/test_anthropic_provider.py::test_provider_name -v`
Expected: FAIL — `AttributeError: 'AnthropicProvider' object has no attribute '_classify_error'`

- [ ] **Step 3: 实现 _classify_error + provider_name + 连接重试**

在 `backend/llm/anthropic_provider.py` 中:

1. 在 class 定义后新增 `provider_name` 属性：
```python
@property
def provider_name(self) -> str:
    return "anthropic"
```

2. 新增 `_classify_error` 方法：
```python
def _classify_error(self, exc: Exception, *, failure_phase: str = "connection") -> LLMError:
    import anthropic
    from llm.errors import LLMError, LLMErrorCode, classify_by_http_status

    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMError(
            code=LLMErrorCode.TRANSIENT,
            message=str(exc),
            retryable=True,
            provider="anthropic",
            model=self.model,
            failure_phase="connection",
            raw_error=repr(exc),
        )
    if isinstance(exc, anthropic.APIStatusError):
        err = classify_by_http_status(
            exc.status_code,
            provider="anthropic",
            model=self.model,
            raw_error=str(exc),
        )
        if exc.status_code == 429:
            retry_after_header = getattr(exc.response, "headers", {}).get("retry-after")
            if retry_after_header:
                try:
                    err.retry_after = float(retry_after_header)
                except (ValueError, TypeError):
                    pass
        return err
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return LLMError(
            code=LLMErrorCode.TRANSIENT,
            message=str(exc),
            retryable=True,
            provider="anthropic",
            model=self.model,
            failure_phase="connection",
            raw_error=repr(exc),
        )
    if isinstance(exc, json.JSONDecodeError):
        return LLMError(
            code=LLMErrorCode.PROTOCOL_ERROR,
            message="Failed to parse LLM response JSON",
            retryable=False,
            provider="anthropic",
            model=self.model,
            failure_phase="parsing",
            raw_error=repr(exc),
        )
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

3. 修改 `chat()` 方法中的 `except` 块（行 328-340），添加连接重试逻辑：

将现有 try/except 改为：
```python
import asyncio as _asyncio
from llm.errors import LLMError

max_conn_retries = 2
for _attempt in range(max_conn_retries + 1):
    try:
        if not stream or tools:
            response = await self.client.messages.create(**kwargs)
            async for chunk in self._emit_nonstream_response(response, span=span):
                yield chunk
            return

        async with self.client.messages.stream(**kwargs) as stream_resp:
            # ... 现有流式处理逻辑不变 ...
            pass
        return  # 流式正常结束
    except LLMError:
        raise  # 已经归一化的异常直接抛出
    except Exception as exc:
        self._write_debug_log("error", {
            "stream": stream,
            "used_nonstream_fallback": (not stream) or bool(tools),
            "message_count": len(converted),
            "tool_count": len(kwargs.get("tools", [])),
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        llm_err = self._classify_error(exc)
        if llm_err.failure_phase == "connection" and llm_err.retryable and _attempt < max_conn_retries:
            delay = [1.0, 3.0][_attempt]
            await _asyncio.sleep(delay)
            continue
        raise llm_err
```

- [ ] **Step 4: 运行全部 anthropic provider 测试确认通过**

Run: `cd backend && python -m pytest tests/test_anthropic_provider.py -v`
Expected: all passed

- [ ] **Step 5: 提交**

```bash
git add backend/llm/anthropic_provider.py backend/tests/test_anthropic_provider.py
git commit -m "feat(llm): add error classification and connection retry to AnthropicProvider"
```

---

### Task 3: OpenAIProvider 错误归一化 + 连接重试

**Files:**
- Modify: `backend/llm/openai_provider.py:16-23` (新增 provider_name) 和 `:84-232` (chat 方法)
- Test: `backend/tests/test_openai_provider.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_openai_provider.py` 末尾追加：

```python
from llm.errors import LLMError, LLMErrorCode


def test_classify_error_api_status_503(provider):
    import openai
    exc = openai.APIStatusError(
        message="overloaded",
        response=MagicMock(status_code=503),
        body=None,
    )
    result = provider._classify_error(exc)
    assert isinstance(result, LLMError)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_api_status_429(provider):
    import openai
    exc = openai.APIStatusError(
        message="rate limited",
        response=MagicMock(status_code=429),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.RATE_LIMITED


def test_classify_error_connection_error(provider):
    import openai
    exc = openai.APIConnectionError(request=MagicMock())
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_unknown_exception(provider):
    exc = RuntimeError("something weird")
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.PROTOCOL_ERROR
    assert result.retryable is False


def test_provider_name(provider):
    assert provider.provider_name == "openai"


@pytest.mark.asyncio
async def test_chat_raises_llm_error_on_api_failure(provider):
    import openai

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(
            side_effect=openai.APIStatusError(
                message="overloaded",
                response=MagicMock(status_code=503),
                body=None,
            )
        )
        provider.client = instance
        with pytest.raises(LLMError) as exc_info:
            async for _ in provider.chat(
                [Message(role=Role.USER, content="hi")],
                stream=False,
            ):
                pass
        assert exc_info.value.code == LLMErrorCode.TRANSIENT
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_openai_provider.py::test_classify_error_api_status_503 tests/test_openai_provider.py::test_provider_name -v`
Expected: FAIL

- [ ] **Step 3: 实现 _classify_error + provider_name + 连接重试**

在 `backend/llm/openai_provider.py` 中：

1. 在 class 定义后新增 `provider_name` 属性：
```python
@property
def provider_name(self) -> str:
    return "openai"
```

2. 新增 `_classify_error` 方法（结构与 Anthropic 版相同，SDK 类型换成 openai 的）：
```python
def _classify_error(self, exc: Exception, *, failure_phase: str = "connection") -> LLMError:
    import openai
    from llm.errors import LLMError, LLMErrorCode, classify_by_http_status

    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, openai.APIConnectionError):
        return LLMError(
            code=LLMErrorCode.TRANSIENT,
            message=str(exc),
            retryable=True,
            provider="openai",
            model=self.model,
            failure_phase="connection",
            raw_error=repr(exc),
        )
    if isinstance(exc, openai.APIStatusError):
        return classify_by_http_status(
            exc.status_code,
            provider="openai",
            model=self.model,
            raw_error=str(exc),
        )
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return LLMError(
            code=LLMErrorCode.TRANSIENT,
            message=str(exc),
            retryable=True,
            provider="openai",
            model=self.model,
            failure_phase="connection",
            raw_error=repr(exc),
        )
    if isinstance(exc, json.JSONDecodeError):
        return LLMError(
            code=LLMErrorCode.PROTOCOL_ERROR,
            message="Failed to parse LLM response JSON",
            retryable=False,
            provider="openai",
            model=self.model,
            failure_phase="parsing",
            raw_error=repr(exc),
        )
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

3. 修改 `chat()` 方法，包裹连接重试循环（结构与 Task 2 中 Anthropic 版相同），在非流式（行 115-149）和流式（行 151-232）代码外加重试 + `except Exception → _classify_error → raise`。

- [ ] **Step 4: 运行全部 openai provider 测试确认通过**

Run: `cd backend && python -m pytest tests/test_openai_provider.py -v`
Expected: all passed

- [ ] **Step 5: 提交**

```bash
git add backend/llm/openai_provider.py backend/tests/test_openai_provider.py
git commit -m "feat(llm): add error classification and connection retry to OpenAIProvider"
```

---

### Task 4: SSE error 事件增强 + 前端类型扩展

**Files:**
- Modify: `backend/main.py:1638-1648`
- Modify: `frontend/src/types/plan.ts:147-158`

- [ ] **Step 1: 在 main.py 中新增 _user_friendly_message 函数**

在 `backend/main.py` 中（`event_stream()` 函数之前的模块级别）添加：

```python
from llm.errors import LLMError, LLMErrorCode

_LLM_ERROR_MESSAGES: dict[LLMErrorCode, str] = {
    LLMErrorCode.TRANSIENT: "模型服务暂时繁忙，本轮回复已中断。请稍后重试。",
    LLMErrorCode.RATE_LIMITED: "请求过于频繁，请稍后再试。",
    LLMErrorCode.BAD_REQUEST: "请求参数异常，请缩短对话长度后重试。",
    LLMErrorCode.STREAM_INTERRUPTED: "模型回复过程中连接中断。请重试。",
    LLMErrorCode.PROTOCOL_ERROR: "模型返回格式异常，请重试或切换模型。",
}

def _user_friendly_message(exc: LLMError) -> str:
    return _LLM_ERROR_MESSAGES.get(exc.code, "系统内部错误，请稍后重试。")
```

- [ ] **Step 2: 修改 event_stream 的 except 块**

将 `backend/main.py:1638-1648` 从：
```python
except Exception as exc:
    logger.exception("Agent stream failed for session %s", plan.session_id)
    yield json.dumps(
        {
            "type": "error",
            "error_code": "AGENT_STREAM_ERROR",
            "error": str(exc),
            "message": "模型服务暂时繁忙，本轮回复已中断。请稍后重试。",
        },
        ensure_ascii=False,
    )
```

改为：
```python
except LLMError as exc:
    logger.exception("LLM error for session %s: %s", plan.session_id, exc.code.value)
    yield json.dumps(
        {
            "type": "error",
            "error_code": exc.code.value,
            "retryable": exc.retryable,
            "can_continue": False,
            "provider": exc.provider,
            "model": exc.model,
            "failure_phase": exc.failure_phase,
            "message": _user_friendly_message(exc),
            "error": exc.raw_error,
        },
        ensure_ascii=False,
    )
except Exception as exc:
    logger.exception("Agent stream failed for session %s", plan.session_id)
    yield json.dumps(
        {
            "type": "error",
            "error_code": "AGENT_STREAM_ERROR",
            "retryable": False,
            "can_continue": False,
            "message": "系统内部错误，请稍后重试。",
            "error": str(exc),
        },
        ensure_ascii=False,
    )
```

- [ ] **Step 3: 扩展前端 SSEEvent 类型**

将 `frontend/src/types/plan.ts:147-158` 从：
```typescript
export interface SSEEvent {
  type: 'text_delta' | 'tool_call' | 'tool_result' | 'state_update' | 'context_compression' | 'memory_recall' | 'error' | 'done'
  content?: string
  tool_call?: ToolCallEvent
  tool_result?: ToolResultEvent
  plan?: TravelPlanState
  compression_info?: CompressionInfo
  item_ids?: string[]
  error?: string
  error_code?: string
  message?: string
}
```

改为：
```typescript
export interface SSEEvent {
  type: 'text_delta' | 'tool_call' | 'tool_result' | 'state_update' | 'context_compression' | 'memory_recall' | 'error' | 'done'
  content?: string
  tool_call?: ToolCallEvent
  tool_result?: ToolResultEvent
  plan?: TravelPlanState
  compression_info?: CompressionInfo
  item_ids?: string[]
  error?: string
  error_code?: string
  message?: string
  retryable?: boolean
  can_continue?: boolean
  failure_phase?: string
  run_id?: string
  run_status?: string
}
```

- [ ] **Step 4: 提交**

```bash
git add backend/main.py frontend/src/types/plan.ts
git commit -m "feat(sse): enhance error events with LLMError classification fields"
```

---

## PR2：停止生成 + RunRecord + KEEPALIVE

### Task 5: RunRecord + IterationProgress 数据结构

**Files:**
- Create: `backend/run.py`
- Test: `backend/tests/test_run.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_run.py
import time
from run import RunRecord, IterationProgress


def test_run_record_defaults():
    r = RunRecord(run_id="r1", session_id="s1", status="running")
    assert r.error_code is None
    assert r.finished_at is None
    assert r.started_at <= time.time()


def test_run_record_status_values():
    for status in ("running", "completed", "failed", "cancelled"):
        r = RunRecord(run_id="r1", session_id="s1", status=status)
        assert r.status == status


def test_iteration_progress_values():
    assert IterationProgress.NO_OUTPUT == "no_output"
    assert IterationProgress.PARTIAL_TEXT == "partial_text"
    assert IterationProgress.PARTIAL_TOOL_CALL == "partial_tool_call"
    assert IterationProgress.TOOLS_READ_ONLY == "tools_read_only"
    assert IterationProgress.TOOLS_WITH_WRITES == "tools_with_writes"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_run.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'run'`

- [ ] **Step 3: 实现 RunRecord 和 IterationProgress**

```python
# backend/run.py
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class IterationProgress(str, Enum):
    NO_OUTPUT = "no_output"
    PARTIAL_TEXT = "partial_text"
    PARTIAL_TOOL_CALL = "partial_tool_call"
    TOOLS_READ_ONLY = "tools_read_only"
    TOOLS_WITH_WRITES = "tools_with_writes"


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    error_code: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_run.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add backend/run.py backend/tests/test_run.py
git commit -m "feat: add RunRecord and IterationProgress data structures"
```

---

### Task 6: SessionStore 扩展 run 字段

**Files:**
- Modify: `backend/storage/database.py:8-51` (schema)
- Modify: `backend/storage/session_store.py:41-70` (update 方法)
- Test: `backend/tests/test_storage_session.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_storage_session.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_update_run_fields(store: SessionStore):
    await store.create("sess_run_test12345", "user1", "测试会话")
    await store.update(
        "sess_run_test12345",
        last_run_id="run_abc",
        last_run_status="completed",
    )
    meta = await store.load("sess_run_test12345")
    assert meta is not None
    assert meta["last_run_id"] == "run_abc"
    assert meta["last_run_status"] == "completed"
    assert meta["last_run_error"] is None


@pytest.mark.asyncio
async def test_update_run_error(store: SessionStore):
    await store.create("sess_run_err12345", "user1", "错误会话")
    await store.update(
        "sess_run_err12345",
        last_run_id="run_def",
        last_run_status="failed",
        last_run_error="LLM_TRANSIENT_ERROR",
    )
    meta = await store.load("sess_run_err12345")
    assert meta is not None
    assert meta["last_run_status"] == "failed"
    assert meta["last_run_error"] == "LLM_TRANSIENT_ERROR"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_storage_session.py::test_update_run_fields -v`
Expected: FAIL — `update()` 不接受 `last_run_id` 参数

- [ ] **Step 3: 修改 schema 和 SessionStore.update()**

在 `backend/storage/database.py` 的 `_SCHEMA` 中，`sessions` 表定义后追加三列：

```sql
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL DEFAULT 'default_user',
    title        TEXT,
    phase        INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    last_run_id     TEXT,
    last_run_status TEXT,
    last_run_error  TEXT
);
```

在 `backend/storage/session_store.py` 的 `update()` 方法签名和方法体中新增三个可选参数：

```python
async def update(
    self,
    session_id: str,
    *,
    phase: int | None = None,
    title: str | None = None,
    status: str | None = None,
    last_run_id: str | None = None,
    last_run_status: str | None = None,
    last_run_error: str | None = None,
) -> None:
    updates: list[str] = []
    params: list[Any] = []

    if phase is not None:
        updates.append("phase = ?")
        params.append(phase)
    if title is not None:
        updates.append("title = ?")
        params.append(title)
    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if last_run_id is not None:
        updates.append("last_run_id = ?")
        params.append(last_run_id)
    if last_run_status is not None:
        updates.append("last_run_status = ?")
        params.append(last_run_status)
    if last_run_error is not None:
        updates.append("last_run_error = ?")
        params.append(last_run_error)
    if not updates:
        return

    updates.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(session_id)
    await self._db.execute(
        f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?",
        tuple(params),
    )
```

- [ ] **Step 4: 运行全部 session store 测试确认通过**

Run: `cd backend && python -m pytest tests/test_storage_session.py -v`
Expected: all passed

- [ ] **Step 5: 提交**

```bash
git add backend/storage/database.py backend/storage/session_store.py backend/tests/test_storage_session.py
git commit -m "feat(storage): extend sessions table with last_run_id/status/error columns"
```

---

### Task 7: AgentLoop 取消检查 + IterationProgress 追踪

**Files:**
- Modify: `backend/agent/loop.py:17-56` (构造函数) 和 `:58-398` (run 方法)
- Test: `backend/tests/test_agent_loop.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_agent_loop.py` 中追加：

```python
import asyncio
from llm.errors import LLMError, LLMErrorCode
from run import IterationProgress


@pytest.mark.asyncio
async def test_cancel_event_stops_before_llm_call():
    cancel_event = asyncio.Event()
    cancel_event.set()  # 已经取消

    mock_llm = MagicMock()
    mock_llm.provider_name = "openai"
    mock_llm.model = "gpt-4o"
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(
        llm=mock_llm,
        tool_engine=engine,
        hooks=hooks,
        cancel_event=cancel_event,
    )
    messages = [Message(role=Role.USER, content="hi")]
    with pytest.raises(LLMError) as exc_info:
        async for _ in loop.run(messages, phase=1):
            pass
    assert exc_info.value.failure_phase == "cancelled"


@pytest.mark.asyncio
async def test_cancel_event_stops_during_streaming():
    cancel_event = asyncio.Event()

    async def fake_chat(messages, **kwargs):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hello")
        cancel_event.set()  # 模拟第一个 chunk 后取消
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content=" world")

    mock_llm = MagicMock()
    mock_llm.provider_name = "openai"
    mock_llm.model = "gpt-4o"
    mock_llm.chat = fake_chat
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(
        llm=mock_llm,
        tool_engine=engine,
        hooks=hooks,
        cancel_event=cancel_event,
    )
    messages = [Message(role=Role.USER, content="hi")]
    chunks = []
    with pytest.raises(LLMError) as exc_info:
        async for chunk in loop.run(messages, phase=1):
            chunks.append(chunk)
    # 第一个 chunk 应该已经 yield 出来了
    assert len(chunks) == 1
    assert chunks[0].content == "hello"
    assert exc_info.value.failure_phase == "cancelled"


@pytest.mark.asyncio
async def test_progress_tracks_partial_text():
    async def fake_chat(messages, **kwargs):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hello")
        yield LLMChunk(type=ChunkType.DONE)

    mock_llm = MagicMock()
    mock_llm.provider_name = "openai"
    mock_llm.model = "gpt-4o"
    mock_llm.chat = fake_chat
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(llm=mock_llm, tool_engine=engine, hooks=hooks)
    messages = [Message(role=Role.USER, content="hi")]
    async for _ in loop.run(messages, phase=1):
        pass
    assert loop.progress == IterationProgress.PARTIAL_TEXT
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_agent_loop.py::test_cancel_event_stops_before_llm_call -v`
Expected: FAIL — `AgentLoop.__init__() got an unexpected keyword argument 'cancel_event'`

- [ ] **Step 3: 实现 cancel_event + _check_cancelled + progress 追踪**

在 `backend/agent/loop.py` 中：

1. 构造函数新增参数和属性：
```python
def __init__(self, ..., cancel_event: "asyncio.Event | None" = None):
    # ... 现有属性
    self.cancel_event = cancel_event
    self._progress: IterationProgress = IterationProgress.NO_OUTPUT
```

2. 新增 `progress` 属性和 `_check_cancelled` 方法：
```python
@property
def progress(self) -> IterationProgress:
    return self._progress

def _check_cancelled(self) -> None:
    if self.cancel_event and self.cancel_event.is_set():
        from llm.errors import LLMError, LLMErrorCode
        raise LLMError(
            code=LLMErrorCode.TRANSIENT,
            message="用户取消了本轮生成",
            retryable=False,
            provider=getattr(self.llm, "provider_name", "unknown"),
            model=getattr(self.llm, "model", "unknown"),
            failure_phase="cancelled",
        )
```

3. 在 `run()` 方法中插入检查点和进度追踪：

   - 迭代入口（行 75 后）：`self._check_cancelled()` + `self._progress = IterationProgress.NO_OUTPUT`
   - `async for chunk in self.llm.chat(...)` 循环内，每个 chunk yield 前：`self._check_cancelled()`
   - TEXT_DELTA 时更新进度：`self._progress = IterationProgress.PARTIAL_TEXT`（仅在 `self._progress == IterationProgress.NO_OUTPUT` 时）
   - TOOL_CALL_START 时：`self._progress = IterationProgress.PARTIAL_TOOL_CALL`
   - 工具执行前（行 256 `tool_engine.execute` 前）：`self._check_cancelled()`
   - 工具执行后，根据 `_is_parallel_read_call` 更新进度：
     ```python
     if self._is_parallel_read_call(tc):
         if self._progress != IterationProgress.TOOLS_WITH_WRITES:
             self._progress = IterationProgress.TOOLS_READ_ONLY
     else:
         self._progress = IterationProgress.TOOLS_WITH_WRITES
     ```

需要顶部导入：
```python
from run import IterationProgress
```

- [ ] **Step 4: 运行全部 agent loop 测试确认通过**

Run: `cd backend && python -m pytest tests/test_agent_loop.py -v`
Expected: all passed

- [ ] **Step 5: 提交**

```bash
git add backend/agent/loop.py backend/tests/test_agent_loop.py
git commit -m "feat(agent): add cancel_event checking and IterationProgress tracking to AgentLoop"
```

---

### Task 8: Cancel API + RunRecord 生命周期 + KEEPALIVE

**Files:**
- Modify: `backend/main.py` — cancel endpoint, RunRecord 管理, keepalive task, done 事件增强

- [ ] **Step 1: 新增 cancel endpoint**

在 `backend/main.py` 的路由注册区域（在 `chat_endpoint` 之后）新增：

```python
@app.post("/api/chat/{session_id}/cancel")
async def cancel_chat(session_id: str):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    cancel_event = session.get("_cancel_event")
    if cancel_event:
        cancel_event.set()
    return {"status": "cancelled"}
```

- [ ] **Step 2: 在 chat endpoint 入口创建 RunRecord 和 cancel_event**

在 `event_stream()` 的开头（`phase_before_run = plan.phase` 附近）：

```python
import uuid
from run import RunRecord

run = RunRecord(run_id=str(uuid.uuid4()), session_id=plan.session_id, status="running")
session["_current_run"] = run
cancel_event = asyncio.Event()
session["_cancel_event"] = cancel_event
# 确保 AgentLoop 能访问 cancel_event
agent.cancel_event = cancel_event
```

- [ ] **Step 3: 修改 except 块处理 cancelled vs failed**

```python
except LLMError as exc:
    if exc.failure_phase == "cancelled":
        run.status = "cancelled"
        run.finished_at = time.time()
        yield json.dumps(
            {"type": "done", "run_id": run.run_id, "run_status": "cancelled"},
            ensure_ascii=False,
        )
    else:
        run.status = "failed"
        run.error_code = exc.code.value
        run.finished_at = time.time()
        logger.exception("LLM error for session %s: %s", plan.session_id, exc.code.value)
        yield json.dumps(
            {
                "type": "error",
                "error_code": exc.code.value,
                "retryable": exc.retryable,
                "can_continue": False,
                "provider": exc.provider,
                "model": exc.model,
                "failure_phase": exc.failure_phase,
                "message": _user_friendly_message(exc),
                "error": exc.raw_error,
            },
            ensure_ascii=False,
        )
except Exception as exc:
    run.status = "failed"
    run.error_code = "AGENT_STREAM_ERROR"
    run.finished_at = time.time()
    logger.exception("Agent stream failed for session %s", plan.session_id)
    yield json.dumps(
        {
            "type": "error",
            "error_code": "AGENT_STREAM_ERROR",
            "retryable": False,
            "can_continue": False,
            "message": "系统内部错误，请稍后重试。",
            "error": str(exc),
        },
        ensure_ascii=False,
    )
```

- [ ] **Step 4: 正常结束时更新 run 状态 + done 事件带 run_id**

在 `event_stream()` 的正常结束路径（`except` 块之后、state 持久化之前），如果 `run.status == "running"`：

```python
if run.status == "running":
    run.status = "completed"
    run.finished_at = time.time()
```

在最终持久化到 session_store 时带上 run 字段：

```python
await session_store.update(
    plan.session_id,
    phase=plan.phase,
    title=_generate_title(plan),
    last_run_id=run.run_id,
    last_run_status=run.status,
    last_run_error=run.error_code,
)
```

- [ ] **Step 5: 添加 KEEPALIVE 后台 task**

在 `event_stream()` 中，`try` 块之前启动 keepalive：

```python
keepalive_queue: asyncio.Queue[str] = asyncio.Queue()

async def _keepalive_loop():
    try:
        while True:
            await asyncio.sleep(15)
            await keepalive_queue.put(json.dumps({"type": "keepalive"}))
    except asyncio.CancelledError:
        pass

keepalive_task = asyncio.create_task(_keepalive_loop())
```

在 agent chunk 循环中，每次 yield 前检查 keepalive_queue：

```python
while not keepalive_queue.empty():
    yield keepalive_queue.get_nowait()
```

在 `event_stream()` 末尾（`finally` 或 `return` 前）取消 keepalive_task：

```python
keepalive_task.cancel()
```

- [ ] **Step 6: 提交**

```bash
git add backend/main.py
git commit -m "feat: add cancel endpoint, RunRecord lifecycle, and keepalive background task"
```

---

### Task 9: 前端停止按钮 + KEEPALIVE 超时检测

**Files:**
- Modify: `frontend/src/hooks/useSSE.ts`
- Modify: `frontend/src/components/ChatPanel.tsx`

- [ ] **Step 1: 改造 useSSE 添加 AbortController + cancel**

将 `frontend/src/hooks/useSSE.ts` 全文替换为：

```typescript
import { useCallback, useRef } from 'react'
import type { SSEEvent } from '../types/plan'

export function useSSE() {
  const abortRef = useRef<AbortController | null>(null)

  const sendMessage = useCallback(
    async (
      sessionId: string,
      message: string,
      onEvent: (event: SSEEvent) => void,
    ) => {
      const controller = new AbortController()
      abortRef.current = controller

      const response = await fetch(`/api/chat/${sessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
        signal: controller.signal,
      })

      if (!response.ok || !response.body) return

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                const event: SSEEvent = JSON.parse(line.slice(6))
                onEvent(event)
              } catch {
                // skip malformed events
              }
            }
          }
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          // 用户主动取消，不是错误
          return
        }
        throw err
      }
    },
    [],
  )

  const cancel = useCallback(async (sessionId: string) => {
    abortRef.current?.abort()
    abortRef.current = null
    try {
      await fetch(`/api/chat/${sessionId}/cancel`, { method: 'POST' })
    } catch {
      // cancel 请求失败不阻塞 UI
    }
  }, [])

  return { sendMessage, cancel }
}
```

- [ ] **Step 2: 在 ChatPanel 中添加停止按钮和超时检测**

在 `frontend/src/components/ChatPanel.tsx` 中：

1. 从 `useSSE` 解构 `cancel`：
```typescript
const { sendMessage, cancel } = useSSE()
```

2. 添加超时检测 ref：
```typescript
const lastEventTimeRef = useRef<number>(Date.now())
const [connectionWarning, setConnectionWarning] = useState(false)
```

3. 在 `handleSend` 的 `sendMessage` 回调中，每收到事件更新时间戳：
```typescript
await sendMessage(sessionId, userMsg, (event: SSEEvent) => {
  lastEventTimeRef.current = Date.now()
  setConnectionWarning(false)
  // ... 现有事件处理
})
```

4. 添加超时检测 effect：
```typescript
useEffect(() => {
  if (!streaming) return
  const timer = setInterval(() => {
    if (Date.now() - lastEventTimeRef.current > 30000) {
      setConnectionWarning(true)
    }
  }, 5000)
  return () => clearInterval(timer)
}, [streaming])
```

5. 添加停止按钮处理函数：
```typescript
const handleStop = async () => {
  await cancel(sessionId)
  setStreaming(false)
  sendingRef.current = false
}
```

6. 在输入区域，当 `streaming` 为 true 时渲染停止按钮替代发送按钮：
```tsx
{streaming ? (
  <button className="stop-btn" onClick={handleStop} title="停止生成">
    ■
  </button>
) : (
  <button className="send-btn" onClick={handleSend} disabled={!input.trim()}>
    ➤
  </button>
)}
```

7. 在消息列表底部，当 `connectionWarning && streaming` 时显示提示：
```tsx
{connectionWarning && streaming && (
  <div className="connection-warning">
    连接可能已断开，可尝试停止后重新发送
  </div>
)}
```

- [ ] **Step 3: 提交**

```bash
git add frontend/src/hooks/useSSE.ts frontend/src/components/ChatPanel.tsx
git commit -m "feat(frontend): add stop button, AbortController, and connection timeout warning"
```

---

## PR3：继续生成 MVP

### Task 10: RunRecord 扩展 can_continue + Message.incomplete

**Files:**
- Modify: `backend/run.py`
- Modify: `backend/agent/types.py:34-40`
- Test: `backend/tests/test_run.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_run.py` 追加：

```python
def test_run_record_continue_fields():
    r = RunRecord(
        run_id="r1",
        session_id="s1",
        status="failed",
        can_continue=True,
        continuation_context={"type": "partial_text", "partial_assistant_text": "hello"},
    )
    assert r.can_continue is True
    assert r.continuation_context["type"] == "partial_text"


def test_run_record_continue_defaults():
    r = RunRecord(run_id="r1", session_id="s1", status="running")
    assert r.can_continue is False
    assert r.continuation_context is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_run.py::test_run_record_continue_fields -v`
Expected: FAIL — `RunRecord.__init__() got an unexpected keyword argument 'can_continue'`

- [ ] **Step 3: 扩展 RunRecord 和 Message**

在 `backend/run.py` 的 `RunRecord` dataclass 中追加：

```python
@dataclass
class RunRecord:
    run_id: str
    session_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    error_code: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    can_continue: bool = False
    continuation_context: dict | None = None
```

在 `backend/agent/types.py` 的 `Message` dataclass 中追加：

```python
@dataclass
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_result: ToolResult | None = None
    name: str | None = None
    incomplete: bool = False
```

在 `Message.to_dict()` 中追加：
```python
if self.incomplete:
    d["incomplete"] = True
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_run.py tests/test_types.py -v`
Expected: all passed

- [ ] **Step 5: 提交**

```bash
git add backend/run.py backend/agent/types.py backend/tests/test_run.py
git commit -m "feat: extend RunRecord with can_continue and Message with incomplete flag"
```

---

### Task 11: can_continue 判定 + 中断时消息持久化

**Files:**
- Modify: `backend/main.py` — except 块内判定 can_continue + 持久化 partial text

- [ ] **Step 1: 在 event_stream 中添加 accum_text 累积变量**

当前 `event_stream()` 没有累积文本——文本累积发生在 AgentLoop 内部。需要在 endpoint 层也追踪，供中断时使用。

在 `event_stream()` 的 `try` 块开头（`llm_started_at = time.monotonic()` 附近）添加：

```python
accum_text = ""  # 追踪本轮 LLM 输出的文本，供中断恢复使用
```

在处理 `text_delta` 事件时（`if chunk.content:` 分支，行 1550 附近）追加累积：

```python
if chunk.content:
    accum_text += chunk.content
    event_data["content"] = chunk.content
```

- [ ] **Step 2: 在 except LLMError 块中添加 can_continue 判定**

在 `event_stream()` 的 `except LLMError as exc:` 块中（非 cancelled 分支），在 yield error 事件之前：

```python
from run import IterationProgress

progress = agent.progress
can_continue = progress in (
    IterationProgress.PARTIAL_TEXT,
    IterationProgress.TOOLS_READ_ONLY,
)

if can_continue and accum_text.strip():
    # 把不完整的 assistant 消息追加到历史
    messages.append(Message(
        role=Role.ASSISTANT,
        content=accum_text,
        incomplete=True,
    ))
    run.continuation_context = {
        "type": progress.value,
        "partial_assistant_text": accum_text,
    }
    if progress == IterationProgress.TOOLS_READ_ONLY:
        run.continuation_context["completed_tool_count"] = sum(
            1 for m in messages if m.role == Role.TOOL
        )

run.can_continue = can_continue
```

然后修改 error 事件的 `can_continue` 字段：

```python
yield json.dumps({
    "type": "error",
    "error_code": exc.code.value,
    "retryable": exc.retryable,
    "can_continue": can_continue,  # 替代之前的固定 False
    # ... 其他字段
}, ensure_ascii=False)
```

- [ ] **Step 2: 提交**

```bash
git add backend/main.py
git commit -m "feat: determine can_continue from IterationProgress and persist partial messages"
```

---

### Task 12: Continue API endpoint

**Files:**
- Modify: `backend/main.py` — 新增 `/api/chat/{session_id}/continue`

- [ ] **Step 1: 实现 continue endpoint**

在 `backend/main.py` 中新增路由：

```python
@app.post("/api/chat/{session_id}/continue")
async def continue_chat(session_id: str):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    last_run = session.get("_current_run")
    if not last_run or not last_run.can_continue:
        raise HTTPException(status_code=400, detail="Cannot continue this run")

    plan = session["plan"]
    messages = session["messages"]
    agent = session["agent"]
    ctx = last_run.continuation_context or {}
    ctx_type = ctx.get("type", "")

    if ctx_type == "partial_text":
        messages.append(Message(
            role=Role.SYSTEM,
            content="你的上一轮回复因网络中断未完成，请从断点继续，不要重复已说的内容。",
        ))
    elif ctx_type == "tools_read_only":
        messages.append(Message(
            role=Role.SYSTEM,
            content="你已经调用了工具并获得结果，但总结被中断了。请根据已有的工具结果继续回复。",
        ))
    else:
        raise HTTPException(status_code=400, detail=f"Unknown continuation type: {ctx_type}")

    # 重置 run
    run = RunRecord(run_id=str(uuid.uuid4()), session_id=plan.session_id, status="running")
    session["_current_run"] = run
    cancel_event = asyncio.Event()
    session["_cancel_event"] = cancel_event
    agent.cancel_event = cancel_event

    async def event_stream():
        # 复用主 chat endpoint 的 event_stream 逻辑
        # 从 agent.run(messages, phase=plan.phase) 开始
        # ... 与 chat endpoint 中 try/except 块结构相同
        pass

    return EventSourceResponse(event_stream())
```

注意：`event_stream()` 的具体实现应提取为可复用的内部函数，避免在 chat endpoint 和 continue endpoint 之间复制代码。具体做法：

将 `event_stream()` 的核心逻辑提取为 `_run_agent_stream(session, plan, messages, agent, run, cancel_event)` 生成器函数，chat 和 continue 两个 endpoint 都调用它。

- [ ] **Step 2: 提交**

```bash
git add backend/main.py
git commit -m "feat: add /api/chat/{session_id}/continue endpoint for safe resumption"
```

---

### Task 13: 前端继续按钮 + 未完成消息标注

**Files:**
- Modify: `frontend/src/hooks/useSSE.ts`
- Modify: `frontend/src/components/ChatPanel.tsx`

- [ ] **Step 1: useSSE 添加 continueGeneration 方法**

在 `frontend/src/hooks/useSSE.ts` 中新增：

```typescript
const continueGeneration = useCallback(
  async (
    sessionId: string,
    onEvent: (event: SSEEvent) => void,
  ) => {
    const controller = new AbortController()
    abortRef.current = controller

    const response = await fetch(`/api/chat/${sessionId}/continue`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: controller.signal,
    })

    if (!response.ok || !response.body) return

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const event: SSEEvent = JSON.parse(line.slice(6))
              onEvent(event)
            } catch {
              // skip malformed events
            }
          }
        }
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        return
      }
      throw err
    }
  },
  [],
)

return { sendMessage, cancel, continueGeneration }
```

- [ ] **Step 2: ChatPanel 添加继续按钮**

在 `frontend/src/components/ChatPanel.tsx` 中：

1. 解构 `continueGeneration`：
```typescript
const { sendMessage, cancel, continueGeneration } = useSSE()
```

2. 添加 `canContinue` 状态：
```typescript
const [canContinue, setCanContinue] = useState(false)
```

3. 在 error 事件处理中检查 `can_continue`：
```typescript
} else if (event.type === 'error') {
  const message = event.message ?? '模型服务暂时不可用，请稍后重试。'
  const detail = event.error ? `\n\n${event.error}` : ''
  setMessages((prev) =>
    prev.map((item) =>
      item.id === currentAssistantId
        ? { ...item, content: `${message}${detail}` }
        : item,
    ),
  )
  if (event.can_continue) {
    setCanContinue(true)
  }
}
```

4. 添加继续处理函数：
```typescript
const handleContinue = async () => {
  setCanContinue(false)
  sendingRef.current = true
  setStreaming(true)
  const newAssistantId = createMessageId()
  setMessages((prev) => [
    ...prev,
    { id: newAssistantId, role: 'assistant', content: '' },
  ])

  let assistantContent = ''
  try {
    await continueGeneration(sessionId, (event: SSEEvent) => {
      lastEventTimeRef.current = Date.now()
      setConnectionWarning(false)
      if (event.type === 'text_delta' && event.content) {
        assistantContent += event.content
        setMessages((prev) =>
          prev.map((m) =>
            m.id === newAssistantId
              ? { ...m, content: assistantContent }
              : m,
          ),
        )
      }
      // ... 复用其他事件处理逻辑
    })
  } finally {
    sendingRef.current = false
    setStreaming(false)
    setMessages((prev) => prev.filter((m) =>
      !(m.id === newAssistantId && m.role === 'assistant' && !m.content.trim())
    ))
  }
}
```

5. 在消息区域底部，当 `canContinue && !streaming` 时渲染按钮：
```tsx
{canContinue && !streaming && (
  <button className="continue-btn" onClick={handleContinue}>
    继续生成
  </button>
)}
```

- [ ] **Step 3: 提交**

```bash
git add frontend/src/hooks/useSSE.ts frontend/src/components/ChatPanel.tsx
git commit -m "feat(frontend): add continue generation button and incomplete message display"
```

---

## 最终验证

### Task 14: 端到端验证

- [ ] **Step 1: 运行全部后端测试**

Run: `cd backend && python -m pytest tests/ -v --tb=short`
Expected: all passed, no regressions

- [ ] **Step 2: 启动开发服务器手动验证**

1. 启动后端：`cd backend && python main.py`
2. 启动前端：`cd frontend && npm run dev`
3. 验证场景：
   - 正常对话 → 应正常工作，done 事件带 run_id/run_status
   - 流式生成中点击停止 → 应立即中断，保留已输出文本
   - 模拟 LLM 错误 → error 事件应包含 error_code/retryable/can_continue
   - 连接超时 → 30秒后应显示断开提示

- [ ] **Step 3: 提交最终状态**

更新 `PROJECT_OVERVIEW.md` 中的错误处理部分，确保反映新的 LLMError 体系。

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: update PROJECT_OVERVIEW with LLM resilience architecture"
```
