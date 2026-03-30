# 可观测性 Phase B：结构化调试日志（Span Events）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为已有 span 添加结构化 event，让开发者在 Jaeger 中点开 span 即可看到 tool 出入参、LLM 请求摘要、phase 快照等调试信息。

**Architecture:** 在 Phase A tracing 基础上，为 `tool.execute`、`llm.chat`、`phase.transition`、`context.should_compress` 四类 span 添加 `span.add_event()` 调用。所有 event payload 通过 `truncate()` 控制大小。零新增依赖，仅修改已有文件。

**Tech Stack:** OpenTelemetry Python SDK（`span.add_event()`）、pytest + InMemorySpanExporter

---

### Task 1: 新增 truncate 函数和 event 名称常量

**Files:**
- Modify: `backend/telemetry/attributes.py`
- Create: `backend/tests/test_telemetry_events.py`

- [ ] **Step 1: 编写 truncate 和常量的失败测试**

在 `backend/tests/test_telemetry_events.py` 中：

```python
from telemetry.attributes import (
    truncate,
    EVENT_TOOL_INPUT,
    EVENT_TOOL_OUTPUT,
    EVENT_LLM_REQUEST,
    EVENT_LLM_RESPONSE,
    EVENT_PHASE_PLAN_SNAPSHOT,
    EVENT_CONTEXT_COMPRESSION,
)


def test_truncate_short_string():
    assert truncate("hello") == "hello"


def test_truncate_exact_boundary():
    s = "a" * 512
    assert truncate(s) == s


def test_truncate_long_string():
    s = "a" * 600
    result = truncate(s)
    assert len(result) == 512 + len("...(truncated)")
    assert result.endswith("...(truncated)")


def test_truncate_custom_max():
    s = "a" * 300
    result = truncate(s, max_len=100)
    assert result == "a" * 100 + "...(truncated)"


def test_event_constants_exist():
    assert EVENT_TOOL_INPUT == "tool.input"
    assert EVENT_TOOL_OUTPUT == "tool.output"
    assert EVENT_LLM_REQUEST == "llm.request"
    assert EVENT_LLM_RESPONSE == "llm.response"
    assert EVENT_PHASE_PLAN_SNAPSHOT == "phase.plan_snapshot"
    assert EVENT_CONTEXT_COMPRESSION == "context.compression"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_telemetry_events.py -v`
Expected: FAIL — `ImportError: cannot import name 'truncate'`

- [ ] **Step 3: 实现 truncate 函数和 event 常量**

在 `backend/telemetry/attributes.py` 末尾追加：

```python


# --- Phase B: Span Event Names ---

EVENT_TOOL_INPUT = "tool.input"
EVENT_TOOL_OUTPUT = "tool.output"
EVENT_LLM_REQUEST = "llm.request"
EVENT_LLM_RESPONSE = "llm.response"
EVENT_PHASE_PLAN_SNAPSHOT = "phase.plan_snapshot"
EVENT_CONTEXT_COMPRESSION = "context.compression"


def truncate(value: str, max_len: int = 512) -> str:
    if len(value) <= max_len:
        return value
    return value[:max_len] + "...(truncated)"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_telemetry_events.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add backend/telemetry/attributes.py backend/tests/test_telemetry_events.py
git commit -m "feat(telemetry): add truncate function and event name constants for Phase B"
```

---

### Task 2: tool.execute span 添加 tool.input 和 tool.output event

**Files:**
- Modify: `backend/tools/engine.py`
- Modify: `backend/tests/test_telemetry_tool_engine.py`

- [ ] **Step 1: 编写 tool event 的失败测试**

在 `backend/tests/test_telemetry_tool_engine.py` 末尾追加：

