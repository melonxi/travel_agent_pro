# backend/tests/test_day_worker.py
import json
import pytest

from agent.day_worker import (
    DayWorkerResult,
    _MAX_POI_RECOVERY,
    _MAX_SAME_QUERY,
    _should_force_emit,
    _tool_query_fingerprint,
    _tool_recovery_key,
    extract_dayplan_json,
    run_day_worker,
)
from agent.types import ToolCall, ToolResult
from agent.worker_prompt import DayTask
from llm.types import ChunkType, LLMChunk
from state.models import DateRange, TravelPlanState


def _stub_plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="s-day-worker")
    plan.phase = 5
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

    async def chat(self, messages, tools=None, stream=True):
        self.calls.append(list(messages))
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
    def __init__(self, results=None):
        self._results = list(results) if results else []

    def get_tool(self, name):
        return None

    async def execute_batch(self, tool_calls):
        return self._results


def _tc(name: str, call_id: str = "call_1", **kwargs) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=kwargs)


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
    assert "只输出合法 DayPlan JSON" in repair_message.content
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

    ca = ToolCall(id="4", name="check_availability", arguments={"placeName": "寿司大", "date": "2026-05-01"})
    assert _tool_query_fingerprint(ca) == "check_availability:寿司大:2026-05-01"

    other = ToolCall(id="5", name="calculate_route", arguments={"from": "A", "to": "B"})
    assert _tool_query_fingerprint(other) is None


def test_tool_recovery_key():
    gpi = ToolCall(id="1", name="get_poi_info", arguments={"query": "浅草寺"})
    assert _tool_recovery_key(gpi) == "浅草寺"

    ca = ToolCall(id="2", name="check_availability", arguments={"placeName": "寿司大"})
    assert _tool_recovery_key(ca) == "寿司大"

    ws = ToolCall(id="3", name="web_search", arguments={"query": "东京塔门票"})
    assert _tool_recovery_key(ws) == "东京塔门票"

    other = ToolCall(id="4", name="calculate_route", arguments={})
    assert _tool_recovery_key(other) is None


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
            if msg.role.value == "system" and "收口阶段" in msg.content:
                late_emit_found = True
    assert late_emit_found


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
                    tool_call=_tc("web_search", call_id="r1", query="浅草寺"),
                ),
                LLMChunk(type=ChunkType.DONE),
            ],
            [
                LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=_tc("check_availability", call_id="r2", placeName="浅草寺", date="2026-05-01"),
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