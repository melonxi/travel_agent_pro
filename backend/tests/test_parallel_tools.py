# backend/tests/test_parallel_tools.py
import asyncio
import pytest

from agent.execution.tool_batches import ToolBatchOutcome, execute_tool_batch
from agent.execution.tool_invocation import SearchHistoryTracker
from agent.hooks import HookManager
from agent.internal_tasks import InternalTask
from agent.types import Message, Role
from agent.types import ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from run import IterationProgress
from telemetry.trace_recorder import TraceContext, TraceRecorder
from state.models import DateRange, DayPlan, TravelPlanState
from tools.base import ToolDef, ToolError
from tools.engine import ToolEngine
from tools.plan_tools.backtrack import make_request_backtrack_tool


async def _slow_read(**kwargs):
    await asyncio.sleep(0.05)
    return {"query": kwargs.get("q", "")}


async def _write(**kwargs):
    return {"written": kwargs.get("field", "")}


def _make_engine() -> ToolEngine:
    engine = ToolEngine()
    engine.register(ToolDef(
        name="search_a", description="", phases=[1], parameters={},
        _fn=_slow_read, side_effect="read",
    ))
    engine.register(ToolDef(
        name="search_b", description="", phases=[1], parameters={},
        _fn=_slow_read, side_effect="read",
    ))
    engine.register(ToolDef(
        name="update_state", description="", phases=[1], parameters={},
        _fn=_write, side_effect="write",
    ))
    return engine


class _TraceStore:
    def __init__(self):
        self.events = []
        self.artifacts = []

    async def append_event(self, event):
        self.events.append(event)

    async def save_artifact_metadata(self, metadata):
        self.artifacts.append(metadata)


async def _empty_after_tool_result_hook(**kwargs):
    if False:
        yield LLMChunk(type="keepalive")


async def _soft_judge_after_tool_result_hook(**kwargs):
    yield LLMChunk(
        type=ChunkType.INTERNAL_TASK,
        internal_task=InternalTask(
            id="soft_judge:call-1",
            kind="soft_judge",
            label="judge",
            status="warning",
            message="score low",
            blocking=False,
            related_tool_call_id="call-1",
            result={"overall": 2.0, "suggestions_count": 1},
        ),
    )


@pytest.mark.asyncio
async def test_execute_batch_returns_empty_list_for_no_calls():
    engine = _make_engine()
    assert await engine.execute_batch([]) == []


@pytest.mark.asyncio
async def test_execute_batch_returns_results_in_original_order():
    engine = _make_engine()
    calls = [
        ToolCall(id="1", name="search_a", arguments={"q": "a"}),
        ToolCall(id="2", name="update_state", arguments={"field": "x"}),
        ToolCall(id="3", name="search_b", arguments={"q": "b"}),
    ]
    results = await engine.execute_batch(calls)
    assert len(results) == 3
    assert results[0].tool_call_id == "1"
    assert results[1].tool_call_id == "2"
    assert results[2].tool_call_id == "3"


@pytest.mark.asyncio
async def test_execute_batch_reads_run_in_parallel():
    """Two 50ms reads should complete in ~50ms total, not ~100ms."""
    engine = _make_engine()
    calls = [
        ToolCall(id="1", name="search_a", arguments={"q": "a"}),
        ToolCall(id="2", name="search_b", arguments={"q": "b"}),
    ]
    start = asyncio.get_event_loop().time()
    results = await engine.execute_batch(calls)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.09  # should be ~50ms, not ~100ms
    assert all(r.status == "success" for r in results)


