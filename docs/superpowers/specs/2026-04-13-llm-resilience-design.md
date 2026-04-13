# LLM 韧性体系：错误归一化 / 停止 / 继续生成

## 概述

当前 LLM 调用失败以 Python 原生异常直接穿透到 SSE endpoint，没有语义化分类。前端没有 AbortController 也没有停止按钮，后端没有取消机制。流式中断后无法恢复。

本设计引入三层改进，按三个独立 PR 递进交付：

1. **PR1** — LLM 错误归一化 + provider 连接重试 + SSE error 事件增强
2. **PR2** — RunRecord + 停止生成 + KEEPALIVE 超时检测
3. **PR3** — 继续生成 MVP（纯文本中断 + 只读工具中断）

## 设计决策记录

| 决策 | 选项 | 选定 | 理由 |
|------|------|------|------|
| 不认识的异常归类策略 | A)保守 B)乐观 C)启发式 | C | HTTP status 做主判断，兜底 PROTOCOL_ERROR |
| 停止检查粒度 | A)粗 B)细 C)混合 | B | 用户最常在 LLM 长输出时按停止，粗粒度覆盖不到 |
| 重试职责分层 | A)Provider B)AgentLoop C)两层分治 | C | 连接失败和流式中断是不同故障，各层管各自 |
| 继续功能 MVP 范围 | A)仅纯文本 B)纯文本+只读工具 C)不做 | B | 只读工具中断更常见，且已有 side_effect 判断基础 |
| RunRecord 持久化方式 | A)纯内存 B)复用 SessionStore C)新建表 | B | 只需最近一次 run 状态，后续升级到 C 改动量小 |

---

## PR1：LLM 错误归一化

### 1.1 错误码枚举

文件：`backend/llm/errors.py`（新建）

```python
from enum import Enum

class LLMErrorCode(str, Enum):
    TRANSIENT          = "LLM_TRANSIENT_ERROR"
    RATE_LIMITED       = "LLM_RATE_LIMITED"
    BAD_REQUEST        = "LLM_BAD_REQUEST"
    STREAM_INTERRUPTED = "LLM_STREAM_INTERRUPTED"
    PROTOCOL_ERROR     = "LLM_PROTOCOL_ERROR"
```

### 1.2 统一异常类

文件：`backend/llm/errors.py`

```python
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
```

字段说明：
- `failure_phase`：`"connection"` | `"streaming"` | `"parsing"` | `"cancelled"`，标识异常发生阶段。`"cancelled"` 由停止生成功能（PR2）引入
- `partial_output`：流式中途断裂时为 `True`，表示已产出部分内容
- `retry_after`：仅 `RATE_LIMITED` 时填充，单位秒
- `raw_error`：原始异常 repr，用于日志定位

### 1.3 Provider 归一化

在 `anthropic_provider.py` 和 `openai_provider.py` 中各新增 `_classify_error()` 方法。

**分类规则（启发式）：**

| 条件 | 错误码 | retryable | failure_phase |
|------|--------|-----------|---------------|
| HTTP 429 / SDK RateLimitError | `RATE_LIMITED` | `true` | `connection` |
| HTTP 5xx / SDK APIStatusError(5xx) | `TRANSIENT` | `true` | `connection` |
| HTTP 400/422 / SDK BadRequestError | `BAD_REQUEST` | `false` | `connection` |
| `ConnectionError` / `TimeoutError` / DNS 错误 | `TRANSIENT` | `true` | `connection` |
| 流式读取中途 EOF / `ChunkedEncodingError` / `httpx.RemoteProtocolError` | `STREAM_INTERRUPTED` | `true` | `streaming` |
| `json.JSONDecodeError`（解析 tool call 参数） | `PROTOCOL_ERROR` | `false` | `parsing` |
| Anthropic `anthropic.APIConnectionError` | `TRANSIENT` | `true` | `connection` |
| Anthropic `anthropic.APIStatusError` | 按 status_code 归类 | — | `connection` |
| OpenAI `openai.APIConnectionError` | `TRANSIENT` | `true` | `connection` |
| OpenAI `openai.APIStatusError` | 按 status_code 归类 | — | `connection` |
| 其他不认识的异常 | `PROTOCOL_ERROR` | `false` | — |

**实现位置：**

```python
# anthropic_provider.py, chat() 方法
except Exception as exc:
    self._write_debug_log("error", {...})
    raise self._classify_error(exc)  # 替代原来的 raise

# openai_provider.py, chat() 方法（同理）
```

