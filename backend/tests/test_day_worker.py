# backend/tests/test_day_worker.py
import json
import pytest

from agent.phase3.candidate_store import Phase3CandidateStore
from agent.phase3.day_worker import (
    DayWorkerResult,
    _MAX_POI_RECOVERY,
    _MAX_SAME_QUERY,
    _dayplan_time_conflicts,
    _should_force_emit,
    _tool_query_fingerprint,
    _tool_recovery_key,
    extract_dayplan_json,
    run_day_worker,
)
from agent.types import ToolCall, ToolResult
from agent.phase3.worker_prompt import DayTask
from llm.types import ChunkType, LLMChunk
from state.models import DateRange, TravelPlanState
from telemetry.stats import SessionStats
from telemetry.trace_recorder import TraceContext, TraceRecorder


class _TraceStore:
    def __init__(self):
        self.events = []
        self.artifacts = []

    async def append_event(self, event):
        self.events.append(event)

    async def save_artifact_metadata(self, metadata):
        self.artifacts.append(metadata)


def _stub_plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="s-day-worker")
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.selected_skeleton_id = "skeleton-1"
    plan.skeleton_plans = [{"id": "skeleton-1", "days": [{"area": "A", "theme": "T"}]}]
    return plan


def _task() -> DayTask:
    return DayTask(
        day=1,
        date="2026-05-01",
        skeleton_slice={"area": "A", "theme": "T"},
        pace="balanced",
    )


class _LLMStub:
    def __init__(self, chunk_batches: list[list[LLMChunk]]):
        self._chunk_batches = list(chunk_batches)
        self.calls: list[list] = []
        self.tool_schemas: list[list[dict] | None] = []
        self.provider_name = "test-provider"
        self.model = "test-model"

    async def chat(self, messages, tools=None, stream=True):
        self.calls.append(list(messages))
        self.tool_schemas.append(list(tools) if tools is not None else None)
        batch = self._chunk_batches.pop(0)
        for chunk in batch:
            yield chunk


class _ToolEngineStub:
    def get_tool(self, name):
        return None

    async def execute_batch(self, tool_calls):
        raise AssertionError("unexpected tool execution")


class _ToolResultHelper:
    def __init__(self, tool_call_id, status, data=None):
        self.tool_call_id = tool_call_id
        self.status = status
        self.data = data
        self.metadata = None
        self.error = None
        self.error_code = None
        self.suggestion = None


class _ToolEngineWithResults:
    def __init__(self, results=None, tool_names=None):
        self._results = list(results) if results else []
        self.executed_batches: list[list[ToolCall]] = []
        self._tool_names = set(tool_names or [])

    def get_tool(self, name):
        if name not in self._tool_names:
            return None
        return _ToolDefStub(name)

    async def execute_batch(self, tool_calls):
        self.executed_batches.append(list(tool_calls))
        count = len(tool_calls)
        results = self._results[:count]
        self._results = self._results[count:]
        return results


class _ToolDefStub:
    def __init__(self, name):
        self.name = name
        self.human_label = name

    def to_schema(self):
        return {
            "name": self.name,
            "description": f"{self.name} test tool",
            "parameters": {"type": "object", "properties": {}},
        }


def _tc(name: str, call_id: str = "call_1", **kwargs) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=kwargs)


@pytest.mark.asyncio
async def test_run_day_worker_puts_day_task_in_user_message():
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content='{"day": 1, "date": "2026-05-01", "activities": []}',
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )

    result = await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="SHARED PREFIX",
        timeout_seconds=5,
    )

    assert result.success is True
    first_call_messages = llm.calls[0]
    assert first_call_messages[0].role.value == "system"
    assert first_call_messages[0].content == "SHARED PREFIX"
    assert first_call_messages[1].role.value == "user"
    assert "第 1 天" in first_call_messages[1].content
    assert "请执行以上 DayTask" in first_call_messages[1].content
    assert "工具调用预算" in first_call_messages[1].content


@pytest.mark.asyncio
async def test_run_day_worker_accepts_submit_day_plan_candidate_tool(tmp_path):
    dayplan = {"day": 1, "date": "2026-05-01", "notes": "submitted", "activities": []}
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc(
                        "submit_day_plan_candidate",
                        call_id="submit_1",
                        dayplan=dayplan,
                    ),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(type=ChunkType.TEXT_DELTA, content="已提交第 1 天计划。"),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    store = Phase3CandidateStore(tmp_path)

    result = await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
        candidate_store=store,
        run_id="run_1",
        attempt=1,
    )

    assert result.success is True
    assert result.dayplan == dayplan
    loaded = store.load_latest_candidates("s-day-worker", "run_1")
    assert len(loaded) == 1
    assert loaded[0]["dayplan"] == dayplan


