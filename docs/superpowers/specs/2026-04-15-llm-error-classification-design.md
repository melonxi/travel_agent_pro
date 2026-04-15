# LLM Provider 错误分类修复：不透明 APIError 的准确归类

- 日期：2026-04-15
- 范围：backend（`llm/errors.py`、`llm/openai_provider.py`、`llm/anthropic_provider.py`）
- 关联 Bug：`sess_d5d536bafeed` 澳门会话，前端报错 "连接阶段：模型返回格式异常，请重试或切换模型"；真实原因是讯飞网关返回 `EngineInternalError:The system is busy`
- 关联 TODO：`docs/TODO.md` 第 2 条

## 1. 问题陈述

### 1.1 现象

用户在"澳门两日游"会话中看到：

- 主文案：`本轮生成未完成，请调整后重新发送。`
- 详情：`连接阶段：模型返回格式异常，请重试或切换模型。`

后端日志（节选）：

```
openai.APIError: Xunfei request failed with ... code: 10012,
message: EngineInternalError:The system is busy, please try again later.
```

### 1.2 根因

`backend/llm/openai_provider.py` 的 `_classify_error` 对异常按以下优先级分类：

1. `APIStatusError` 子类 → `classify_by_http_status(status_code)`
2. `RateLimitError` / `APITimeoutError` / `APIConnectionError` → 专门分支
3. 其它 → **fallthrough：`PROTOCOL_ERROR + retryable=False + failure_phase="connection"`**

讯飞等 OpenAI 兼容网关在上游错误时，常常抛裸 `openai.APIError`（不是 `APIStatusError` 子类），即使 body 里明确写了 `code: 10012` + `system is busy`，也走到 fallthrough，被归为 `PROTOCOL_ERROR`。前端据此渲染"模型返回格式异常"，**误导用户以为是模型输出格式问题，实则是上游暂时繁忙**。

### 1.3 Bug 代码位置

`backend/llm/openai_provider.py:82-90`（fallthrough）：

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

`backend/llm/anthropic_provider.py:96-104` 存在对称问题。

### 1.4 风险范围澄清

- `APIStatusError` 强类型分支**行为正确**，不改
- `_RETRY_DELAYS = (1.0, 3.0)` 自动重试机制**不改**
- 前端 `_LLM_ERROR_MESSAGES` 文案表、`createErrorFeedback` **不改**
- 本次只改"裸 APIError → fallthrough"这一条路径

## 2. 设计目标

1. **正确归类裸 APIError**：从 `str(exc)` / `exc.body` 里尽量抽取信号（HTTP 状态码、gateway 错误码、关键词），归入最贴近真实原因的 `LLMErrorCode`
2. **兼容多家 provider**：讯飞、DeepSeek、ModelArts、智谱等 OpenAI 兼容网关都有各自错误格式；采用 **opencode 风格**的分层策略（结构化信号优先 + 关键词兜底 + 不认识就保守）
3. **准确兜底**：完全不认识的异常归为 `TRANSIENT + retryable=False` —— 文案准确（"模型暂时繁忙"）、不触发盲目自动重试
4. **最小改动面**：两个 provider 的 fallthrough 各改一处；公共工具抽到 `llm/errors.py`

## 3. 非目标

- 不处理已强类型的异常（`APIStatusError` 等，已正确）
- 不改前端文案与错误映射表
- 不动重试次数/退避策略
- 不做"自动切换 provider"类的高级恢复
- 不矫正历史会话数据

## 4. 架构

### 4.1 核心思路

保持 provider `_classify_error` 的现有三层结构，只改 fallthrough：

```
provider._classify_error(exc)
  ├─ 强类型（APIStatusError 等）→ classify_by_http_status（不动）
  └─ 裸 APIError / 其它            → classify_opaque_api_error（新）
     ├─ 匹配规则   → TRANSIENT / RATE_LIMITED / BAD_REQUEST
     └─ 不匹配     → TRANSIENT + retryable=False（opencode 风格兜底）
```

### 4.2 分类优先级（由强到弱）

1. **结构化 HTTP 状态码**：`exc.status_code` / `exc.body["status_code"]` / 文本 regex `status code: (\d{3})`
2. **Gateway 特定错误码**：讯飞 `10012`、ModelArts `xxx` 等（白名单）
3. **关键词**：busy/繁忙/try again/请稍后 → TRANSIENT；rate limit/too many/quota → RATE_LIMITED；invalid/malformed/param validation → BAD_REQUEST
4. **兜底**：`TRANSIENT + retryable=False`

### 4.3 关键组件