两个 provider 各自实现 `_classify_error()`，内部识别各自 SDK 的异常类型。公共的 HTTP status 启发式逻辑可以提取到 `llm/errors.py` 的模块级函数 `classify_by_http_status(status_code, raw_error) -> LLMError`。

### 1.4 连接阶段重试

仅在 `failure_phase="connection"` 且 `retryable=True` 时重试。在各 provider 的 `chat()` 方法内部实现：

```python
async def chat(self, messages, **kwargs):
    max_conn_retries = 2
    for attempt in range(max_conn_retries + 1):
        try:
            # 创建请求 / 建立流式连接
            ...
            # 一旦开始收到 token，就进入流式阶段，不再由 provider 重试
            async for chunk in ...:
                yield chunk
            return
        except LLMError as e:
            if e.failure_phase != "connection" or not e.retryable or attempt == max_conn_retries:
                raise
            delay = [1.0, 3.0][attempt]
            await asyncio.sleep(delay)
```

注意：重试循环包裹的范围只到"连接建立 + 首个 token"之前。一旦流式开始，异常直接抛出。

### 1.5 SSE error 事件增强

文件：`backend/main.py`，`event_stream()` 的 `except` 块

当前：
```python
except Exception as exc:
    yield {"type": "error", "error_code": "AGENT_STREAM_ERROR", "error": str(exc), "message": "..."}
```

改为：
```python
except LLMError as exc:
    yield {
        "type": "error",
        "error_code": exc.code.value,
        "retryable": exc.retryable,
        "can_continue": False,  # PR1 阶段固定 false，PR3 启用
        "provider": exc.provider,
        "model": exc.model,
        "failure_phase": exc.failure_phase,
        "message": _user_friendly_message(exc),
        "error": exc.raw_error,
    }
except Exception as exc:
    yield {
        "type": "error",
        "error_code": "AGENT_STREAM_ERROR",
        "retryable": False,
        "can_continue": False,
        "message": "系统内部错误，请稍后重试。",
        "error": str(exc),
    }
```

`_user_friendly_message()` 根据 `LLMErrorCode` 返回中文提示：
- `TRANSIENT` → "模型服务暂时繁忙，本轮回复已中断。请稍后重试。"
- `RATE_LIMITED` → "请求过于频繁，请稍后再试。"
- `BAD_REQUEST` → "请求参数异常，请缩短对话长度后重试。"
- `STREAM_INTERRUPTED` → "模型回复过程中连接中断。请重试。"
- `PROTOCOL_ERROR` → "模型返回格式异常，请重试或切换模型。"

### 1.6 前端 SSEEvent 类型扩展

文件：`frontend/src/types/plan.ts`

```typescript
export interface SSEEvent {
  // ... 现有字段不变
  retryable?: boolean
  can_continue?: boolean
  failure_phase?: string
}
```

前端 error 事件处理逻辑暂不变（PR2/PR3 再改），但类型定义先到位。

### 1.7 改动文件清单

| 文件 | 操作 |
|------|------|
| `backend/llm/errors.py` | 新建 |
| `backend/llm/anthropic_provider.py` | 修改：`_classify_error()` + 连接重试 |
| `backend/llm/openai_provider.py` | 修改：`_classify_error()` + 连接重试 |
| `backend/main.py` | 修改：`event_stream()` except 块 |
| `frontend/src/types/plan.ts` | 修改：SSEEvent 扩展字段 |

---

## PR2：停止生成 + RunRecord + KEEPALIVE

### 2.1 RunRecord

文件：`backend/run.py`（独立于 llm 层，因为 RunRecord 是 endpoint 层概念）

```python
@dataclass
class RunRecord:
    run_id: str
    session_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    error_code: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
```

生命周期：
1. `/api/chat` 入口创建 → `status="running"`
2. `event_stream()` 正常结束 → `status="completed"`
3. 捕获 `LLMError` → `status="failed"`
4. 收到取消信号 → `status="cancelled"`
5. 最终持久化到 `session_store.update()`

### 2.2 SessionStore 扩展

在现有 sessions 表新增三列：

```sql
ALTER TABLE sessions ADD COLUMN last_run_id TEXT;
ALTER TABLE sessions ADD COLUMN last_run_status TEXT;
ALTER TABLE sessions ADD COLUMN last_run_error TEXT;
```

`session_store.update()` 方法扩展接受这三个可选参数。

### 2.3 停止生成——后端

**取消信号：**

session 字典新增 `_cancel_event: asyncio.Event`。每次 `/api/chat` 调用时重置。

**新增 API：**

```
POST /api/chat/{session_id}/cancel
```

