# 并行 tool_calls 场景下 system 消息错位注入修复 · 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `on_validate` 回调在并行 tool_calls 之间 append `[实时约束检查]` system 消息导致的 OpenAI 协议违规，使后端发往 LLM 的消息序列永远满足 "assistant.tool_calls 后连续紧跟全部 tool 答复"。

**Architecture:** 新增两个模块级小工具 `push_pending_system_note` / `flush_pending_system_notes`，把 `on_validate` 里直接 `session["messages"].append(SYSTEM)` 改为写入 session 的 `_pending_system_notes` 缓冲区；在 `on_before_llm` 函数开头统一 flush 到 `msgs` 末尾。Flush 点是唯一合法的单一注入时机，保证协议原子性。

**Tech Stack:** Python 3.12 / FastAPI / pytest / httpx.AsyncClient（集成测试沿用 `test_realtime_validation_hook.py` 风格）。

**Spec:** `docs/superpowers/specs/2026-04-15-parallel-toolcall-system-injection-design.md`

---

## 文件结构

| 文件 | 操作 | 说明 |
|---|---|---|
| `backend/main.py` | 修改 | 加两个模块级 helper；改 `on_validate`；在 `on_before_llm` 加 flush；session 初始化两处加字段 |
| `backend/tests/test_pending_system_notes.py` | 新建 | 对两个 helper 的纯单元测试 |
| `backend/tests/test_parallel_tool_call_sequence.py` | 新建 | 沿用 httpx 风格的集成测试，覆盖并行 tool_calls 场景 |
| `backend/tests/test_realtime_validation_hook.py` | 修改 | 调整 `[实时约束检查]` 断言时机（errors 触发时 messages 暂不含，flush 后才含） |
| `PROJECT_OVERVIEW.md` | 修改 | 记录本次修复（commit 同步要求） |
| `docs/TODO.md` | 无需改（上一轮已加第 2 条）|

---

## Task 1: 新增 pending system notes 两个 helper（TDD）

**Files:**
- Create: `backend/tests/test_pending_system_notes.py`
- Modify: `backend/main.py`（加模块级 helper，位置：`app = FastAPI(...)` 语句之前的合适位置；搜 `def create_app` 上方）

### Step 1.1: 写失败测试

- [ ] **Step 1.1: 新建单元测试**

创建 `backend/tests/test_pending_system_notes.py`：

```python
"""Unit tests for pending system notes helpers.

These helpers exist so that system messages triggered during tool
execution (e.g. `[实时约束检查]`) don't get appended to `session["messages"]`
in the middle of a parallel tool_calls sequence. They're buffered and
flushed exactly once, just before the next LLM call.
"""

import pytest

from agent.types import Message, Role
from main import flush_pending_system_notes, push_pending_system_note


def _new_session() -> dict:
    return {"messages": []}


def test_push_initializes_buffer_when_missing():
    session = _new_session()
    push_pending_system_note(session, "hello")
    assert session["_pending_system_notes"] == ["hello"]


def test_push_appends_in_order():
    session = _new_session()
    push_pending_system_note(session, "first")
    push_pending_system_note(session, "second")
    assert session["_pending_system_notes"] == ["first", "second"]


def test_push_does_not_touch_messages():
    session = _new_session()
    push_pending_system_note(session, "hello")
    assert session["messages"] == []


def test_flush_appends_each_note_as_system_message():
    session = {"messages": [], "_pending_system_notes": ["a", "b"]}
    msgs: list[Message] = []
    count = flush_pending_system_notes(session, msgs)
    assert count == 2
    assert [m.role for m in msgs] == [Role.SYSTEM, Role.SYSTEM]
    assert [m.content for m in msgs] == ["a", "b"]


def test_flush_clears_buffer():
    session = {"messages": [], "_pending_system_notes": ["a"]}
    flush_pending_system_notes(session, [])
    assert session["_pending_system_notes"] == []


def test_flush_is_noop_when_buffer_empty():
    session = {"messages": [], "_pending_system_notes": []}
    msgs: list[Message] = []
    count = flush_pending_system_notes(session, msgs)
    assert count == 0
    assert msgs == []


def test_flush_is_noop_when_buffer_missing():
    session = {"messages": []}
    msgs: list[Message] = []
    count = flush_pending_system_notes(session, msgs)
    assert count == 0
    assert msgs == []


def test_flush_does_not_touch_existing_messages():
    existing = Message(role=Role.USER, content="hi")
    msgs = [existing]
    session = {"messages": [], "_pending_system_notes": ["note"]}
    flush_pending_system_notes(session, msgs)
    assert msgs[0] is existing
    assert msgs[1].role == Role.SYSTEM
    assert msgs[1].content == "note"
```