@pytest.mark.asyncio
async def test_execute_batch_writes_after_reads():
    read_finished = asyncio.Event()
    write_started = asyncio.Event()
    write_finished = asyncio.Event()

    async def blocking_read(**kwargs):
        await asyncio.sleep(0.05)
        read_finished.set()
        return {}

    async def tracked_write(**kwargs):
        write_started.set()
        await asyncio.sleep(0)
        write_finished.set()
        return {}

    engine = ToolEngine()
    engine.register(ToolDef(
        name="search_a", description="", phases=[1], parameters={},
        _fn=blocking_read, side_effect="read",
    ))
    engine.register(ToolDef(
        name="update_state", description="", phases=[1], parameters={},
        _fn=tracked_write, side_effect="write",
    ))
    calls = [
        ToolCall(id="1", name="search_a", arguments={"q": "a"}),
        ToolCall(id="2", name="update_state", arguments={"field": "x"}),
    ]
    batch_task = asyncio.create_task(engine.execute_batch(calls))
    await asyncio.wait_for(read_finished.wait(), timeout=0.2)
    assert not write_started.is_set()
    await batch_task
    assert write_started.is_set()
    assert write_finished.is_set()


@pytest.mark.asyncio
async def test_execute_batch_single_tool_works():
    engine = _make_engine()
    calls = [ToolCall(id="1", name="search_a", arguments={"q": "a"})]
    results = await engine.execute_batch(calls)
    assert len(results) == 1
    assert results[0].status == "success"


@pytest.mark.asyncio
async def test_execute_batch_read_failure_does_not_block_others():
    async def failing_read(**kwargs):
        raise Exception("network error")

    engine = ToolEngine()
    engine.register(ToolDef(
        name="bad_search", description="", phases=[1], parameters={},
        _fn=failing_read, side_effect="read",
    ))
    engine.register(ToolDef(
        name="search_a", description="", phases=[1], parameters={},
        _fn=_slow_read, side_effect="read",
    ))
    calls = [
        ToolCall(id="1", name="bad_search", arguments={}),
        ToolCall(id="2", name="search_a", arguments={"q": "ok"}),
    ]
    results = await engine.execute_batch(calls)
    assert results[0].status == "error"
    assert results[1].status == "success"


@pytest.mark.asyncio
async def test_execute_batch_maps_gather_exception_to_internal_error_result():
    async def execute_stub(call):
        if call.name == "bad_search":
            raise RuntimeError("boom")
        return ToolResult(tool_call_id=call.id, status="success", data={"ok": True})

    engine = _make_engine()
    engine.execute = execute_stub  # type: ignore[method-assign]
    calls = [
        ToolCall(id="1", name="bad_search", arguments={}),
        ToolCall(id="2", name="search_a", arguments={"q": "ok"}),
    ]

    results = await engine.execute_batch(calls)

    assert results[0].status == "error"
    assert results[0].error_code == "INTERNAL_ERROR"
    assert results[0].error == "boom"
    assert results[0].suggestion == "An unexpected error occurred"
    assert results[1].status == "success"


@pytest.mark.asyncio
async def test_execute_tool_batch_emits_tool_call_and_result_trace_events():
    store = _TraceStore()
    recorder = TraceRecorder(trace_store=store)
    engine = _make_engine()
    messages: list[Message] = []
    outcome = None

    async for item in execute_tool_batch(
        tool_calls=[ToolCall(id="call-1", name="search_a", arguments={"q": "tokyo"})],
        messages=messages,
        tool_engine=engine,
        hooks=HookManager(),
        guardrail=None,
        parallel_tool_execution=False,
        parallel_group_counter=0,
        search_history=SearchHistoryTracker(),
        check_cancelled=lambda: None,
        run_after_tool_result_hook=_empty_after_tool_result_hook,
        current_progress=IterationProgress.NO_OUTPUT,
        trace_recorder=recorder,
        trace_context=TraceContext(
            run_id="run-1",
            session_id="session-1",
            phase=1,
            correlation_id="corr-1",
        ),
        trace_parent_event_id="evt-llm-output",
        trace_correlation_id="corr-1",
    ):
        if isinstance(item, ToolBatchOutcome):
            outcome = item

    assert outcome is not None
    assert [event.event_type for event in store.events] == [
        "tool_call",
        "tool_result",
    ]
    call_event, result_event = store.events
    assert call_event.parent_event_id == "evt-llm-output"
    assert call_event.payload["tool_call_id"] == "call-1"
    assert call_event.payload["arguments_hash"].startswith("sha256:")
    assert call_event.payload["side_effect"] == "read"
    assert result_event.parent_event_id == call_event.event_id
    assert result_event.payload["status"] == "success"
    assert result_event.payload["quality_flags"]["usable"] is True
    assert [artifact.kind for artifact in store.artifacts] == [
        "tool_arguments",
        "tool_result",
    ]
    assert messages[-1].role == Role.TOOL