```python
import json
from telemetry.attributes import EVENT_TOOL_INPUT, EVENT_TOOL_OUTPUT


async def test_tool_execute_has_input_event(otel_exporter):
    engine = ToolEngine()

    async def my_tool(**kwargs):
        return {"result": "ok"}

    engine.register(ToolDef(
        name="test_tool", description="test", phases=[1], parameters={}, _fn=my_tool,
    ))

    call = ToolCall(id="t1", name="test_tool", arguments={"dest": "Tokyo"})
    await engine.execute(call)

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "tool.execute")
    events = span.events
    input_event = next(e for e in events if e.name == EVENT_TOOL_INPUT)
    assert "arguments" in input_event.attributes
    parsed = json.loads(input_event.attributes["arguments"])
    assert parsed["dest"] == "Tokyo"


async def test_tool_execute_has_output_event_success(otel_exporter):
    engine = ToolEngine()

    async def my_tool(**kwargs):
        return {"flights": ["ANA"]}

    engine.register(ToolDef(
        name="test_tool", description="test", phases=[1], parameters={}, _fn=my_tool,
    ))

    call = ToolCall(id="t1", name="test_tool", arguments={})
    await engine.execute(call)

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "tool.execute")
    events = span.events
    output_event = next(e for e in events if e.name == EVENT_TOOL_OUTPUT)
    assert "data" in output_event.attributes


async def test_tool_execute_has_output_event_error(otel_exporter):
    engine = ToolEngine()

    async def fail_tool(**kwargs):
        raise ToolError("bad", error_code="BAD_INPUT")

    engine.register(ToolDef(
        name="fail_tool", description="test", phases=[1], parameters={}, _fn=fail_tool,
    ))

    call = ToolCall(id="t2", name="fail_tool", arguments={})
    await engine.execute(call)

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "tool.execute")
    events = span.events
    output_event = next(e for e in events if e.name == EVENT_TOOL_OUTPUT)
    assert "error" in output_event.attributes
    assert "error_code" in output_event.attributes


async def test_tool_input_event_truncated(otel_exporter):
    engine = ToolEngine()

    async def my_tool(**kwargs):
        return {"ok": True}

    engine.register(ToolDef(
        name="test_tool", description="test", phases=[1], parameters={}, _fn=my_tool,
    ))

    long_arg = "x" * 1000
    call = ToolCall(id="t3", name="test_tool", arguments={"data": long_arg})
    await engine.execute(call)

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "tool.execute")
    input_event = next(e for e in events if e.name == "tool.input")
    assert input_event.attributes["arguments"].endswith("...(truncated)")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_telemetry_tool_engine.py::test_tool_execute_has_input_event -v`
Expected: FAIL — `StopIteration` (no matching event)

- [ ] **Step 3: 在 engine.py 中添加 event**

修改 `backend/tools/engine.py`，添加导入和 event 调用：

导入区追加：
```python
import json
from telemetry.attributes import TOOL_NAME, TOOL_STATUS, TOOL_ERROR_CODE, EVENT_TOOL_INPUT, EVENT_TOOL_OUTPUT, truncate
```

在 `execute()` 方法中：

1. 在 `tool_def = self._tools.get(call.name)` 之后、`if not tool_def:` 之前添加：
```python
            span.add_event(EVENT_TOOL_INPUT, {
                "arguments": truncate(json.dumps(call.arguments, ensure_ascii=False)),
            })
```

2. 在成功 `return ToolResult(...)` 之前添加：
```python
                span.add_event(EVENT_TOOL_OUTPUT, {
                    "data": truncate(json.dumps(data, ensure_ascii=False)),
                })
```

3. 在 `ToolError` 捕获的 `return ToolResult(...)` 之前添加：
```python
                span.add_event(EVENT_TOOL_OUTPUT, {
                    "error": str(e),
                    "error_code": e.error_code,
                })
```

4. 在 `Exception` 捕获的 `return ToolResult(...)` 之前添加：
```python
                span.add_event(EVENT_TOOL_OUTPUT, {
                    "error": truncate(str(e)),
                    "error_code": "INTERNAL_ERROR",
                })
```

完整修改后的 `execute()` 方法：