- [ ] **Step 1.2: 运行测试，确认失败**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && \
  source .venv/bin/activate && \
  python -m pytest tests/test_pending_system_notes.py -v
```

Expected: 全部失败，错误 `ImportError: cannot import name 'flush_pending_system_notes' from 'main'` 或类似。

- [ ] **Step 1.3: 在 `backend/main.py` 模块级加两个 helper**

加在 `def create_app(...)` 函数之前（建议在文件顶部 import 块之后、任何 app 定义之前）。若文件已有模块级工具区，紧邻已有工具添加：

```python
def push_pending_system_note(session: dict, content: str) -> None:
    """Buffer a system note to be flushed into messages before next LLM call.

    Writing to session["messages"] during tool execution risks inserting
    a system message between an assistant.tool_calls and its tool responses,
    which breaks OpenAI protocol. Use this helper instead; flush at on_before_llm.
    """
    session.setdefault("_pending_system_notes", []).append(content)


def flush_pending_system_notes(session: dict, msgs: list) -> int:
    """Flush buffered notes into msgs as SYSTEM messages. Returns count flushed."""
    from agent.types import Message, Role

    pending = session.get("_pending_system_notes") or []
    if not pending:
        return 0
    for content in pending:
        msgs.append(Message(role=Role.SYSTEM, content=content))
    session["_pending_system_notes"] = []
    return len(pending)
```

- [ ] **Step 1.4: 重新运行测试，确认通过**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && \
  source .venv/bin/activate && \
  python -m pytest tests/test_pending_system_notes.py -v
```

Expected: 8 passed

- [ ] **Step 1.5: 提交**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && \
  git add backend/main.py backend/tests/test_pending_system_notes.py && \
  git commit -m "feat(backend): add pending system notes buffer helpers

push_pending_system_note + flush_pending_system_notes decouple
'tool execution callbacks that want to inject system messages' from
'the live messages list', so we never break a parallel tool_calls
sequence with a stray SYSTEM in the middle.

Flush point will be wired into on_before_llm in a follow-up commit."
```

---

## Task 2: 把 `on_validate` 改为写缓冲区

**Files:**
- Modify: `backend/main.py:459-468`

### Step 2.1: 改 on_validate

- [ ] **Step 2.1: 替换直接 append 为 push_pending_system_note**

在 `backend/main.py` 中找到：

```python
                if errors:
                    if session:
                        session["_pending_validation_errors"] = errors
                        session["messages"].append(
                            Message(
                                role=Role.SYSTEM,
                                content="[实时约束检查]\n"
                                + "\n".join(f"- {error}" for error in errors),
                            )
                        )
```

改为：

```python
                if errors:
                    if session:
                        session["_pending_validation_errors"] = errors
                        push_pending_system_note(
                            session,
                            "[实时约束检查]\n"
                            + "\n".join(f"- {error}" for error in errors),
                        )
```

- [ ] **Step 2.2: 暂不提交；下一步同时改 on_before_llm 后一起测试**

---

## Task 3: 在 `on_before_llm` 加 flush 点

**Files:**
- Modify: `backend/main.py:470-475`

### Step 3.1: 改 on_before_llm

- [ ] **Step 3.1: 在 on_before_llm 函数开头插入 flush**

在 `backend/main.py` 中找到：

```python
        async def on_before_llm(**kwargs):
            msgs = kwargs.get("messages")
            tools = kwargs.get("tools") or []
            phase = kwargs.get("phase", plan.phase)
            if not msgs:
                return