@pytest.mark.asyncio
async def test_execute_tool_batch_emits_state_diff_for_writer_tool():
    class _Plan:
        def __init__(self):
            self.destination = None

        def to_dict(self):
            return {"destination": self.destination, "phase": 1}

    plan = _Plan()

    async def write_destination(**kwargs):
        plan.destination = kwargs["destination"]
        return {"updated_fields": ["destination"]}

    engine = ToolEngine()
    engine.register(
        ToolDef(
            name="update_state",
            description="",
            phases=[1],
            parameters={"type": "object", "properties": {}},
            _fn=write_destination,
            side_effect="write",
        )
    )
    store = _TraceStore()
    recorder = TraceRecorder(trace_store=store)

    async for _item in execute_tool_batch(
        tool_calls=[
            ToolCall(
                id="call-1",
                name="update_state",
                arguments={"destination": "Tokyo"},
            )
        ],
        messages=[],
        tool_engine=engine,
        hooks=HookManager(),
        guardrail=None,
        parallel_tool_execution=False,
        parallel_group_counter=0,
        search_history=SearchHistoryTracker(),
        check_cancelled=lambda: None,
        run_after_tool_result_hook=_empty_after_tool_result_hook,
        current_progress=IterationProgress.NO_OUTPUT,
        plan=plan,
        trace_recorder=recorder,
        trace_context=TraceContext(run_id="run-1", session_id="session-1", phase=1),
        trace_parent_event_id="evt-llm-output",
        trace_correlation_id="corr-1",
    ):
        pass

    event_types = [event.event_type for event in store.events]
    assert event_types == ["tool_call", "tool_result", "state_diff"]
    result_event = store.events[1]
    diff_event = store.events[2]
    assert diff_event.parent_event_id == result_event.event_id
    assert diff_event.payload["state_hash_before"] != diff_event.payload["state_hash_after"]
    assert diff_event.payload["changed_top_level_fields"] == ["destination"]
    assert diff_event.payload["field_diffs"]["destination"]["before_hash"].startswith(
        "sha256:"
    )


@pytest.mark.asyncio
async def test_execute_tool_batch_emits_no_op_state_diff_for_writer_tool():
    class _Plan:
        def to_dict(self):
            return {"destination": "Tokyo", "phase": 1}

    async def no_op_writer(**kwargs):
        return {"updated_fields": []}

    engine = ToolEngine()
    engine.register(
        ToolDef(
            name="update_state",
            description="",
            phases=[1],
            parameters={"type": "object", "properties": {}},
            _fn=no_op_writer,
            side_effect="write",
        )
    )
    store = _TraceStore()
    recorder = TraceRecorder(trace_store=store)

    async for _item in execute_tool_batch(
        tool_calls=[ToolCall(id="call-1", name="update_state", arguments={})],
        messages=[],
        tool_engine=engine,
        hooks=HookManager(),
        guardrail=None,
        parallel_tool_execution=False,
        parallel_group_counter=0,
        search_history=SearchHistoryTracker(),
        check_cancelled=lambda: None,
        run_after_tool_result_hook=_empty_after_tool_result_hook,
        current_progress=IterationProgress.NO_OUTPUT,
        plan=_Plan(),
        trace_recorder=recorder,
        trace_context=TraceContext(run_id="run-1", session_id="session-1", phase=1),
        trace_parent_event_id="evt-llm-output",
        trace_correlation_id="corr-1",
    ):
        pass

    diff_event = next(event for event in store.events if event.event_type == "state_diff")
    assert diff_event.status == "success"
    assert diff_event.payload["no_op"] is True
    assert diff_event.payload["state_hash_before"] == diff_event.payload["state_hash_after"]
    assert diff_event.payload["changed_top_level_fields"] == []
    assert diff_event.payload["field_diffs"] == {}


