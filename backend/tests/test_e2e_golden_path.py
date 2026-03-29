# backend/tests/test_e2e_golden_path.py
"""
Golden-path end-to-end test: simulates a complete conversation
from "五一去东京" through all phases to summary generation.
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent.types import ToolCall
from llm.types import ChunkType, LLMChunk
from state.models import (
    Accommodation,
    Activity,
    Budget,
    DateRange,
    DayPlan,
    Location,
    TravelPlanState,
    Travelers,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text_chunks(*texts: str) -> list[LLMChunk]:
    """Build a list of TEXT_DELTA chunks + DONE from string fragments."""
    chunks = [LLMChunk(type=ChunkType.TEXT_DELTA, content=t) for t in texts]
    chunks.append(LLMChunk(type=ChunkType.DONE))
    return chunks


def _tool_then_text(
    tool_call: ToolCall, *texts: str
) -> tuple[list[LLMChunk], list[LLMChunk]]:
    """Return (first-round-chunks-with-tool, second-round-chunks-with-text).

    The caller should wire these into a ``call_count``-based mock so that the
    first LLM round yields the tool call and the second yields final text.
    """
    first = [
        LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tool_call),
        LLMChunk(type=ChunkType.DONE),
    ]
    second = _text_chunks(*texts)
    return first, second


async def _collect_sse(response: httpx.Response) -> list[dict]:
    """Read all SSE 'data:' lines from a streaming response and parse them."""
    events: list[dict] = []
    raw = response.text
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload:
                events.append(json.loads(payload))
    return events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    """Set required env vars and override data_dir to a temp directory."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # Use a temp directory so StateManager does not touch the real filesystem
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def app():
    """Create a fresh FastAPI app for each test."""
    from main import create_app

    return create_app(config_path="__nonexistent__.yaml")


@pytest.fixture
def sessions(app):
    """Return the internal sessions dict from the app closure.

    ``create_app`` stores ``sessions`` in its closure and every route
    handler accesses it via the same reference.  We locate it through the
    ``create_session`` route handler's ``__globals__`` dict which shares
    the closure namespace.
    """
    # All route functions (create_session, chat, …) are closures over the
    # same local variables.  FastAPI stores them in app.routes[].endpoint.
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        # The closure locals are available through __code__.co_freevars and
        # the cell contents.  However, the simplest reliable approach is to
        # look at the endpoint's closure cells.
        closure = getattr(endpoint, "__closure__", None)
        if closure is None:
            continue
        for cell in closure:
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if isinstance(val, dict):
                # sessions is the only dict[str, dict] in the closure.
                # Quick sanity: at this point it should be empty.
                return val
    # Fallback: if we cannot find it, tests will fail with a clear message
    pytest.fail("Could not locate 'sessions' dict from app closure")


