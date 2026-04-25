import pytest

from agent.phase5.day_worker import run_day_worker
from agent.phase5.worker_prompt import DayTask
from agent.types import ToolCall
from llm.types import ChunkType, LLMChunk
from state.models import TravelPlanState, DateRange


def _stub_plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="s-dw")
    plan.phase = 5
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.selected_skeleton_id = "x"
    plan.skeleton_plans = [{"id": "x", "days": [{"area": "A", "theme": "T"}]}]
    return plan


def _task() -> DayTask:
    return DayTask(
        day=1,
        date="2026-05-01",
        skeleton_slice={"area": "A", "theme": "T"},
        pace="balanced",
    )


class _LLMStub:
    def __init__(self, chunk_batches):
        self._batches = list(chunk_batches)

    async def chat(self, messages, tools=None, stream=True):
        batch = self._batches.pop(0)
        for c in batch:
            yield c


class _ToolEngineStub:
    _LABELS = {"get_poi_info": "查询 POI", "calculate_route": "规划路线"}

    def get_tool(self, name):
        if name not in self._LABELS:
            return None
        label = self._LABELS[name]
        tool_name = name

        class _T:
            human_label = label

            def to_schema(self):
                return {"name": tool_name}

        return _T()

    async def execute_batch(self, tcs):
        from agent.types import ToolResult
        return [
            ToolResult(tool_call_id=tc.id, status="success", data={})
            for tc in tcs
        ]


@pytest.mark.asyncio
async def test_worker_emits_iter_start_each_iteration():
    # Two iterations: first yields a tool call (→ second iteration),
    # second yields a final JSON text.
    batch_1 = [
        LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(id="t1", name="get_poi_info", arguments={}),
        ),
        LLMChunk(type=ChunkType.DONE),
    ]
    batch_2 = [
        LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content='```json\n{"day": 1, "activities": []}\n```',
        ),
        LLMChunk(type=ChunkType.DONE),
    ]
    llm = _LLMStub([batch_1, batch_2])
    events: list[tuple[int, str, dict]] = []

    await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        max_iterations=5,
        timeout_seconds=5,
        on_progress=lambda day, kind, payload: events.append((day, kind, payload)),
    )

    iter_events = [e for e in events if e[1] == "iter_start"]
    assert len(iter_events) == 2
    assert iter_events[0][2] == {"iteration": 1, "max": 5}
    assert iter_events[1][2] == {"iteration": 2, "max": 5}


@pytest.mark.asyncio
async def test_worker_emits_tool_start_before_execute():
    batch_1 = [
        LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(id="t1", name="get_poi_info", arguments={}),
        ),
        LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(id="t2", name="calculate_route", arguments={}),
        ),
        LLMChunk(type=ChunkType.DONE),
    ]
    batch_2 = [
        LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content='```json\n{"day": 1, "activities": []}\n```',
        ),
        LLMChunk(type=ChunkType.DONE),
    ]
    llm = _LLMStub([batch_1, batch_2])
    events: list[tuple[int, str, dict]] = []

    await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        max_iterations=5,
        timeout_seconds=5,
        on_progress=lambda day, kind, payload: events.append((day, kind, payload)),
    )

    tool_events = [e for e in events if e[1] == "tool_start"]
    # Exactly one tool_start per batch (the first tool call).
    assert len(tool_events) == 1
    assert tool_events[0][2]["tool"] == "get_poi_info"
    assert tool_events[0][2]["human_label"] == "查询 POI"


@pytest.mark.asyncio
async def test_worker_progress_callback_exception_does_not_kill_worker():
    batch_1 = [
        LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content='```json\n{"day": 1, "activities": []}\n```',
        ),
        LLMChunk(type=ChunkType.DONE),
    ]
    llm = _LLMStub([batch_1])

    def boom(day, kind, payload):
        raise ValueError("intentional")

    result = await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        max_iterations=5,
        timeout_seconds=5,
        on_progress=boom,
    )
    assert result.success is True
    assert result.dayplan == {"day": 1, "activities": []}