实现：
```python
@app.post("/api/chat/{session_id}/cancel")
async def cancel_chat(session_id: str):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    cancel_event = session.get("_cancel_event")
    if cancel_event:
        cancel_event.set()
    return {"status": "cancelled"}
```

**AgentLoop 改造：**

构造函数新增 `cancel_event: asyncio.Event | None = None` 参数。

检查点位置：
1. 迭代入口（`for iteration` 循环顶部）
2. LLM 流式每个 chunk yield 前
3. 工具执行前（`tool_engine.execute()` 调用前）

```python
def _check_cancelled(self) -> None:
    if self.cancel_event and self.cancel_event.is_set():
        raise LLMError(
            code=LLMErrorCode.TRANSIENT,
            message="用户取消了本轮生成",
            retryable=False,
            provider=getattr(self.llm, "provider_name", "unknown"),
            model=getattr(self.llm, "model", "unknown"),
            failure_phase="cancelled",
        )
```

注意：两个 provider 需各自暴露 `provider_name` 属性（`"anthropic"` / `"openai"`）。

**SSE endpoint 处理取消：**

取消是用户主动行为，不发 error 事件：
```python
except LLMError as exc:
    if exc.failure_phase == "cancelled":
        run.status = "cancelled"
        yield {"type": "done", "run_status": "cancelled"}
    else:
        run.status = "failed"
        run.error_code = exc.code.value
        yield {"type": "error", ...}
```

### 2.4 停止生成——前端

**useSSE 改造：**

```typescript
export function useSSE() {
  const abortRef = useRef<AbortController | null>(null)

  const sendMessage = useCallback(
    async (sessionId, message, onEvent) => {
      const controller = new AbortController()
      abortRef.current = controller
      const response = await fetch(`/api/chat/${sessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
        signal: controller.signal,
      })
      // ... 流式读取逻辑不变
    }, []
  )

  const cancel = useCallback(async (sessionId: string) => {
    abortRef.current?.abort()
    await fetch(`/api/chat/${sessionId}/cancel`, { method: 'POST' })
  }, [])

  return { sendMessage, cancel }
}
```

**ChatPanel 停止按钮：**

当 `streaming === true` 时，发送按钮替换为停止按钮（图标从发送 → 方块停止）。点击调用 `cancel(sessionId)`。已接收的 partial text 保留显示。

### 2.5 KEEPALIVE 超时检测

**后端：**

`event_stream()` 内启动后台 task，每 15 秒检查一次：如果距上次 yield 事件已超过 15 秒，yield 一个 keepalive 事件。`event_stream()` 结束时取消该 task。

`event_stream()` 维护一个 `last_event_time` 变量，每次 yield 事件时更新。后台 task 通过 `asyncio.Queue` 向 `event_stream()` 注入 keepalive 事件：

```python
keepalive_queue: asyncio.Queue[str] = asyncio.Queue()

async def keepalive_loop():
    while True:
        await asyncio.sleep(15)
        await keepalive_queue.put(json.dumps({"type": "keepalive"}))

# event_stream() 内部，在主循环空闲时检查 queue
while not keepalive_queue.empty():
    yield keepalive_queue.get_nowait()
```

**前端：**

ChatPanel 维护 `lastEventTime` ref。超过 30 秒无事件时，在 assistant 消息下方显示灰色提示行："连接可能已断开，可尝试停止后重新发送"。收到任何事件后提示消失。

### 2.6 IterationProgress 预埋

PR2 中在 AgentLoop 里预埋 `IterationProgress` 枚举和追踪逻辑，但不使用其恢复能力（PR3 才用）。

```python
class IterationProgress(str, Enum):
    NO_OUTPUT          = "no_output"
    PARTIAL_TEXT       = "partial_text"
    PARTIAL_TOOL_CALL  = "partial_tool_call"
    TOOLS_READ_ONLY    = "tools_read_only"
    TOOLS_WITH_WRITES  = "tools_with_writes"