@pytest.mark.asyncio
async def test_execute_tool_batch_emits_failed_state_diff_for_writer_tool():
    class _Plan:
        def to_dict(self):
            return {"destination": "Tokyo", "phase": 1}

    async def failing_writer(**kwargs):
        raise ToolError(
            "cannot write",
            error_code="INVALID_VALUE",
            suggestion="fix arguments",
        )

    engine = ToolEngine()
    engine.register(
        ToolDef(
            name="update_state",
            description="",
            phases=[1],
            parameters={"type": "object", "properties": {}},
            _fn=failing_writer,
            side_effect="write",
        )
    )
    store = _TraceStore()
    recorder = TraceRecorder(trace_store=store)

    async for _item in execute_tool_batch(
        tool_calls=[ToolCall(id="call-1", name="update_state", arguments={})],
        messages=[],
        tool_engine=engine,
        hooks=HookManager(),
        guardrail=None,
        parallel_tool_execution=False,
        parallel_group_counter=0,
        search_history=SearchHistoryTracker(),
        check_cancelled=lambda: None,
        run_after_tool_result_hook=_empty_after_tool_result_hook,
        current_progress=IterationProgress.NO_OUTPUT,
        plan=_Plan(),
        trace_recorder=recorder,
        trace_context=TraceContext(run_id="run-1", session_id="session-1", phase=1),
        trace_parent_event_id="evt-llm-output",
        trace_correlation_id="corr-1",
    ):
        pass

    result_event = next(event for event in store.events if event.event_type == "tool_result")
    diff_event = next(event for event in store.events if event.event_type == "state_diff")
    assert result_event.status == "error"
    assert result_event.payload["error_code"] == "INVALID_VALUE"
    assert diff_event.parent_event_id == result_event.event_id
    assert diff_event.status == "error"
    assert diff_event.payload["no_op"] is True
    assert diff_event.payload["state_hash_before"] == diff_event.payload["state_hash_after"]


@pytest.mark.asyncio
async def test_execute_tool_batch_state_diff_captures_backtrack_clearing_downstream():
    plan = TravelPlanState(
        session_id="session-1",
        phase=3,
        destination="Tokyo",
        dates=DateRange(start="2026-07-01", end="2026-07-02"),
        trip_brief={"pace": "relaxed"},
        candidate_pool=[{"id": "poi-1"}],
        shortlist=[{"id": "poi-1"}],
        skeleton_plans=[{"id": "sk-1", "days": [{"day": 1}, {"day": 2}]}],
        selected_skeleton_id="sk-1",
        daily_plans=[DayPlan(day=1, date="2026-07-01")],
        deliverables={"travel_plan.md": "artifact-1"},
    )
    engine = ToolEngine()
    engine.register(make_request_backtrack_tool(plan))
    store = _TraceStore()
    recorder = TraceRecorder(trace_store=store)

    async for _item in execute_tool_batch(
        tool_calls=[
            ToolCall(
                id="call-1",
                name="request_backtrack",
                arguments={"to_phase": 2, "reason": "换住宿策略"},
            )
        ],
        messages=[],
        tool_engine=engine,
        hooks=HookManager(),
        guardrail=None,
        parallel_tool_execution=False,
        parallel_group_counter=0,
        search_history=SearchHistoryTracker(),
        check_cancelled=lambda: None,
        run_after_tool_result_hook=_empty_after_tool_result_hook,
        current_progress=IterationProgress.NO_OUTPUT,
        plan=plan,
        trace_recorder=recorder,
        trace_context=TraceContext(run_id="run-1", session_id="session-1", phase=3),
        trace_parent_event_id="evt-llm-output",
        trace_correlation_id="corr-1",
    ):
        pass

    diff_event = next(event for event in store.events if event.event_type == "state_diff")
    changed_fields = set(diff_event.payload["changed_top_level_fields"])
    # P1-3 ②：选择性清除——dates/trip_brief/candidate_pool 保留不再出现在 diff 中
    assert {"skeleton_plans", "selected_skeleton_id", "daily_plans", "deliverables"} <= changed_fields
    assert diff_event.payload["field_diffs"]["daily_plans"]["after_hash"].startswith("sha256:")
    assert plan.phase == 2
    assert plan.dates is not None  # 选择性清除保留 dates
    assert plan.daily_plans == []
    assert plan.deliverables is None