```python
    async def execute(self, call: ToolCall) -> ToolResult:
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("tool.execute") as span:
            span.add_event(EVENT_TOOL_INPUT, {
                "arguments": truncate(json.dumps(call.arguments, ensure_ascii=False)),
            })

            tool_def = self._tools.get(call.name)
            if not tool_def:
                span.set_attribute(TOOL_NAME, call.name)
                span.set_attribute(TOOL_STATUS, "error")
                span.set_attribute(TOOL_ERROR_CODE, "UNKNOWN_TOOL")
                span.add_event(EVENT_TOOL_OUTPUT, {
                    "error": f"Unknown tool: {call.name}",
                    "error_code": "UNKNOWN_TOOL",
                })
                return ToolResult(
                    tool_call_id=call.id,
                    status="error",
                    error=f"Unknown tool: {call.name}",
                    error_code="UNKNOWN_TOOL",
                    suggestion=f"Available tools: {', '.join(self._tools.keys())}",
                )

            try:
                data = await tool_def(**call.arguments)
                span.set_attribute(TOOL_NAME, call.name)
                span.set_attribute(TOOL_STATUS, "success")
                span.add_event(EVENT_TOOL_OUTPUT, {
                    "data": truncate(json.dumps(data, ensure_ascii=False)),
                })
                return ToolResult(
                    tool_call_id=call.id,
                    status="success",
                    data=data,
                )
            except ToolError as e:
                span.set_attribute(TOOL_NAME, call.name)
                span.set_attribute(TOOL_STATUS, "error")
                span.set_attribute(TOOL_ERROR_CODE, e.error_code)
                span.add_event(EVENT_TOOL_OUTPUT, {
                    "error": str(e),
                    "error_code": e.error_code,
                })
                return ToolResult(
                    tool_call_id=call.id,
                    status="error",
                    error=str(e),
                    error_code=e.error_code,
                    suggestion=e.suggestion,
                )
            except Exception as e:
                span.set_attribute(TOOL_NAME, call.name)
                span.set_attribute(TOOL_STATUS, "error")
                span.set_attribute(TOOL_ERROR_CODE, "INTERNAL_ERROR")
                span.record_exception(e)
                span.add_event(EVENT_TOOL_OUTPUT, {
                    "error": truncate(str(e)),
                    "error_code": "INTERNAL_ERROR",
                })
                return ToolResult(
                    tool_call_id=call.id,
                    status="error",
                    error=str(e),
                    error_code="INTERNAL_ERROR",
                    suggestion="An unexpected error occurred",
                )
```

- [ ] **Step 4: 修复测试中的变量引用 bug**

注意 `test_tool_input_event_truncated` 中有一个 bug：`events` 变量应该从 `span.events` 获取。修正为：

```python
async def test_tool_input_event_truncated(otel_exporter):
    engine = ToolEngine()

    async def my_tool(**kwargs):
        return {"ok": True}

    engine.register(ToolDef(
        name="test_tool", description="test", phases=[1], parameters={}, _fn=my_tool,
    ))

    long_arg = "x" * 1000
    call = ToolCall(id="t3", name="test_tool", arguments={"data": long_arg})
    await engine.execute(call)

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "tool.execute")
    events = span.events
    input_event = next(e for e in events if e.name == "tool.input")
    assert input_event.attributes["arguments"].endswith("...(truncated)")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_telemetry_tool_engine.py -v`
Expected: 6 passed（原 2 + 新 4）

- [ ] **Step 6: 提交**

```bash
git add backend/tools/engine.py backend/tests/test_telemetry_tool_engine.py
git commit -m "feat(telemetry): add tool.input and tool.output span events"
```

---

### Task 3: llm.chat span 添加 llm.request 和 llm.response event（OpenAI）

**Files:**
- Modify: `backend/llm/openai_provider.py`
- Modify: `backend/tests/test_telemetry_llm.py`

- [ ] **Step 1: 编写 OpenAI LLM event 的失败测试**

在 `backend/tests/test_telemetry_llm.py` 末尾追加：