```

改为：

```python
        async def on_before_llm(**kwargs):
            msgs = kwargs.get("messages")
            tools = kwargs.get("tools") or []
            phase = kwargs.get("phase", plan.phase)
            if not msgs:
                return
            session = sessions.get(plan.session_id)
            if session:
                flush_pending_system_notes(session, msgs)
```

说明：`sessions` 和 `plan` 在 `_build_agent` 闭包中均可访问（同一处已有 `session = sessions.get(plan.session_id)` 先例，如 `on_validate` 内）。

- [ ] **Step 3.2: 新建 session 初始化处加 `_pending_system_notes` 字段**

位置：`backend/main.py:1215-1223`

找到：

```python
        sessions[plan.session_id] = {
            "plan": plan,
            "messages": [],
            "agent": agent,
            "needs_rebuild": False,
            "user_id": "default_user",
            "compression_events": compression_events,
            "stats": SessionStats(),
        }
```

改为：

```python
        sessions[plan.session_id] = {
            "plan": plan,
            "messages": [],
            "agent": agent,
            "needs_rebuild": False,
            "user_id": "default_user",
            "compression_events": compression_events,
            "stats": SessionStats(),
            "_pending_system_notes": [],
        }
```

- [ ] **Step 3.3: 恢复 session 初始化处加 `_pending_system_notes` 字段**

位置：`backend/main.py:1193-1201`

找到：

```python
        return {
            "plan": plan,
            "messages": restored_messages,
            "agent": agent,
            "needs_rebuild": False,
            "user_id": meta["user_id"],
            "compression_events": compression_events,
            "stats": SessionStats(),
        }
```

改为：

```python
        return {
            "plan": plan,
            "messages": restored_messages,
            "agent": agent,
            "needs_rebuild": False,
            "user_id": meta["user_id"],
            "compression_events": compression_events,
            "stats": SessionStats(),
            "_pending_system_notes": [],
        }
```

（注意：`push_pending_system_note` 已用 `setdefault` 兜底，忘记初始化也不会崩；但显式初始化更清晰、便于调试日志和 session 字段审计。）

- [ ] **Step 3.4: 运行现有测试，快速确认没有破坏基本路径**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && \
  source .venv/bin/activate && \
  python -m pytest tests/test_pending_system_notes.py tests/test_agent_loop.py -v
```

Expected: `test_pending_system_notes.py` 8 passed；`test_agent_loop.py` 全部通过（或至少不因本次改动新增失败）。

- [ ] **Step 3.5: 提交**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && \
  git add backend/main.py && \
  git commit -m "fix(backend): route [实时约束检查] through pending buffer

on_validate used to session['messages'].append(SYSTEM) immediately
when update_plan_state hit a constraint, which could land between
two tool responses of the same parallel tool_calls batch — that
breaks OpenAI protocol and made Xunfei return 400 with
'Messages with role tool must be a response to a preceding message
with tool_calls'.

Now on_validate pushes into session['_pending_system_notes'] and
on_before_llm flushes the buffer to msgs just before invoking LLM,
guaranteeing every SYSTEM note lands after the full tool group.

Session init sites (new + restored) declare the field explicitly."
```

---

## Task 4: 调整现有 `test_realtime_validation_hook.py` 断言

**Files:**
- Modify: `backend/tests/test_realtime_validation_hook.py`

### Step 4.1: 更新断言

- [ ] **Step 4.1: 先看一下当前断言**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && \
  sed -n '95,115p' tests/test_realtime_validation_hook.py
```

Expected 看到：

```python
    assert resp.status_code == 200
    realtime_messages = [
        message.content
        for message in session["messages"]
        if message.role.value == "system" and message.content
    ]
    assert any("[实时约束检查]" in content for content in realtime_messages)
    assert any("时间冲突" in content for content in realtime_messages)
```

