# LLM Providers Slice

## 什么时候读

当任务涉及模型供应商、按阶段切换、streaming、token 计数、错误恢复、cancel/continue 时读取。

## 最小事实

- LLM 抽象是 Protocol：

```python
class LLMProvider(Protocol):
    async def chat(messages, tools, stream): ...
    async def count_tokens(messages) -> int: ...
    async def get_context_window() -> int | None: ...
```

- `config.yaml` 支持按阶段覆写 provider / model。
- 当前支持 OpenAI 与 Anthropic。
- 流式 generator 一旦 yield 过数据，异常后禁止自动重试，避免重复输出。

## 错误归一化

```text
Provider._classify_error
  -> LLMError(code, retryable, provider, status_code)
  -> chat stream SSE error
  -> RunRecord can_continue
  -> 前端继续/停止/未完成消息状态
```

常见分类：
- `TRANSIENT` / `RATE_LIMITED`：未 yield 前可重试。
- `BAD_REQUEST` / `STREAM_INTERRUPTED` / `PROTOCOL_ERROR`：不重试，通知用户。
- 裸 APIError：走 opaque classifier，按状态码/关键词保守归类。

## 关键代码

- `backend/llm/base.py`
- `backend/llm/errors.py`
- `backend/llm/factory.py`
- `backend/llm/openai_provider.py`
- `backend/llm/anthropic_provider.py`
- `backend/run.py`
- `backend/api/orchestration/chat/stream.py`

## 深入阅读

- SSE error 协议：`../deep/sse-events.md`
- 数据流：`data-flow.md`