```python
from telemetry.attributes import EVENT_LLM_REQUEST, EVENT_LLM_RESPONSE


async def test_openai_chat_has_request_event(otel_exporter):
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "hello"
    mock_choice.message.tool_calls = None
    mock_response.choices = [mock_choice]

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=mock_response)

        from llm.openai_provider import OpenAIProvider

        provider = OpenAIProvider(model="gpt-4o")
        messages = [Message(role=Role.USER, content="hello")]

        async for _ in provider.chat(messages, stream=False):
            pass

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "llm.chat")
    events = span.events

    req_event = next(e for e in events if e.name == EVENT_LLM_REQUEST)
    assert req_event.attributes["message_count"] == 1
    assert req_event.attributes["has_tools"] is False

    resp_event = next(e for e in events if e.name == EVENT_LLM_RESPONSE)
    assert "text_preview" in resp_event.attributes


async def test_openai_chat_request_event_with_tools(otel_exporter):
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "ok"
    mock_choice.message.tool_calls = None
    mock_response.choices = [mock_choice]

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=mock_response)

        from llm.openai_provider import OpenAIProvider

        provider = OpenAIProvider(model="gpt-4o")
        messages = [Message(role=Role.USER, content="hello")]
        tools = [{"name": "search", "description": "search", "parameters": {}}]

        async for _ in provider.chat(messages, tools=tools, stream=False):
            pass

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "llm.chat")
    req_event = next(e for e in span.events if e.name == EVENT_LLM_REQUEST)
    assert req_event.attributes["has_tools"] is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_telemetry_llm.py::test_openai_chat_has_request_event -v`
Expected: FAIL — `StopIteration`

- [ ] **Step 3: 在 openai_provider.py 中添加 event**

修改 `backend/llm/openai_provider.py`：

导入区追加：
```python
from telemetry.attributes import LLM_PROVIDER, LLM_MODEL, EVENT_LLM_REQUEST, EVENT_LLM_RESPONSE, truncate
```

在 `chat()` 方法中：

1. 在 `span.set_attribute(LLM_MODEL, self.model)` 之后添加 request event：
```python
            total_chars = sum(len(m.content or "") for m in messages)
            span.add_event(EVENT_LLM_REQUEST, {
                "message_count": len(messages),
                "total_chars": total_chars,
                "has_tools": tools is not None and len(tools) > 0,
            })
```

2. 在非流式分支的 `yield LLMChunk(type=ChunkType.DONE)` 之前添加 response event：
```python
                # 收集 response 信息
                text_preview = truncate(choice.message.content or "", max_len=200)
                tool_names = []
                if choice.message.tool_calls:
                    tool_names = [tc.function.name for tc in choice.message.tool_calls]
                span.add_event(EVENT_LLM_RESPONSE, {
                    "text_preview": text_preview,
                    "tool_calls": json.dumps(tool_names),
                })
```

3. 在流式分支的 `yield LLMChunk(type=ChunkType.DONE)` 之前添加 response event：
```python
                    # 收集流式 response 摘要
                    collected_text = ""  # 需要在流式循环外部累积
                    tool_call_names = [entry["name"] for entry in current_tool_calls.values()]
                    span.add_event(EVENT_LLM_RESPONSE, {
                        "text_preview": truncate(collected_text, max_len=200),
                        "tool_calls": json.dumps(tool_call_names),
                    })
```

注意：流式分支需要在循环中累积 `collected_text`。在 `current_tool_calls: dict[int, dict] = {}` 之后加 `collected_text = ""`，在 `if delta.content:` 分支中追加 `collected_text += delta.content`。

完整修改后的 `chat()` 方法流式部分关键代码：

```python
            response = await self.client.chat.completions.create(**kwargs)
            current_tool_calls: dict[int, dict] = {}
            collected_text = ""

            async for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                if delta.content:
                    collected_text += delta.content
                    yield LLMChunk(type=ChunkType.TEXT_DELTA, content=delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        # ... existing tool call accumulation ...
                        pass

                if chunk.choices[0].finish_reason:
                    for entry in current_tool_calls.values():
                        yield LLMChunk(
                            type=ChunkType.TOOL_CALL_START,
                            tool_call=ToolCall(
                                id=entry["id"],
                                name=entry["name"],
                                arguments=json.loads(entry["arguments"])
                                if entry["arguments"]
                                else {},
                            ),
                        )
                    tool_call_names = [entry["name"] for entry in current_tool_calls.values()]
                    span.add_event(EVENT_LLM_RESPONSE, {
                        "text_preview": truncate(collected_text, max_len=200),
                        "tool_calls": json.dumps(tool_call_names),
                    })
                    yield LLMChunk(type=ChunkType.DONE)
                    return
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_telemetry_llm.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add backend/llm/openai_provider.py backend/tests/test_telemetry_llm.py
git commit -m "feat(telemetry): add llm.request and llm.response span events to OpenAI provider"
```