@pytest.mark.asyncio
async def test_execute_tool_batch_emits_validation_trace_after_hook_metadata():
    store = _TraceStore()
    recorder = TraceRecorder(trace_store=store)
    engine = _make_engine()
    hooks = HookManager()

    async def mark_validation_error(**kwargs):
        result = kwargs["result"]
        result.metadata = dict(result.metadata or {})
        result.metadata["validation_errors"] = ["bad output"]
        result.metadata["validation_rule_id"] = "test_rule"
        result.metadata["judge_scores"] = {"overall": 2.5}

    hooks.register("after_tool_call", mark_validation_error)

    async for _item in execute_tool_batch(
        tool_calls=[ToolCall(id="call-1", name="search_a", arguments={"q": "tokyo"})],
        messages=[],
        tool_engine=engine,
        hooks=hooks,
        guardrail=None,
        parallel_tool_execution=False,
        parallel_group_counter=0,
        search_history=SearchHistoryTracker(),
        check_cancelled=lambda: None,
        run_after_tool_result_hook=_empty_after_tool_result_hook,
        current_progress=IterationProgress.NO_OUTPUT,
        trace_recorder=recorder,
        trace_context=TraceContext(run_id="run-1", session_id="session-1", phase=1),
        trace_parent_event_id="evt-llm-output",
        trace_correlation_id="corr-1",
    ):
        pass

    event_types = [event.event_type for event in store.events]
    assert event_types == ["tool_call", "tool_result", "validation"]
    result_event = store.events[1]
    validation_event = store.events[2]
    assert result_event.payload["judge_scores"] == {"overall": 2.5}
    assert validation_event.parent_event_id == result_event.event_id
    assert validation_event.payload["validation_rule_id"] == "test_rule"
    assert validation_event.payload["severity"] == "error"
    assert validation_event.payload["message"] == "bad output"


@pytest.mark.asyncio
async def test_execute_tool_batch_emits_soft_judge_trace_from_internal_task():
    store = _TraceStore()
    recorder = TraceRecorder(trace_store=store)

    async for _item in execute_tool_batch(
        tool_calls=[ToolCall(id="call-1", name="search_a", arguments={"q": "tokyo"})],
        messages=[],
        tool_engine=_make_engine(),
        hooks=HookManager(),
        guardrail=None,
        parallel_tool_execution=False,
        parallel_group_counter=0,
        search_history=SearchHistoryTracker(),
        check_cancelled=lambda: None,
        run_after_tool_result_hook=_soft_judge_after_tool_result_hook,
        current_progress=IterationProgress.NO_OUTPUT,
        trace_recorder=recorder,
        trace_context=TraceContext(run_id="run-1", session_id="session-1", phase=1),
        trace_parent_event_id="evt-llm-output",
        trace_correlation_id="corr-1",
    ):
        pass

    result_event = next(event for event in store.events if event.event_type == "tool_result")
    judge_event = next(event for event in store.events if event.event_type == "soft_judge")
    assert judge_event.parent_event_id == result_event.event_id
    assert judge_event.payload["judge_scores"]["overall"] == 2.0
    assert judge_event.payload["advisory"] is True
    assert judge_event.payload["related_tool_call_id"] == "call-1"