# ---------------------------------------------------------------------------
# The golden-path test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_golden_path_tokyo_trip(app, sessions):
    """Simulate a full user journey: 五一去东京 → summary generation."""

    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        # ==================================================================
        # Step 0: Create a session
        # ==================================================================
        resp = await client.post("/api/sessions")
        assert resp.status_code == 200
        session_data = resp.json()
        session_id = session_data["session_id"]
        assert session_data["phase"] == 1

        # Grab the live plan object from sessions dict
        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        assert plan.phase == 1

        # ==================================================================
        # Step 1  Phase 1 → Needs gathering
        # "我想五一去东京玩5天，预算2万元，2个大人"
        #
        # apply_trip_facts will extract:
        #   destination = "东京"
        #   dates       = DateRange(2026-05-01, 2026-05-06)   (五一 + 5天)
        #   budget      = Budget(total=20000, currency="CNY")
        #
        # After extraction, PhaseRouter.infer_phase sees destination+dates
        # but no accommodation → phase should become 4 (skipping 2 & 3).
        # ==================================================================

        async def _agent_run_phase1(messages, phase, **kw):
            for chunk in _text_chunks(
                "好的！五一去东京5天，预算2万元。",
                "我来帮您规划行程，先确认一下住宿偏好。",
            ):
                yield chunk

        with patch.object(session["agent"], "run", side_effect=_agent_run_phase1):
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我想五一去东京玩5天，预算2万元，2个大人"},
            )
            assert resp.status_code == 200

        # Verify intake extraction
        assert plan.destination == "东京"
        assert plan.dates is not None
        assert plan.dates.start == "2026-05-01"
        assert plan.dates.end == "2026-05-06"
        assert plan.dates.total_days == 5
        assert plan.budget is not None
        assert plan.budget.total == 20000.0
        assert plan.budget.currency == "CNY"

        # Phase should have advanced past 1/2/3 because destination+dates
        # are already set.  infer_phase → 4 (no accommodation yet).
        assert plan.phase == 4, f"Expected phase 4, got {plan.phase}"

        # ==================================================================
        # Step 2  Phase 4 → Set accommodation
        #
        # The user says "住新宿".  We mock the agent to call
        # update_plan_state but since the real tool is wired to the plan,
        # we can just let it execute or set state directly.
        # For simplicity, we set accommodation directly on the plan object
        # before the chat call, then let the agent return text only.
        # ==================================================================

        # Manually set travelers (not extracted by regex in step 1)
        plan.travelers = Travelers(adults=2, children=0)

        # Now let's simulate the accommodation selection.
        # We directly set accommodation on the plan, then trigger phase
        # re-evaluation.
        plan.accommodation = Accommodation(area="新宿", hotel="新宿华盛顿酒店")
        from phase.router import PhaseRouter

        router = PhaseRouter()
        router.check_and_apply_transition(plan)

        # With destination, dates, accommodation set but no daily_plans:
        # infer_phase → 5
        assert plan.phase == 5, f"Expected phase 5, got {plan.phase}"

        # Also rebuild the agent since phase changed (mirrors real flow)
        # In real flow the agent is rebuilt on backtrack; for forward
        # transitions it keeps going.  For the test we just need the mock.

        # ==================================================================
        # Step 3  Phase 5 → Assemble day plans
        #
        # We simulate the agent calling assemble_day_plan for 5 days.
        # Since the real tool would need LLM interaction, we set daily_plans
        # directly on the plan object.
        # ==================================================================

        sample_activity = Activity(
            name="浅草寺",
            location=Location(lat=35.7148, lng=139.7967, name="浅草寺"),
            start_time="09:00",
            end_time="11:00",
            category="景点",
            cost=0,
        )

        for day_num in range(1, 6):
            day_date = f"2026-05-{day_num:02d}"
            plan.daily_plans.append(
                DayPlan(
                    day=day_num,
                    date=day_date,
                    activities=[sample_activity],
                    notes=f"第{day_num}天行程",
                )
            )

        router.check_and_apply_transition(plan)

        # 5 daily_plans for 5 total_days → infer_phase returns 7
        assert plan.phase == 7, f"Expected phase 7, got {plan.phase}"

        # ==================================================================
        # Step 4  Phase 7 → Generate summary
        #
        # Mock agent calling check_weather + generate_summary, then
        # returning final text.
        # ==================================================================

        call_count = 0

        async def _agent_run_phase7(messages, phase, **kw):
            nonlocal call_count
            # First round: tool call for check_weather
            # (The real agent loop drives multiple rounds, but here we
            #  mock the entire run() generator to emit the expected chunks.)
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_weather",
                    name="check_weather",
                    arguments={"destination": "东京", "date_range": "2026-05-01~2026-05-06"},
                ),
            )
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content="东京五一期间天气温暖，建议带轻便衣物。",
            )
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content="\n\n您的5天东京之旅已全部规划完成！祝旅途愉快！",
            )
            yield LLMChunk(type=ChunkType.DONE)

        with patch.object(session["agent"], "run", side_effect=_agent_run_phase7):
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "帮我生成最终的出行摘要"},
            )
            assert resp.status_code == 200

        # ==================================================================
        # Final assertions: the plan should be complete
        # ==================================================================
        assert plan.phase == 7
        assert plan.destination == "东京"
        assert plan.dates.start == "2026-05-01"
        assert plan.dates.end == "2026-05-06"
        assert plan.budget.total == 20000.0
        assert plan.travelers.adults == 2
        assert plan.accommodation.area == "新宿"
        assert len(plan.daily_plans) == 5
        assert plan.daily_plans[0].activities[0].name == "浅草寺"

        # Verify via the GET /api/plan endpoint
        resp = await client.get(f"/api/plan/{session_id}")
        assert resp.status_code == 200
        plan_dict = resp.json()
        assert plan_dict["phase"] == 7
        assert plan_dict["destination"] == "东京"
        assert plan_dict["dates"]["start"] == "2026-05-01"
        assert plan_dict["dates"]["end"] == "2026-05-06"
        assert plan_dict["budget"]["total"] == 20000.0
        assert plan_dict["accommodation"]["area"] == "新宿"
        assert len(plan_dict["daily_plans"]) == 5