---

### Task 4: llm.chat span 添加 llm.request 和 llm.response event（Anthropic）

**Files:**
- Modify: `backend/llm/anthropic_provider.py`
- Modify: `backend/tests/test_telemetry_llm.py`

- [ ] **Step 1: 编写 Anthropic LLM event 的失败测试**

在 `backend/tests/test_telemetry_llm.py` 末尾追加：

```python
async def test_anthropic_chat_has_request_event(otel_exporter):
    mock_response = MagicMock()
    mock_block = MagicMock()
    mock_block.type = "text"
    mock_block.text = "bonjour"
    mock_response.content = [mock_block]

    with patch("llm.anthropic_provider.AsyncAnthropic") as MockClient:
        instance = MockClient.return_value
        instance.messages.create = AsyncMock(return_value=mock_response)

        from llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(model="claude-sonnet-4-20250514")
        messages = [Message(role=Role.USER, content="hello")]

        async for _ in provider.chat(messages, stream=False):
            pass

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "llm.chat")
    events = span.events

    req_event = next(e for e in events if e.name == EVENT_LLM_REQUEST)
    assert req_event.attributes["message_count"] == 1
    assert req_event.attributes["has_tools"] is False

    resp_event = next(e for e in events if e.name == EVENT_LLM_RESPONSE)
    assert resp_event.attributes["text_preview"] == "bonjour"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_telemetry_llm.py::test_anthropic_chat_has_request_event -v`
Expected: FAIL — `StopIteration`

- [ ] **Step 3: 在 anthropic_provider.py 中添加 event**

修改 `backend/llm/anthropic_provider.py`：

导入区修改为：
```python
from telemetry.attributes import LLM_PROVIDER, LLM_MODEL, EVENT_LLM_REQUEST, EVENT_LLM_RESPONSE, truncate
```

在 `chat()` 方法中：

1. 在 `span.set_attribute(LLM_MODEL, self.model)` 之后添加 request event：
```python
            total_chars = sum(len(m.content or "") for m in messages)
            span.add_event(EVENT_LLM_REQUEST, {
                "message_count": len(messages),
                "total_chars": total_chars,
                "has_tools": tools is not None and len(tools) > 0,
            })
```

2. 在非流式分支的 `yield LLMChunk(type=ChunkType.DONE)` 之前添加：
```python
                collected_text = ""
                tool_names = []
                for block in response.content:
                    if block.type == "text":
                        collected_text += block.text
                    elif block.type == "tool_use":
                        tool_names.append(block.name)
                span.add_event(EVENT_LLM_RESPONSE, {
                    "text_preview": truncate(collected_text, max_len=200),
                    "tool_calls": json.dumps(tool_names),
                })
```

3. 在流式分支中，在 `async with self.client.messages.stream(...)` 块内添加 `collected_text = ""`，在文本 delta 处累积，在 `message_stop` event 的 `yield LLMChunk(type=ChunkType.DONE)` 之前添加：
```python
                        tool_call_names = []
                        # current_tool_name 在流式中按 block 处理，收集所有 tool 名
                        span.add_event(EVENT_LLM_RESPONSE, {
                            "text_preview": truncate(collected_text, max_len=200),
                            "tool_calls": json.dumps(tool_call_names),
                        })
```

完整修改后的流式部分关键代码：