```

AgentLoop 在迭代内维护 `self._progress: IterationProgress`，随事件推进更新。PR3 通过读取这个状态来判定 `can_continue`。

### 2.7 SSEEvent 增强（done 事件）

`done` 事件增加 `run_status` 和 `run_id` 字段：

```json
{"type": "done", "run_id": "uuid", "run_status": "completed"}
```

前端可据此确认本轮是否正常结束。

### 2.8 改动文件清单

| 文件 | 操作 |
|------|------|
| `backend/run.py` | 新建（RunRecord + IterationProgress） |
| `backend/agent/loop.py` | 修改：cancel_event + _check_cancelled + IterationProgress 追踪 |
| `backend/main.py` | 修改：RunRecord 生命周期 + cancel endpoint + keepalive task + done 事件增强 |
| `backend/storage/session_store.py` | 修改：新增三列 + update 方法扩展 |
| `frontend/src/hooks/useSSE.ts` | 修改：AbortController + cancel 方法 |
| `frontend/src/components/ChatPanel.tsx` | 修改：停止按钮 + keepalive 超时提示 |
| `frontend/src/types/plan.ts` | 修改：SSEEvent 增加 run_id / run_status |

---

## PR3：继续生成 MVP

### 3.1 can_continue 判定

在 `event_stream()` 的 `except LLMError` 块中，读取 AgentLoop 的 `_progress` 状态：

| IterationProgress | can_continue | 理由 |
|---|---|---|
| `NO_OUTPUT` | `false` | 无产出，直接重新发消息即可 |
| `PARTIAL_TEXT` | `true` | 保留 partial text 让 LLM 续写 |
| `PARTIAL_TOOL_CALL` | `false` | JSON 不完整，无法安全恢复 |
| `TOOLS_READ_ONLY` | `true` | 工具结果已在消息历史中 |
| `TOOLS_WITH_WRITES` | `false` | 有写状态，不能重放 |

### 3.2 RunRecord 扩展

```python
@dataclass
class RunRecord:
    # ... PR2 字段
    can_continue: bool = False
    continuation_context: dict | None = None
```

`continuation_context` 结构：
- PARTIAL_TEXT 场景：`{"type": "partial_text", "partial_assistant_text": "..."}`
- TOOLS_READ_ONLY 场景：`{"type": "tools_read_only", "partial_assistant_text": "...", "completed_tool_count": N}`

### 3.3 继续 API

```
POST /api/chat/{session_id}/continue
```

处理逻辑：
1. 检查 `last_run.can_continue`，为 `false` 返回 400
2. 读取 `continuation_context`
3. 恢复策略：
   - `partial_text`：把 partial text 作为 assistant message 追加到 messages，注入 system 提示 "你的上一轮回复因网络中断未完成，请从断点继续，不要重复已说的内容。"
   - `tools_read_only`：messages 中已有工具结果，注入 system 提示 "你已经调用了工具并获得结果，但总结被中断了。请根据已有的工具结果继续回复。"
4. 创建新 `run_id`，走正常 `event_stream()` 流程
5. 返回 SSE 流

### 3.4 消息历史一致性

**流式中断时：**
- `event_stream()` except 块检查累积文本 `accum_text`
- 非空时，追加为 assistant message 到 messages 列表
- 该消息带 `incomplete=True` 标记（Message dataclass 新增可选字段）
- 持久化到 `_persist_messages()`

**继续时：**
- 不删除不完整消息
- LLM 看到完整上下文（包括不完整的 assistant 回复 + system 恢复提示）
- 新输出追加为新的 assistant message

**不继续时：**
- 不完整消息保留在历史中
- 前端展示时检查到 error 事件后的 assistant 消息，尾部标注"（回复未完成）"

### 3.5 前端继续按钮

当收到 `can_continue: true` 的 error 事件时：
- 在错误提示下方渲染"继续生成"按钮
- 点击后调用 `POST /api/chat/{session_id}/continue`
- 复用 SSE 流式处理，在当前位置继续追加消息
- 按钮一次性，不重复渲染

### 3.6 改动文件清单

| 文件 | 操作 |
|------|------|
| `backend/run.py` | 修改：RunRecord 新增 can_continue / continuation_context |
| `backend/agent/types.py` | 修改：Message 新增 incomplete 可选字段 |
| `backend/agent/loop.py` | 修改：暴露 `_progress` 给调用方 |
| `backend/main.py` | 修改：can_continue 判定 + continue endpoint + 中断时消息持久化 |
| `frontend/src/components/ChatPanel.tsx` | 修改：继续按钮 + 未完成消息标注 |
| `frontend/src/hooks/useSSE.ts` | 修改：新增 continueGeneration 方法 |

---

## 不在范围内

- 完整 run 历史（新建 runs 表） — 等 B 方案验证后按需升级
- 多 provider failover（切换到备用模型） — 独立功能，不在本设计范围
- 工具执行超时 — 当前 tool engine 已有超时机制，不需要额外改造
- 前端离线重连 — 超出 MVP 范围

## 交付顺序

```
PR1 (LLM 错误归一化)
 └── PR2 (停止生成 + RunRecord)
      └── PR3 (继续生成 MVP)
```

每个 PR 独立可交付、可验证。PR1 不依赖任何新数据模型。PR2 依赖 PR1 的 LLMError。PR3 依赖 PR2 的 RunRecord 和 IterationProgress。