| 组件 | 位置 | 职责 |
|---|---|---|
| `classify_opaque_api_error` | `llm/errors.py`（新增） | 纯函数，输入异常 + provider/model，输出 `LLMError` |
| 规则常量 | `llm/errors.py` 底部（或 `_opaque_rules.py`） | 关键词列表、HTTP 状态集合、regex、gateway code 白名单 |
| openai fallthrough 改造 | `llm/openai_provider.py:82-90` | 删掉硬编码 `PROTOCOL_ERROR`，改调新函数 |
| anthropic fallthrough 改造 | `llm/anthropic_provider.py:96-104` | 同上 |

### 4.4 retryable 语义（与 opencode 对齐）

| 分类路径 | code | retryable | 触发自动重试 |
|---|---|---|---|
| 关键词/码命中 transient，或 5xx | TRANSIENT | True | 是 |
| 命中 rate limit/429 | RATE_LIMITED | True | 是（有退避） |
| 命中 bad request/400/422 | BAD_REQUEST | False | 否 |
| 全部不命中 | TRANSIENT | **False** | 否 |

**关键取舍**：能认出来 → 敢重试；不认识 → 保守（文案准确但不盲目重试）。

### 4.5 不变的部分

- 强类型异常分支
- `classify_by_http_status`
- 重试机制、退避策略
- `_LLM_ERROR_MESSAGES` 文案表、前端渲染逻辑
- `failure_phase` 语义（调用方传入）

## 5. 具体改动点

### 5.1 新增 `llm/errors.py` 规则常量

```python
_STATUS_CODE_RE = re.compile(r"status code[:\s]+(\d{3})", re.IGNORECASE)

_TRANSIENT_KEYWORDS = (
    "busy", "繁忙", "try again", "请稍后",
    "temporarily unavailable", "engine internal",
    "system is busy", "overloaded",
)
_RATE_LIMITED_KEYWORDS = ("rate limit", "too many requests", "quota exceeded")
_BAD_REQUEST_KEYWORDS = ("invalid request", "malformed", "param validation")

_GATEWAY_TRANSIENT_CODES = ("10012",)  # 讯飞 EngineInternalError
```

### 5.2 新增 `classify_opaque_api_error`

```python
def classify_opaque_api_error(
    exc: Exception,
    *,
    provider: str,
    model: str,
    failure_phase: str = "connection",
) -> LLMError:
    """从裸 APIError 中尽量抽取分类信号；不认识则返回
    TRANSIENT+retryable=False 作为准确但保守的兜底。"""
    text = f"{exc!s}".lower()
    body_text = _extract_body_text(exc).lower()
    haystack = f"{text} {body_text}"

    # 1. 结构化状态码
    status = _extract_status_code(exc)
    if status is not None:
        return classify_by_http_status(
            status, provider=provider, model=model,
            failure_phase=failure_phase, raw_error=repr(exc),
        )

    # 2. gateway 特定码
    for code in _GATEWAY_TRANSIENT_CODES:
        if code in haystack:
            return _make(LLMErrorCode.TRANSIENT, True, ...)

    # 3. 关键词
    if any(kw in haystack for kw in _TRANSIENT_KEYWORDS):
        return _make(LLMErrorCode.TRANSIENT, True, ...)
    if any(kw in haystack for kw in _RATE_LIMITED_KEYWORDS):
        return _make(LLMErrorCode.RATE_LIMITED, True, ...)
    if any(kw in haystack for kw in _BAD_REQUEST_KEYWORDS):
        return _make(LLMErrorCode.BAD_REQUEST, False, ...)

    # 4. 兜底
    return _make(LLMErrorCode.TRANSIENT, False, ...)
```

### 5.3 `openai_provider.py` fallthrough 改造

原 `lines 82-90` 改为：

```python
return classify_opaque_api_error(
    exc,
    provider="openai",
    model=self.model,
    failure_phase=failure_phase,
)
```

### 5.4 `anthropic_provider.py` 对称改造

原 `lines 96-104`，`provider="anthropic"`。

## 6. 数据流

```
provider.complete() 抛异常
  ↓
_classify_error(exc)
  ├─ isinstance(exc, APIStatusError) → classify_by_http_status (不动)
  ├─ isinstance(exc, RateLimitError/APITimeoutError/APIConnectionError) → 专门分支 (不动)
  └─ 其它 → classify_opaque_api_error
     ├─ 抽结构化状态码 → classify_by_http_status
     ├─ 查 gateway 码 → TRANSIENT+retryable=True
     ├─ 关键词匹配 → TRANSIENT/RATE_LIMITED/BAD_REQUEST
     └─ 不匹配 → TRANSIENT+retryable=False
  ↓
agent loop 收到 LLMError
  ├─ retryable=True → 走 _RETRY_DELAYS 自动重试
  └─ retryable=False → 返回给前端
  ↓
前端 createErrorFeedback 查 _LLM_ERROR_MESSAGES 渲染
```

## 7. 不变量