- [ ] **Step 4.2: 运行这个测试，确认现在它会失败**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && \
  source .venv/bin/activate && \
  python -m pytest tests/test_realtime_validation_hook.py -v
```

Expected: 至少一个用例（`test_realtime_validation_hook_appends_feedback` 或同名）失败，原因是 `session["messages"]` 里找不到 `[实时约束检查]`（现在它缓冲在 `_pending_system_notes`）。

若测试仍通过，说明在该集成路径里已经触发了下一轮 `on_before_llm`，已经被 flush 到了 `messages` —— 这种情况下测试不需要改，跳到 Step 4.5。

- [ ] **Step 4.3: 如 4.2 失败，调整断言**

把 `session["messages"]` 的 system 检查改成「messages 或 pending 任一处包含」，反映 "约束检查至少已被登记下来" 的语义：

把：

```python
    realtime_messages = [
        message.content
        for message in session["messages"]
        if message.role.value == "system" and message.content
    ]
    assert any("[实时约束检查]" in content for content in realtime_messages)
    assert any("时间冲突" in content for content in realtime_messages)
```

改为：

```python
    realtime_messages = [
        message.content
        for message in session["messages"]
        if message.role.value == "system" and message.content
    ] + list(session.get("_pending_system_notes") or [])
    assert any("[实时约束检查]" in content for content in realtime_messages)
    assert any("时间冲突" in content for content in realtime_messages)
```

- [ ] **Step 4.4: 再跑一遍，确认通过**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && \
  source .venv/bin/activate && \
  python -m pytest tests/test_realtime_validation_hook.py -v
```

Expected: all passed.

- [ ] **Step 4.5: 提交（如有改动）**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && \
  git add backend/tests/test_realtime_validation_hook.py && \
  git commit -m "test(backend): update [实时约束检查] assertion for pending buffer

Reflects the new flush semantics — the note may live in
session['_pending_system_notes'] until the next on_before_llm
hook fires it into session['messages']. Test now accepts either
location."
```

---

## Task 5: 集成测试 — 并行 tool_calls 场景

**Files:**
- Create: `backend/tests/test_parallel_tool_call_sequence.py`

本测试沿用 `test_realtime_validation_hook.py` 的 `httpx.AsyncClient` 风格，mock LLM 返回并行 `tool_calls`，驱动真实 `on_validate` + `on_before_llm`，然后断言发往 LLM 的 messages 序列合法。

### Step 5.1-5.3: 场景用例

- [ ] **Step 5.1: 新建测试文件**

创建 `backend/tests/test_parallel_tool_call_sequence.py`：

```python
"""Integration tests: parallel tool_calls must not be split by SYSTEM injects.

Regression for the 2026-04-15 Hong Kong session bug where on_validate
appended [实时约束检查] between the 1st and 2nd tool responses of a
parallel update_plan_state batch, causing Xunfei gateway to return 400.
"""

import pytest
import httpx

from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from state.plan import TravelPlanState


def _assert_toolcalls_block_is_contiguous(msgs: list[Message]) -> None:
    """Protocol check: every assistant.tool_calls must be followed by
    exactly len(tool_calls) consecutive role=tool messages, no other
    role in between."""
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.role == Role.ASSISTANT and m.tool_calls:
            expected = len(m.tool_calls)
            for k in range(1, expected + 1):
                assert i + k < len(msgs), (
                    f"tool_calls group at msg {i} truncated: expected "
                    f"{expected} tool responses, got {len(msgs) - i - 1}"
                )
                follow = msgs[i + k]
                assert follow.role == Role.TOOL, (
                    f"tool_calls group at msg {i}: position {k} must be "
                    f"role=TOOL, got role={follow.role.value}; this is the "
                    f"bug we're preventing."
                )
            i += expected + 1
        else:
            i += 1


