from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent.execution.llm_turn import LlmTurnOutcome, run_llm_turn
from agent.hooks import HookManager
from agent.types import Message, Role, ToolCall
from llm.errors import LLMError, LLMErrorCode
from llm.types import ChunkType, LLMChunk
from run import IterationProgress


@dataclass
class _ToolDef:
    human_label: str


class _ToolEngine:
    def get_tool(self, name: str):
        if name == "search":
            return _ToolDef(human_label="搜索")
        return None


class _Plan:
    phase = 1
    phase3_step = None
    destination = None


class _Reflection:
    def check_and_inject(self, messages, plan, previous_step):
        return "reflection message"


class _ToolChoiceDecider:
    def decide(self, plan, messages, phase):
        return {"type": "tool", "name": "search"}


class _LLM:
    def __init__(self):
        self.kwargs = None

    async def chat(self, messages, **kwargs):
        self.kwargs = kwargs
        yield LLMChunk(
            type=ChunkType.PROVIDER_STATE_DELTA,
            provider_state={"reasoning_content": "thinking"},
        )
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hello")
        yield LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(id="tc1", name="search", arguments={}),
        )
        yield LLMChunk(type=ChunkType.USAGE, usage_info={"total_tokens": 3})
        yield LLMChunk(type=ChunkType.DONE)


@pytest.mark.asyncio
async def test_run_llm_turn_emits_status_reflection_and_collects_outcome():
    llm = _LLM()
    hooks = HookManager()
    messages = [Message(role=Role.USER, content="hi")]
    compression_events = [{"before": 10, "after": 5}]
    cancelled_checks = 0
    observed_progress: list[IterationProgress] = []

    def check_cancelled() -> None:
        nonlocal cancelled_checks
        cancelled_checks += 1

    chunks = []
    outcome = None
    async for item in run_llm_turn(
        llm=llm,
        tool_engine=_ToolEngine(),
        hooks=hooks,
        messages=messages,
        tools=[{"name": "search"}],
        current_phase=1,
        plan=_Plan(),
        reflection=_Reflection(),
        tool_choice_decider=_ToolChoiceDecider(),
        compression_events=compression_events,
        iteration_idx=0,
        previous_iteration_had_tools=False,
        phase_changed_in_previous_iteration=False,
        previous_phase3_step=None,
        check_cancelled=check_cancelled,
        update_progress=observed_progress.append,
    ):
        if isinstance(item, LLMChunk):
            chunks.append(item)
        else:
            outcome = item

    assert isinstance(outcome, LlmTurnOutcome)
    assert outcome.text_chunks == ["hello"]
    assert outcome.provider_state == {"reasoning_content": "thinking"}
    assert len(outcome.tool_calls) == 1
    assert outcome.tool_calls[0].human_label == "搜索"
    assert outcome.progress == IterationProgress.PARTIAL_TOOL_CALL
    assert observed_progress == [
        IterationProgress.PARTIAL_TEXT,
        IterationProgress.PARTIAL_TOOL_CALL,
    ]
    assert outcome.next_iteration_idx == 1
    assert outcome.previous_phase3_step is None
    assert compression_events == []
    assert messages[-1] == Message(role=Role.SYSTEM, content="reflection message")
    assert llm.kwargs == {
        "tools": [{"name": "search"}],
        "stream": True,
        "tool_choice": {"type": "tool", "name": "search"},
    }
    assert [chunk.type for chunk in chunks] == [
        ChunkType.INTERNAL_TASK,
        ChunkType.AGENT_STATUS,
        ChunkType.CONTEXT_COMPRESSION,
        ChunkType.INTERNAL_TASK,
        ChunkType.AGENT_STATUS,
        ChunkType.INTERNAL_TASK,
        ChunkType.TEXT_DELTA,
        ChunkType.TOOL_CALL_START,
        ChunkType.USAGE,
    ]
    assert cancelled_checks >= 4


@pytest.mark.asyncio
async def test_run_llm_turn_uses_summarizing_stage_after_tools():
    class _EmptyLLM:
        async def chat(self, messages, **kwargs):
            yield LLMChunk(type=ChunkType.DONE)

    chunks = []
    outcome = None
    async for item in run_llm_turn(
        llm=_EmptyLLM(),
        tool_engine=_ToolEngine(),
        hooks=HookManager(),
        messages=[Message(role=Role.USER, content="hi")],
        tools=[],
        current_phase=1,
        plan=None,
        reflection=None,
        tool_choice_decider=None,
        compression_events=[],
        iteration_idx=3,
        previous_iteration_had_tools=True,
        phase_changed_in_previous_iteration=False,
        previous_phase3_step=None,
        check_cancelled=lambda: None,
        update_progress=lambda progress: None,
    ):
        if isinstance(item, LLMChunk):
            chunks.append(item)
        else:
            outcome = item

    assert isinstance(outcome, LlmTurnOutcome)
    status_chunks = [chunk for chunk in chunks if chunk.type == ChunkType.AGENT_STATUS]
    assert status_chunks[0].agent_status["stage"] == "summarizing"
    assert status_chunks[0].agent_status["iteration"] == 3
    assert outcome.next_iteration_idx == 4


@pytest.mark.asyncio
async def test_run_llm_turn_updates_progress_before_stream_error():
    class _FailingLLM:
        async def chat(self, messages, **kwargs):
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="partial")
            raise LLMError(
                code=LLMErrorCode.TRANSIENT,
                message="stream failed",
                retryable=True,
                provider="test",
                model="fake",
                failure_phase="streaming",
            )

    observed_progress: list[IterationProgress] = []

    with pytest.raises(LLMError):
        async for _item in run_llm_turn(
            llm=_FailingLLM(),
            tool_engine=_ToolEngine(),
            hooks=HookManager(),
            messages=[Message(role=Role.USER, content="hi")],
            tools=[],
            current_phase=1,
            plan=None,
            reflection=None,
            tool_choice_decider=None,
            compression_events=[],
            iteration_idx=0,
            previous_iteration_had_tools=False,
            phase_changed_in_previous_iteration=False,
            previous_phase3_step=None,
            check_cancelled=lambda: None,
            update_progress=observed_progress.append,
        ):
            pass

    assert observed_progress == [IterationProgress.PARTIAL_TEXT]