1. `classify_opaque_api_error` 永远返回 `LLMError`，不抛异常
2. `APIStatusError` 等强类型分支行为零改动
3. 裸 `APIError` 路径再也不返回 `PROTOCOL_ERROR`
4. `_LLM_ERROR_MESSAGES` 表与前端渲染不改动
5. `failure_phase` 由调用方传入，工具函数原样透传

## 8. 边界情况

| 场景 | 行为 |
|---|---|
| 异常无 body、str(exc) 为空 | 走兜底：TRANSIENT+retryable=False |
| body 是 dict、有 `status_code` 字段 | 优先级最高，走 `classify_by_http_status` |
| body 是 bytes、解析失败 | 解析 best-effort；失败退回 str(exc) 文本搜索 |
| 关键词同时匹配多类（如 "rate limit, please try again"）| 按优先级：transient → rate_limited → bad_request；先命中先返回 |
| 中文异常文本 | 关键词表含"繁忙/请稍后"，覆盖国内 provider |
| 完全未知的 provider 自定义错误 | 兜底 TRANSIENT+retryable=False（保守、文案准确） |

## 9. 测试计划

### 9.1 单元测试（`backend/tests/test_classify_opaque_api_error.py`）

| 用例 | 断言 |
|---|---|
| `test_xunfei_busy_matches_transient` | body/str 含 "system is busy" → TRANSIENT, retryable=True |
| `test_xunfei_gateway_code_10012` | 含 `code: 10012` → TRANSIENT, retryable=True |
| `test_status_code_400_regex` | 文本含 `status code: 400` → BAD_REQUEST, retryable=False |
| `test_status_code_500_regex` | 文本含 `status code: 500` → TRANSIENT, retryable=True |
| `test_rate_limit_keyword` | 含 "rate limit exceeded" → RATE_LIMITED, retryable=True |
| `test_too_many_requests_keyword` | 含 "too many requests" → RATE_LIMITED |
| `test_invalid_request_keyword` | 含 "invalid request param" → BAD_REQUEST, retryable=False |
| `test_unknown_falls_back` | 完全不匹配任何规则 → TRANSIENT, retryable=False |
| `test_cn_keyword_繁忙` | 含"繁忙" → TRANSIENT |
| `test_cn_keyword_请稍后` | 含"请稍后重试" → TRANSIENT |
| `test_failure_phase_preserved` | 传 `failure_phase="streaming"` → 结果保留 |
| `test_raw_error_populated` | `LLMError.raw_error == repr(exc)` |
| `test_provider_and_model_set` | 字段值来自参数 |

### 9.2 真实日志回归 fixture

新建 `backend/tests/fixtures/opaque_api_errors/`：

- `xunfei_busy.txt` — 本次澳门会话遇到的 `EngineInternalError:The system is busy`
- `xunfei_400_invalid_messages.txt` — 上一轮香港会话的 400 协议错
- `README.md` — 添加新 fixture 的规范

测试函数遍历该目录，按文件名解析期望分类（如 `xunfei_busy__transient.txt`），断言 `classify_opaque_api_error` 输出稳定。

### 9.3 Provider 集成测试调整

`backend/tests/test_openai_provider.py` 补两条：
- `test_opaque_api_error_now_classified_as_transient`：mock 抛裸 APIError（busy 文本）→ 断言 code=TRANSIENT（不再是 PROTOCOL_ERROR）
- `test_api_status_error_path_unchanged`：强类型分支行为回归

对 `anthropic_provider.py` 做对称测试（若已有 test 文件）。

### 9.4 人工验证

本地用能复现讯飞繁忙的场景复测一遍，确认前端文案从"连接阶段：模型返回格式异常"变为"模型服务暂时繁忙，本轮回复已中断。请稍后重试。"。作为 DoD 的人工门。

## 10. 回归风险评估

| 风险 | 评估 |
|---|---|
| 关键词匹配误杀（如普通对话里出现 "busy"）| 只在异常分类路径触发，对话内容不进入；风险零 |
| 规则表漏掉某 provider 的错误 | 兜底为 TRANSIENT+retryable=False，文案仍准确；渐进补充 fixture 即可 |
| 原 PROTOCOL_ERROR 使用者受影响 | 前端文案表同时保留 PROTOCOL_ERROR 条目，其它路径（真正的格式错误）仍可走该分类；不影响 |
| retryable 从 False 变 True 的场景触发过度重试 | 仅在"关键词命中"时才改为 True，且有现成的 `_RETRY_DELAYS` 限流 |
| 异常本身的 str/body 抽取抛异常 | 新函数内部做 best-effort + try/except；保证永不抛 |

## 11. 后续跟进（不在本 spec 范围）

- 规则表的持续补充：每遇一个新的 provider 错误模式，加一条 fixture + 可能的关键词
- 若将来发现结构化字段比 str 解析更可靠，优先级可调整
- 考虑把规则从代码常量迁到配置文件（目前数量少，不必要）