@pytest.mark.asyncio
async def test_parallel_update_plan_state_with_constraints_flushes_after_group(
    app, sessions
):
    """When LLM issues 3 parallel update_plan_state that trigger constraint
    errors, the [实时约束检查] SYSTEM message must appear AFTER the full
    tool group, never between tool responses."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        plan.phase = 3
        plan.destination = "香港"

        agent = session["agent"]

        captured_messages: list[list[Message]] = []
        call_count = 0

        async def fake_chat(messages, tools=None, stream=True, **kw):
            nonlocal call_count
            captured_messages.append([m for m in messages])
            call_count += 1
            if call_count == 1:
                # Return 3 parallel update_plan_state tool_calls
                for i, (field, value) in enumerate([
                    ("dates", {"start": "2026-05-06", "end": "2026-05-07"}),
                    ("travelers", 1),
                    ("constraints", ["住深圳"]),
                ]):
                    yield LLMChunk(
                        type=ChunkType.TOOL_CALL,
                        tool_call=ToolCall(
                            id=f"call_{i}",
                            name="update_plan_state",
                            arguments={"field": field, "value": value},
                        ),
                    )
                yield LLMChunk(type=ChunkType.DONE)
            else:
                # Second call: finish with plain text so we stop looping
                yield LLMChunk(type=ChunkType.TEXT, text="done")
                yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "去香港玩"},
        )

    assert resp.status_code == 200
    assert call_count >= 2, (
        "expected at least a second LLM call to trigger flush; "
        f"got {call_count}"
    )

    # The second call is what flush affects; inspect its messages.
    second_call_msgs = captured_messages[1]
    _assert_toolcalls_block_is_contiguous(second_call_msgs)

    # The constraint note should be somewhere in the second-call messages,
    # strictly AFTER the tool group it was triggered by.
    system_contents = [
        (i, m.content)
        for i, m in enumerate(second_call_msgs)
        if m.role == Role.SYSTEM and m.content and "[实时约束检查]" in m.content
    ]
    assert system_contents, (
        "expected [实时约束检查] to be flushed into second-call messages"
    )


@pytest.mark.asyncio
async def test_parallel_tool_calls_without_constraints_have_no_inject(
    app, sessions
):
    """If no constraint errors, messages should contain the tool group
    contiguously and no [实时约束检查] SYSTEM."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        plan.phase = 1   # phase 1 won't trigger most constraint checks

        agent = session["agent"]
        captured_messages: list[list[Message]] = []
        call_count = 0

        async def fake_chat(messages, tools=None, stream=True, **kw):
            nonlocal call_count
            captured_messages.append([m for m in messages])
            call_count += 1
            if call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL,
                    tool_call=ToolCall(
                        id="call_only",
                        name="update_plan_state",
                        arguments={"field": "destination", "value": "东京"},
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
            else:
                yield LLMChunk(type=ChunkType.TEXT, text="done")
                yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "去东京"},
        )

    assert resp.status_code == 200
    assert call_count >= 2

    second_call_msgs = captured_messages[1]
    _assert_toolcalls_block_is_contiguous(second_call_msgs)

    system_has_realtime = any(
        m.role == Role.SYSTEM and m.content and "[实时约束检查]" in m.content
        for m in second_call_msgs
    )
    assert not system_has_realtime, (
        "unexpected [实时约束检查] SYSTEM when no constraint violated"
    )