@pytest.mark.asyncio
async def test_run_day_worker_records_worker_stats(tmp_path):
    dayplan = {"day": 1, "date": "2026-05-01", "notes": "submitted", "activities": []}
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc(
                        "submit_day_plan_candidate",
                        call_id="submit_1",
                        dayplan=dayplan,
                    ),
                ),
                LLMChunk(
                    type=ChunkType.USAGE,
                    usage_info={"input_tokens": 12, "output_tokens": 4},
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(type=ChunkType.TEXT_DELTA, content="已提交第 1 天计划。"),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    store = Phase3CandidateStore(tmp_path)
    stats = SessionStats()

    result = await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
        candidate_store=store,
        run_id="phase3_run_1",
        attempt=1,
        stats=stats,
    )

    assert result.success is True
    assert len(stats.llm_calls) == 2
    assert stats.llm_calls[0].provider == "test-provider"
    assert stats.llm_calls[0].model == "test-model"
    assert stats.llm_calls[0].input_tokens == 12
    assert stats.llm_calls[0].metadata["scope"] == "phase3_worker"
    assert stats.llm_calls[0].metadata["worker_run_id"] == "phase3_run_1"
    assert stats.tool_calls[0].tool_name == "submit_day_plan_candidate"
    assert stats.tool_calls[0].metadata["day"] == 1
    assert stats.tool_calls[0].metadata["tool_call_id"] == "submit_1"


@pytest.mark.asyncio
async def test_run_day_worker_emits_worker_trace_events(tmp_path):
    dayplan = {"day": 1, "date": "2026-05-01", "notes": "submitted", "activities": []}
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc(
                        "submit_day_plan_candidate",
                        call_id="submit_1",
                        dayplan=dayplan,
                    ),
                ),
                LLMChunk(
                    type=ChunkType.USAGE,
                    usage_info={"input_tokens": 12, "output_tokens": 4},
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(type=ChunkType.TEXT_DELTA, content="已提交第 1 天计划。"),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    trace_store = _TraceStore()

    result = await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="SHARED PREFIX",
        timeout_seconds=5,
        candidate_store=Phase3CandidateStore(tmp_path),
        run_id="phase3_run_1",
        attempt=1,
        trace_recorder=TraceRecorder(trace_store=trace_store),
        trace_context=TraceContext(
            run_id="main-run",
            session_id="s-day-worker",
            phase=3,
        ),
        trace_parent_event_id="evt_orchestrator",
    )

    assert result.success is True
    event_types = [event.event_type for event in trace_store.events]
    assert event_types[:4] == ["llm_call", "llm_output", "tool_call", "tool_result"]

    llm_output = trace_store.events[1]
    tool_call = trace_store.events[2]
    tool_result = trace_store.events[3]
    assert tool_call.parent_event_id == llm_output.event_id
    assert tool_result.parent_event_id == tool_call.event_id
    assert tool_call.actor == "phase3_worker"
    assert tool_call.payload["scope"] == "phase3_worker"
    assert tool_call.payload["day"] == 1
    assert tool_call.payload["side_effect"] == "phase3_candidate_submit"
    assert tool_result.payload["candidate_submission_path"]

    artifact_kinds = {artifact.kind for artifact in trace_store.artifacts}
    assert {"llm_prompt", "llm_response", "tool_arguments", "tool_result"} <= artifact_kinds


def test_extract_dayplan_json_from_code_block():
    text = """我来为你规划第 3 天的行程。

```json
{
  "day": 3,
  "date": "2026-05-03",
  "notes": "浅草-上野文化区",
  "activities": [
    {
      "name": "浅草寺",
      "location": {"name": "浅草寺", "lat": 35.7148, "lng": 139.7967},
      "start_time": "09:00",
      "end_time": "10:30",
      "category": "shrine",
      "cost": 0,
      "transport_from_prev": "地铁",
      "transport_duration_min": 20,
      "notes": ""
    }
  ]
}
```"""
    result = extract_dayplan_json(text)
    assert result is not None
    assert result["day"] == 3
    assert len(result["activities"]) == 1
    assert result["activities"][0]["name"] == "浅草寺"