```python
            async with self.client.messages.stream(**kwargs) as stream_resp:
                current_tool_id: str | None = None
                current_tool_name: str | None = None
                current_tool_json: str = ""
                collected_text = ""
                tool_call_names: list[str] = []

                async for event in stream_resp:
                    if event.type == "content_block_start":
                        if hasattr(event.content_block, "type"):
                            if event.content_block.type == "tool_use":
                                current_tool_id = event.content_block.id
                                current_tool_name = event.content_block.name
                                current_tool_json = ""
                    elif event.type == "content_block_delta":
                        if hasattr(event.delta, "text"):
                            collected_text += event.delta.text
                            yield LLMChunk(
                                type=ChunkType.TEXT_DELTA, content=event.delta.text
                            )
                        elif hasattr(event.delta, "partial_json"):
                            current_tool_json += event.delta.partial_json
                    elif event.type == "content_block_stop":
                        if current_tool_id and current_tool_name:
                            tool_call_names.append(current_tool_name)
                            yield LLMChunk(
                                type=ChunkType.TOOL_CALL_START,
                                tool_call=ToolCall(
                                    id=current_tool_id,
                                    name=current_tool_name,
                                    arguments=json.loads(current_tool_json)
                                    if current_tool_json
                                    else {},
                                ),
                            )
                            current_tool_id = None
                            current_tool_name = None
                    elif event.type == "message_stop":
                        span.add_event(EVENT_LLM_RESPONSE, {
                            "text_preview": truncate(collected_text, max_len=200),
                            "tool_calls": json.dumps(tool_call_names),
                        })
                        yield LLMChunk(type=ChunkType.DONE)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_telemetry_llm.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add backend/llm/anthropic_provider.py backend/tests/test_telemetry_llm.py
git commit -m "feat(telemetry): add llm.request and llm.response span events to Anthropic provider"
```

---

### Task 5: phase.transition span 添加 phase.plan_snapshot event

**Files:**
- Modify: `backend/phase/router.py`
- Modify: `backend/tests/test_telemetry_phase_context.py`

- [ ] **Step 1: 编写 phase snapshot event 的失败测试**

在 `backend/tests/test_telemetry_phase_context.py` 末尾追加：

```python
from state.models import DateRange
from telemetry.attributes import EVENT_PHASE_PLAN_SNAPSHOT


def test_phase_transition_has_plan_snapshot_event(otel_exporter):
    router = PhaseRouter()
    plan = TravelPlanState(
        session_id="s1",
        destination="Tokyo",
        dates=DateRange(start="2026-04-01", end="2026-04-05"),
    )

    changed = router.check_and_apply_transition(plan)
    assert changed

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "phase.transition")
    events = span.events
    snapshot = next(e for e in events if e.name == EVENT_PHASE_PLAN_SNAPSHOT)
    assert snapshot.attributes["destination"] == "Tokyo"
    assert snapshot.attributes["dates"] == "2026-04-01 ~ 2026-04-05"
    assert snapshot.attributes["daily_plans_count"] == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_telemetry_phase_context.py::test_phase_transition_has_plan_snapshot_event -v`
Expected: FAIL — `StopIteration`

- [ ] **Step 3: 在 router.py 中添加 event**

修改 `backend/phase/router.py`：

导入区追加：
```python
from telemetry.attributes import PHASE_FROM, PHASE_TO, EVENT_PHASE_PLAN_SNAPSHOT
```

在 `check_and_apply_transition()` 中，`span.set_attribute(PHASE_TO, inferred)` 之后添加：

```python
                span.add_event(EVENT_PHASE_PLAN_SNAPSHOT, {
                    "destination": plan.destination or "",
                    "dates": f"{plan.dates.start} ~ {plan.dates.end}" if plan.dates else "",
                    "daily_plans_count": len(plan.daily_plans),
                })
```

完整修改后的 `check_and_apply_transition()` 方法：

```python
    def check_and_apply_transition(self, plan: TravelPlanState) -> bool:
        """Check if plan_state warrants a phase change. Returns True if phase changed."""
        inferred = self.infer_phase(plan)
        if inferred != plan.phase:
            tracer = trace.get_tracer("travel-agent-pro")
            with tracer.start_as_current_span("phase.transition") as span:
                span.set_attribute(PHASE_FROM, plan.phase)
                span.set_attribute(PHASE_TO, inferred)
                span.add_event(EVENT_PHASE_PLAN_SNAPSHOT, {
                    "destination": plan.destination or "",
                    "dates": f"{plan.dates.start} ~ {plan.dates.end}" if plan.dates else "",
                    "daily_plans_count": len(plan.daily_plans),
                })
                plan.phase = inferred
            return True
        return False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_telemetry_phase_context.py -v`
Expected: 4 passed（原 3 + 新 1）