@pytest.mark.asyncio
async def test_pending_buffer_cleared_between_rounds(app, sessions):
    """After flush, the buffer is empty and the next round accumulates afresh."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]
        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        plan.phase = 3
        plan.destination = "香港"

        agent = session["agent"]
        call_count = 0

        async def fake_chat(messages, tools=None, stream=True, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL,
                    tool_call=ToolCall(
                        id="c1",
                        name="update_plan_state",
                        arguments={
                            "field": "dates",
                            "value": {"start": "2026-05-06", "end": "2026-05-07"},
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
            else:
                yield LLMChunk(type=ChunkType.TEXT, text="done")
                yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        await client.post(
            f"/api/chat/{session_id}",
            json={"message": "去香港"},
        )

    # After the chat turn, buffer must be empty (flushed on second LLM call).
    assert session.get("_pending_system_notes") == []
```

- [ ] **Step 5.2: 运行测试**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && \
  source .venv/bin/activate && \
  python -m pytest tests/test_parallel_tool_call_sequence.py -v
```

Expected: 3 passed.

注意：如果测试 import 了 `app` / `sessions` fixture，需与现有 conftest 对齐。若出现 fixture 未定义，检查 `backend/tests/conftest.py`，沿用 `test_realtime_validation_hook.py` 的 import 和 fixture 方式——它们位于同一目录。

- [ ] **Step 5.3: 提交**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && \
  git add backend/tests/test_parallel_tool_call_sequence.py && \
  git commit -m "test(backend): integration coverage for parallel tool_calls SYSTEM order

Three scenarios pinning down the invariant 'tool_calls group is never
split by SYSTEM inject' and 'pending buffer is cleared between rounds'."
```

---

## Task 6: 同步 PROJECT_OVERVIEW.md 并人工验证

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

### Step 6.1-6.3

- [ ] **Step 6.1: 更新 PROJECT_OVERVIEW.md**

在 `PROJECT_OVERVIEW.md` 合适章节（建议在描述 `on_before_llm` / agent loop / session 字段的位置）增补一小段：

```markdown
### pending system notes 缓冲区

Session 字典持有 `_pending_system_notes: list[str]`，用于缓存在工具执行阶段
产生、但**不应**立即 append 到 `session["messages"]` 的 SYSTEM 消息（典型如
实时约束检查 `[实时约束检查]`）。

- 写入点：`on_validate` 等工具执行回调，经 `push_pending_system_note(session, content)` 追加。
- 消费点：**唯一**的 flush 发生在 `on_before_llm` 开头，经 `flush_pending_system_notes(session, msgs)` 按序 append 到 msgs 末尾并清空缓冲区。
- 目的：保证 `assistant.tool_calls → 全部 tool 答复` 的协议序列原子性；并行 tool_calls 期间任何 SYSTEM 都只会落在整组 tool 之后、下一次 assistant 之前。
- 不落盘：session 重载后重置为 `[]`，未 flush 的提醒丢失（提醒本身是状态派生物，无需持久化）。
```

- [ ] **Step 6.2: 人工验证**

重启 dev（`npm run dev:all`），前端开一个新会话，按类似 "香港两日游 / 住深圳，五一后去，一个人" 的对话让模型一次性并行调用多个 `update_plan_state`（可观察 thinking bubble / 工具调用 chip）。Expected：

- 不再出现 "本轮生成未完成 / 连接阶段：模型返回格式异常" 的错误提示
- 约束提醒（如有）仍然在下一轮 LLM 回复中得到体现，语义不丢失

记录验证结果（通过/失败）。

- [ ] **Step 6.3: 提交**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && \
  git add PROJECT_OVERVIEW.md && \
  git commit -m "docs: document pending system notes buffer in PROJECT_OVERVIEW"
```

---

## 验收清单

- [ ] Task 1：helper 单元测试 8 passed
- [ ] Task 2：`on_validate` 已改写
- [ ] Task 3：`on_before_llm` 已接 flush，两处 session 初始化加字段
- [ ] Task 4：`test_realtime_validation_hook.py` 绿
- [ ] Task 5：`test_parallel_tool_call_sequence.py` 3 passed
- [ ] Task 6：PROJECT_OVERVIEW 更新 + 人工跑一遍香港场景通过
- [ ] 全部 commit 完成，`git status` 清洁

## 不变量（修复后必须成立）

1. 任何发往 LLM 的 `messages` 中，`assistant.tool_calls` 后面紧跟的必然是其全部 `tool` 答复（顺序对应），中间无其它 role。
2. `_pending_system_notes` 只在 `on_before_llm` 开头被消费；工具执行阶段只写不读。
3. `session["messages"]` 在工具执行阶段只被主循环追加 `tool` 消息，不再被回调追加 `system` 消息。
