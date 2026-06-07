"""
Reproduce iteration-2 bug from session sess_26bb75610d54:

LLM calls save_day_plan(mode="replace_existing", day=1, tips="...")
without the required `activities` field, and the engine correctly
rejects it with INVALID_ARGUMENTS.  The next LLM iteration then
generates the correct call with activities.

This test exercises the full round-trip through ToolEngine ->
error propagation -> LLM retry -> success, verifying that:

1. ToolEngine pre-validation rejects missing `activities`
2. The error is surfaced to the LLM as a tool result
3. The LLM can produce a corrected call with `activities`
4. The corrected call succeeds and updates the plan
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent.types import Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from state.models import DateRange, DayPlan, TravelPlanState

_SAMPLE_ACTIVITY = {
    "name": "新宿御苑",
    "location": {"name": "新宿御苑", "lat": 35.6852, "lng": 139.7101},
    "start_time": "10:00",
    "end_time": "12:00",
    "category": "park",
    "cost": 50,
}


def _make_phase3_plan(session_id: str = "test-tips-bug") -> TravelPlanState:
    plan = TravelPlanState(session_id=session_id)
    plan.phase = 3
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-07-10", end="2026-07-12")
    plan.daily_plans = [
        DayPlan.from_dict(
            {"day": 1, "date": "2026-07-10", "activities": [_SAMPLE_ACTIVITY]}
        ),
        DayPlan.from_dict(
            {"day": 2, "date": "2026-07-11", "activities": [_SAMPLE_ACTIVITY]}
        ),
        DayPlan.from_dict(
            {"day": 3, "date": "2026-07-12", "activities": [_SAMPLE_ACTIVITY]}
        ),
    ]
    return plan


def _text_chunks(*texts: str) -> list[LLMChunk]:
    chunks = [LLMChunk(type=ChunkType.TEXT_DELTA, content=text) for text in texts]
    chunks.append(LLMChunk(type=ChunkType.DONE))
    return chunks


async def _collect_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload:
            events.append(json.loads(payload))
    return events


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")


@pytest.fixture
def app():
    from main import create_app

    return create_app(config_path="__nonexistent__.yaml")


@pytest.fixture
def sessions(app):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        closure = getattr(endpoint, "__closure__", None)
        if endpoint is None or closure is None:
            continue
        for cell in closure:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    pytest.fail("Could not locate 'sessions' dict from app closure")


@pytest.mark.asyncio
async def test_save_day_plan_missing_activities_rejected_then_fixed(app, sessions):
    """
    Iteration 1: LLM calls save_day_plan with tips but no activities → INVALID_ARGUMENTS
    Iteration 2: LLM calls save_day_plan with activities → success
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/sessions")
        assert resp.status_code == 200
        session_id = resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]

        phase3_plan = _make_phase3_plan()
        plan.phase = phase3_plan.phase
        plan.destination = phase3_plan.destination
        plan.dates = phase3_plan.dates
        plan.daily_plans = phase3_plan.daily_plans

        agent = session["agent"]
        agent.phase3_parallel_config = None

        call_count = 0
        error_seen_in_context = False

        async def fake_chat(messages, tools=None, stream=True, **kw):
            nonlocal call_count, error_seen_in_context
            call_count += 1

            if call_count == 1:
                # Reproduce iteration-2 bug: call save_day_plan with tips but no activities
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_buggy_day1",
                        name="save_day_plan",
                        arguments={
                            "mode": "replace_existing",
                            "day": 1,
                            "date": "2026-07-10",
                            "tips": "到达日。预留充足休整时间：落地→酒店办理入住约1.5h",
                        },
                    ),
                )
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_buggy_day3",
                        name="save_day_plan",
                        arguments={
                            "mode": "replace_existing",
                            "day": 3,
                            "date": "2026-07-12",
                            "tips": "离开日。精简至4个核心活动，节奏轻松",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return

            if call_count > 2:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已修正第1天行程。")
                yield LLMChunk(type=ChunkType.DONE)
                return

            # Iteration 2: verify LLM received the error and fix it
            for msg in messages:
                if msg.role == Role.TOOL and msg.tool_result is not None:
                    if msg.tool_result.error_code == "INVALID_ARGUMENTS":
                        error_seen_in_context = True

            assert error_seen_in_context, (
                "LLM should have received INVALID_ARGUMENTS error from iteration 1"
            )

            # LLM retries with correct calls including activities
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_fixed_day1",
                    name="save_day_plan",
                    arguments={
                        "mode": "replace_existing",
                        "day": 1,
                        "date": "2026-07-10",
                        "tips": "到达日休整",
                        "activities": [
                            {
                                "name": "羽田机场到达",
                                "location": {"name": "羽田机场", "lat": 35.5483, "lng": 139.778},
                                "start_time": "13:00",
                                "end_time": "14:30",
                                "category": "transport",
                                "cost": 55,
                            },
                            {
                                "name": "新宿御苑",
                                "location": {"name": "新宿御苑", "lat": 35.6852, "lng": 139.7101},
                                "start_time": "15:30",
                                "end_time": "17:00",
                                "category": "park",
                                "cost": 50,
                            },
                        ],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "优化第1天和第3天的行程"},
        )
        assert resp.status_code == 200
        events = await _collect_sse(resp)

        tool_error_events = [
            e
            for e in events
            if e.get("type") == "tool_result"
            and e.get("tool_result", {}).get("status") == "error"
        ]

        assert call_count == 3, f"Expected 3 LLM iterations, got {call_count}"
        assert error_seen_in_context, "Error should have been injected into LLM context"

        invalid_args_errors = [
            e
            for e in tool_error_events
            if e.get("tool_result", {}).get("error_code") == "INVALID_ARGUMENTS"
        ]
        assert len(invalid_args_errors) >= 2, (
            f"Expected at least 2 INVALID_ARGUMENTS errors for missing activities, "
            f"got {len(invalid_args_errors)}"
        )

        for err_event in invalid_args_errors:
            error_msg = err_event["tool_result"].get("error", "")
            assert "activities" in error_msg, (
                f"Error message should mention 'activities', got: {error_msg}"
            )

        tool_success_events = [
            e
            for e in events
            if e.get("type") == "tool_result"
            and e.get("tool_result", {}).get("status") == "success"
            and e.get("tool_result", {}).get("tool_call_id") == "tc_fixed_day1"
        ]
        assert len(tool_success_events) >= 1, "Expected the corrected save_day_plan call to succeed"

        assert plan.daily_plans[0].activities[0].name == "羽田机场到达"