def test_extract_dayplan_json_bare_json():
    """Worker 可能直接输出 JSON 不带代码块。"""
    data = {
        "day": 1,
        "date": "2026-05-01",
        "notes": "",
        "activities": [],
    }
    text = json.dumps(data, ensure_ascii=False)
    result = extract_dayplan_json(text)
    assert result is not None
    assert result["day"] == 1


def test_extract_dayplan_json_no_json():
    text = "我正在规划行程，请稍等..."
    result = extract_dayplan_json(text)
    assert result is None


def test_day_worker_result_success():
    r = DayWorkerResult(
        day=1,
        date="2026-05-01",
        success=True,
        dayplan={"day": 1, "date": "2026-05-01", "activities": []},
        error=None,
    )
    assert r.success is True
    assert r.dayplan is not None
    assert r.error_code is None


def test_day_worker_result_failure():
    r = DayWorkerResult(
        day=2,
        date="2026-05-02",
        success=False,
        dayplan=None,
        error="LLM timeout",
        error_code="LLM_TIMEOUT",
    )
    assert r.success is False
    assert "timeout" in r.error
    assert r.error_code == "LLM_TIMEOUT"


@pytest.mark.asyncio
async def test_run_day_worker_retries_once_when_first_final_output_is_not_json():
    llm = _LLMStub(
        [
            [
                LLMChunk(type=ChunkType.TEXT_DELTA, content="我整理一下后直接给你结果"),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content='{"day": 1, "date": "2026-05-01", "activities": []}',
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )

    result = await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
    )

    assert result.success is True
    assert result.dayplan == {"day": 1, "date": "2026-05-01", "activities": []}
    assert len(llm.calls) == 2
    repair_message = llm.calls[1][-1]
    assert repair_message.role.value == "system"
    assert "submit_day_plan_candidate" in repair_message.content
    assert "day" in repair_message.content
    assert "date" in repair_message.content
    assert "activities" in repair_message.content


@pytest.mark.asyncio
async def test_run_day_worker_returns_json_emit_failed_after_repair_attempt_exhausted():
    llm = _LLMStub(
        [
            [
                LLMChunk(type=ChunkType.TEXT_DELTA, content="先给你一个自然语言版本"),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(type=ChunkType.TEXT_DELTA, content="还是先描述一下今天安排"),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )

    result = await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
    )

    assert result.success is False
    assert result.dayplan is None
    assert result.error_code == "JSON_EMIT_FAILED"
    assert len(llm.calls) == 2


def test_tool_query_fingerprint():
    ws = ToolCall(id="1", name="web_search", arguments={"query": "东京美食"})
    assert _tool_query_fingerprint(ws) == "web_search:东京美食"

    gpi_q = ToolCall(id="2", name="get_poi_info", arguments={"query": "浅草寺"})
    assert _tool_query_fingerprint(gpi_q) == "get_poi_info:浅草寺"

    gpi_n = ToolCall(id="3", name="get_poi_info", arguments={"name": "天空树"})
    assert _tool_query_fingerprint(gpi_n) == "get_poi_info:天空树"

    route = ToolCall(
        id="5",
        name="calculate_route",
        arguments={
            "origin_lat": 35.7147651,
            "origin_lng": 139.7966553,
            "dest_lat": 35.7147557,
            "dest_lng": 139.7734312,
            "mode": "transit",
        },
    )
    assert (
        _tool_query_fingerprint(route)
        == "calculate_route:transit:35.71477,139.79666->35.71476,139.77343"
    )


def test_tool_recovery_key():
    gpi = ToolCall(id="1", name="get_poi_info", arguments={"query": "浅草寺"})
    assert _tool_recovery_key(gpi) == "浅草寺"

    ws = ToolCall(id="3", name="web_search", arguments={"query": "东京塔门票"})
    assert _tool_recovery_key(ws) == "东京塔门票"

    route = ToolCall(
        id="4",
        name="calculate_route",
        arguments={
            "origin_lat": 1,
            "origin_lng": 2,
            "dest_lat": 3,
            "dest_lng": 4,
        },
    )
    assert _tool_recovery_key(route) == "calculate_route:transit:1.0,2.0->3.0,4.0"


def test_dayplan_time_conflicts_detects_transport_gap():
    dayplan = {
        "day": 1,
        "date": "2026-05-01",
        "activities": [
            {"name": "浅草寺", "start_time": "10:00", "end_time": "11:00"},
            {
                "name": "上野公园",
                "start_time": "11:00",
                "end_time": "12:00",
                "transport_duration_min": 15,
            },
        ],
    }

    issues = _dayplan_time_conflicts(dayplan)

    assert len(issues) == 1
    assert "浅草寺 11:00 结束 + 交通 15min > 上野公园 11:00 开始" in issues[0]


@pytest.mark.asyncio
async def test_submit_day_plan_candidate_rejects_time_conflict_then_repairs(tmp_path):
    bad_dayplan = {
        "day": 1,
        "date": "2026-05-01",
        "activities": [
            {
                "name": "A",
                "location": {"name": "A", "lat": 1, "lng": 2},
                "start_time": "10:00",
                "end_time": "11:00",
                "category": "activity",
                "cost": 0,
            },
            {
                "name": "B",
                "location": {"name": "B", "lat": 3, "lng": 4},
                "start_time": "11:00",
                "end_time": "12:00",
                "category": "activity",
                "cost": 0,
                "transport_duration_min": 20,
            },
        ],
    }
    repaired_dayplan = {
        **bad_dayplan,
        "activities": [
            bad_dayplan["activities"][0],
            {**bad_dayplan["activities"][1], "start_time": "11:25"},
        ],
    }
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc(
                        "submit_day_plan_candidate",
                        call_id="submit_bad",
                        dayplan=bad_dayplan,
                    ),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc(
                        "submit_day_plan_candidate",
                        call_id="submit_fixed",
                        dayplan=repaired_dayplan,
                    ),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(type=ChunkType.TEXT_DELTA, content="已提交第 1 天计划。"),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    store = Phase3CandidateStore(tmp_path)

    result = await run_day_worker(
        llm=llm,
        tool_engine=_ToolEngineStub(),
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
        candidate_store=store,
        run_id="run_time_repair",
        attempt=1,
    )

    assert result.success is True
    assert result.dayplan == repaired_dayplan
    loaded = store.load_latest_candidates("s-day-worker", "run_time_repair")
    assert loaded[0]["dayplan"] == repaired_dayplan


@pytest.mark.asyncio
async def test_no_route_duplicate_calculate_route_is_short_circuited():
    route_args = {
        "origin_lat": 35.7147651,
        "origin_lng": 139.7966553,
        "dest_lat": 35.7147557,
        "dest_lng": 139.7734312,
        "mode": "transit",
    }
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("calculate_route", call_id="r1", **route_args),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("calculate_route", call_id="r2", **route_args),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content='{"day": 1, "date": "2026-05-01", "activities": []}',
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    first_error = ToolResult(
        tool_call_id="r1",
        status="error",
        error="Google Directions API returned ZERO_RESULTS",
        error_code="NO_ROUTE",
        suggestion="Use a conservative estimate.",
    )
    tool_engine = _ToolEngineWithResults([first_error])

    result = await run_day_worker(
        llm=llm,
        tool_engine=tool_engine,
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
    )

    assert result.success is True
    assert len(tool_engine.executed_batches) == 1
    assert tool_engine.executed_batches[0][0].id == "r1"
    route_hint_found = False
    for call_msgs in llm.calls:
        for msg in call_msgs:
            if msg.role.value == "system" and "web_search 兜底一次" in msg.content:
                route_hint_found = True
    assert route_hint_found


@pytest.mark.asyncio
async def test_followup_prompts_are_appended_after_all_tool_results():
    route_args = {
        "origin_lat": 35.7147651,
        "origin_lng": 139.7966553,
        "dest_lat": 35.7147557,
        "dest_lng": 139.7734312,
        "mode": "transit",
    }
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("calculate_route", call_id="route1", **route_args),
                ),
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("get_poi_info", call_id="poi1", query="浅草寺"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content='{"day": 1, "date": "2026-05-01", "activities": []}',
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    route_error = ToolResult(
        tool_call_id="route1",
        status="error",
        error="Google Directions API returned ZERO_RESULTS",
        error_code="NO_ROUTE",
    )
    poi_result = ToolResult(
        tool_call_id="poi1",
        status="success",
        data={"pois": [{"name": "浅草寺"}]},
    )
    tool_engine = _ToolEngineWithResults([route_error, poi_result])

    result = await run_day_worker(
        llm=llm,
        tool_engine=tool_engine,
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
    )

    assert result.success is True
    second_call_messages = llm.calls[1]
    assistant_idx = max(
        index
        for index, msg in enumerate(second_call_messages)
        if msg.role.value == "assistant" and msg.tool_calls
    )
    roles_after_assistant = [
        msg.role.value for msg in second_call_messages[assistant_idx + 1:]
    ]
    assert roles_after_assistant[:2] == ["tool", "tool"]
    assert roles_after_assistant[2:] == ["system"]


@pytest.mark.asyncio
async def test_get_poi_info_failure_adds_single_web_search_fallback_hint():
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("get_poi_info", call_id="poi1", query="浅草寺"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content='{"day": 1, "date": "2026-05-01", "activities": []}',
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    poi_error = ToolResult(
        tool_call_id="poi1",
        status="error",
        error="No POI results from any source",
        error_code="NO_RESULTS",
        suggestion="Try a different search query",
    )
    tool_engine = _ToolEngineWithResults([poi_error])
    stats = SessionStats()

    result = await run_day_worker(
        llm=llm,
        tool_engine=tool_engine,
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
        stats=stats,
    )

    assert result.success is True
    fallback_hint_found = False
    for msg in llm.calls[1]:
        if (
            msg.role.value == "system"
            and "浅草寺" in msg.content
            and "web_search 兜底一次" in msg.content
        ):
            fallback_hint_found = True
    assert fallback_hint_found
    assert stats.tool_calls[0].metadata["fallback_source"] == "web_search_once"
    assert stats.tool_calls[0].metadata["fallback_count"] == 1


@pytest.mark.asyncio
async def test_web_search_failure_adds_degrade_hint():
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("web_search", call_id="web1", query="东京路线"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content='{"day": 1, "date": "2026-05-01", "activities": []}',
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    web_error = ToolResult(
        tool_call_id="web1",
        status="error",
        error="Tavily API error: 500",
        error_code="API_ERROR",
    )
    tool_engine = _ToolEngineWithResults([web_error])

    result = await run_day_worker(
        llm=llm,
        tool_engine=tool_engine,
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
    )

    assert result.success is True
    assert any(
        msg.role.value == "system" and "不要再围绕同一查询追加兜底工具" in msg.content
        for msg in llm.calls[1]
    )


@pytest.mark.asyncio
async def test_xiaohongshu_auth_failure_disables_future_xhs_tools():
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc(
                        "xiaohongshu_search_notes",
                        call_id="xhs1",
                        query="东京甜品",
                    ),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc(
                        "xiaohongshu_search_notes",
                        call_id="xhs2",
                        query="东京甜品",
                    ),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content='{"day": 1, "date": "2026-05-01", "activities": []}',
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    xhs_auth_error = ToolResult(
        tool_call_id="xhs1",
        status="error",
        error="login required",
        error_code="NOT_AUTHENTICATED",
    )
    tool_engine = _ToolEngineWithResults(
        [xhs_auth_error],
        tool_names=["xiaohongshu_search_notes", "web_search"],
    )

    result = await run_day_worker(
        llm=llm,
        tool_engine=tool_engine,
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
    )

    assert result.success is True
    assert len(tool_engine.executed_batches) == 1
    assert tool_engine.executed_batches[0][0].id == "xhs1"
    second_call_tool_names = {schema["name"] for schema in llm.tool_schemas[1]}
    assert "xiaohongshu_search_notes" not in second_call_tool_names
    assert "web_search" in second_call_tool_names
    assert any(
        msg.role.value == "system" and "小红书工具当前不可用" in msg.content
        for msg in llm.calls[1]
    )


def test_max_constants():
    assert _MAX_SAME_QUERY == 2
    assert _MAX_POI_RECOVERY == 3


def test_should_force_emit():
    assert _should_force_emit(2, 5) is True
    assert _should_force_emit(1, 5) is False
    assert _should_force_emit(5, 10) is True
    assert _should_force_emit(2, 3) is True


@pytest.mark.asyncio
async def test_late_emit_hint_added_when_past_60_percent():
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("web_search", call_id="c1", query="test1"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("web_search", call_id="c2", query="test2"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("web_search", call_id="c3", query="test3"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("web_search", call_id="c4", query="test4"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content='{"day": 1, "date": "2026-05-01", "activities": []}',
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    tool_results = [
        _ToolResultHelper("c1", "success", data={"results": []}),
        _ToolResultHelper("c2", "success", data={"results": []}),
        _ToolResultHelper("c3", "success", data={"results": []}),
    ]
    tool_engine = _ToolEngineWithResults(tool_results)

    result = await run_day_worker(
        llm=llm,
        tool_engine=tool_engine,
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        max_iterations=5,
        timeout_seconds=5,
    )

    assert result.success is True
    assert result.dayplan == {"day": 1, "date": "2026-05-01", "activities": []}
    late_emit_found = False
    for call_msgs in llm.calls:
        for msg in call_msgs:
            if msg.role.value == "system" and "工具调用预算" in msg.content:
                late_emit_found = True
    assert late_emit_found
    third_call_messages = llm.calls[2]
    assistant_idx = max(
        index
        for index, msg in enumerate(third_call_messages)
        if msg.role.value == "assistant" and msg.tool_calls
    )
    roles_after_assistant = [
        msg.role.value for msg in third_call_messages[assistant_idx + 1:]
    ]
    assert roles_after_assistant[:1] == ["tool"]
    assert roles_after_assistant[1:] == ["system"]


@pytest.mark.asyncio
async def test_repeated_query_triggers_forced_emit():
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("web_search", call_id="c1", query="东京美食"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("web_search", call_id="c2", query="东京美食"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("web_search", call_id="c3", query="东京美食"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content='{"day": 1, "date": "2026-05-01", "activities": []}',
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    tool_results = [
        _ToolResultHelper("c1", "success", data={"results": []}),
        _ToolResultHelper("c2", "success", data={"results": []}),
    ]
    tool_engine = _ToolEngineWithResults(tool_results)

    result = await run_day_worker(
        llm=llm,
        tool_engine=tool_engine,
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
    )

    assert result.success is True
    assert result.dayplan == {"day": 1, "date": "2026-05-01", "activities": []}
    forced_call_messages = llm.calls[3]
    assistant_idx = max(
        index
        for index, msg in enumerate(forced_call_messages)
        if msg.role.value == "assistant" and msg.tool_calls
    )
    roles_after_assistant = [
        msg.role.value for msg in forced_call_messages[assistant_idx + 1:]
    ]
    assert roles_after_assistant[:1] == ["tool"]
    assert roles_after_assistant[1:] == ["system"]


@pytest.mark.asyncio
async def test_recovery_chain_triggers_forced_emit():
    llm = _LLMStub(
        [
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("get_poi_info", call_id="r0", query="浅草寺"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("web_search", call_id="r1", query="浅草寺 开放时间"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("web_search", call_id="r2", query="浅草寺 营业时间"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("get_poi_info", call_id="r3", query="浅草寺"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content='{"day": 1, "date": "2026-05-01", "activities": []}',
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
        ]
    )
    tool_results = [
        _ToolResultHelper("r0", "success", data={}),
        _ToolResultHelper("r1", "success", data={}),
        _ToolResultHelper("r2", "success", data={}),
        _ToolResultHelper("r3", "success", data={}),
    ]
    tool_engine = _ToolEngineWithResults(tool_results)

    result = await run_day_worker(
        llm=llm,
        tool_engine=tool_engine,
        plan=_stub_plan(),
        task=_task(),
        shared_prefix="",
        timeout_seconds=5,
    )

    assert result.success is True
    assert result.dayplan == {"day": 1, "date": "2026-05-01", "activities": []}


def test_submit_schema_has_inline_properties():
    from agent.phase3.day_worker import _SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA
    schema = _SUBMIT_DAY_PLAN_CANDIDATE_SCHEMA
    assert schema["name"] == "submit_day_plan_candidate"
    dayplan = schema["parameters"]["properties"]["dayplan"]
    assert dayplan["type"] == "object"
    assert "day" in dayplan["properties"]
    assert "activities" in dayplan["properties"]
    act_item = dayplan["properties"]["activities"]["items"]
    assert act_item["properties"]["location"]["type"] == "object"
    assert "lat" in act_item["properties"]["location"]["properties"]
    assert "enum" in act_item["properties"]["category"]
    desc = schema["description"]
    assert "INVALID_DAYPLAN" in desc
    assert "SUBMIT_UNAVAILABLE" in desc


def test_forced_emit_prompt_no_fake_coordinates():
    from agent.phase3.day_worker import _FORCED_EMIT_PROMPT
    assert "0,0" not in _FORCED_EMIT_PROMPT or "绝不" in _FORCED_EMIT_PROMPT
    assert "绝不在 location 中填入 0,0 假坐标" in _FORCED_EMIT_PROMPT


def test_json_repair_prompt_references_submit_tool():
    from agent.phase3.day_worker import _JSON_REPAIR_PROMPT
    assert "submit_day_plan_candidate" in _JSON_REPAIR_PROMPT
    assert "day" in _JSON_REPAIR_PROMPT