- [ ] **Step 5: 提交**

```bash
git add backend/phase/router.py backend/tests/test_telemetry_phase_context.py
git commit -m "feat(telemetry): add phase.plan_snapshot span event to phase transitions"
```

---

### Task 6: context.should_compress span 添加 context.compression event

**Files:**
- Modify: `backend/context/manager.py`
- Modify: `backend/tests/test_telemetry_phase_context.py`

- [ ] **Step 1: 编写 compression event 的失败测试**

在 `backend/tests/test_telemetry_phase_context.py` 末尾追加：

```python
from telemetry.attributes import EVENT_CONTEXT_COMPRESSION


def test_context_compression_event_when_triggered(otel_exporter):
    """压缩判定为 True 时，应添加 context.compression event。"""
    manager = ContextManager()
    messages = [
        Message(role=Role.USER, content="Hello " * 500),
        Message(role=Role.ASSISTANT, content="Response " * 300),
    ]
    max_tokens = 100  # 故意设小让 should_compress 返回 True

    result = manager.should_compress(messages, max_tokens)
    assert result is True

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "context.should_compress")
    events = span.events
    comp_event = next(e for e in events if e.name == EVENT_CONTEXT_COMPRESSION)
    assert comp_event.attributes["message_count"] == 2
    assert "estimated_tokens" in comp_event.attributes


def test_context_no_compression_event_when_not_triggered(otel_exporter):
    """压缩判定为 False 时，不应添加 context.compression event。"""
    manager = ContextManager()
    messages = [
        Message(role=Role.USER, content="Hi"),
    ]
    max_tokens = 100000

    result = manager.should_compress(messages, max_tokens)
    assert result is False

    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "context.should_compress")
    events = span.events
    compression_events = [e for e in events if e.name == "context.compression"]
    assert len(compression_events) == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_telemetry_phase_context.py::test_context_compression_event_when_triggered -v`
Expected: FAIL — `StopIteration`

- [ ] **Step 3: 在 manager.py 中添加 event**

修改 `backend/context/manager.py`：

导入区追加：
```python
from telemetry.attributes import CONTEXT_TOKENS_BEFORE, CONTEXT_TOKENS_AFTER, EVENT_CONTEXT_COMPRESSION
```

修改 `should_compress()` 方法，在 `result = estimated > max_tokens * 0.5` 之后、`return result` 之前添加：

```python
            if result:
                must_keep, _ = self.classify_messages(messages)
                span.add_event(EVENT_CONTEXT_COMPRESSION, {
                    "message_count": len(messages),
                    "estimated_tokens": estimated,
                    "must_keep_count": len(must_keep),
                })
```

完整修改后的 `should_compress()` 方法：

```python
    def should_compress(self, messages: list[Message], max_tokens: int) -> bool:
        tracer = trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("context.should_compress") as span:
            estimated = sum(len(m.content or "") // 3 for m in messages)
            span.set_attribute(CONTEXT_TOKENS_BEFORE, estimated)
            span.set_attribute("context.max_tokens", max_tokens)
            result = estimated > max_tokens * 0.5
            if result:
                must_keep, _ = self.classify_messages(messages)
                span.add_event(EVENT_CONTEXT_COMPRESSION, {
                    "message_count": len(messages),
                    "estimated_tokens": estimated,
                    "must_keep_count": len(must_keep),
                })
            return result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_telemetry_phase_context.py -v`
Expected: 6 passed（原 3 + Task5 的 1 + 新 2）

- [ ] **Step 5: 提交**

```bash
git add backend/context/manager.py backend/tests/test_telemetry_phase_context.py
git commit -m "feat(telemetry): add context.compression span event"
```

---

### Task 7: 全量测试验证

**Files:**
- 无新增文件

- [ ] **Step 1: 运行全部测试**

Run: `cd backend && python -m pytest tests/ -v`
Expected: 所有测试通过，包括原有 Phase A 测试和新增 Phase B 测试

- [ ] **Step 2: 确认无回归**

检查 Phase A 原有测试全部绿色，新增测试也全部绿色。

- [ ] **Step 3: 提交（如有格式修正）**

若需要微调则提交，否则跳过此步。
