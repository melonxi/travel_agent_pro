# backend/tests/test_parallel_phase3_integration.py
"""Integration tests for Phase 3 parallel orchestrator mode.

Uses mock LLM that returns pre-built DayPlan JSON to verify
the end-to-end flow: split → spawn → collect → validate → write.
"""

import json
import pytest

from agent.phase3.orchestrator import Phase3Orchestrator
from agent.types import ToolResult
from config import Phase3ParallelConfig
from llm.types import ChunkType, LLMChunk
from state.models import (
    TravelPlanState,
    DateRange,
    Travelers,
    Accommodation,
    Budget,
)


def _make_plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="test-integration")
    plan.phase = 3
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.travelers = Travelers(adults=2)
    plan.trip_brief = {"goal": "文化探索", "pace": "balanced", "departure_city": "上海"}
    plan.accommodation = Accommodation(area="新宿", hotel="新宿华盛顿酒店")
    plan.budget = Budget(total=30000, currency="CNY")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [
        {
            "id": "plan_A",
            "name": "平衡版",
            "days": [
                {
                    "area": "新宿/原宿",
                    "theme": "潮流文化",
                    "core_activities": ["明治神宫"],
                    "fatigue": "低",
                },
                {
                    "area": "浅草/上野",
                    "theme": "传统文化",
                    "core_activities": ["浅草寺"],
                    "fatigue": "中等",
                },
                {
                    "area": "涩谷",
                    "theme": "购物",
                    "core_activities": ["涩谷十字路口"],
                    "fatigue": "中等",
                },
            ],
        }
    ]
    return plan


def _make_dayplan_json(day: int, date: str, name: str) -> str:
    return json.dumps(
        {
            "day": day,
            "date": date,
            "notes": f"Day {day} test",
            "activities": [
                {
                    "name": name,
                    "location": {"name": name, "lat": 35.0, "lng": 139.0},
                    "start_time": "09:00",
                    "end_time": "11:00",
                    "category": "activity",
                    "cost": 1000,
                    "transport_from_prev": "地铁",
                    "transport_duration_min": 15,
                    "notes": "",
                }
            ],
        },
        ensure_ascii=False,
    )


class MockLLM:
    """Mock LLM that returns DayPlan JSON based on the day number in the prompt."""

    def __init__(self, day_responses: dict[int, str]):
        self._day_responses = day_responses

    async def chat(self, messages, **kwargs):
        # Extract day number from user message (day_suffix is in messages[1])
        day_msg = (
            messages[1].content if len(messages) > 1 else (messages[0].content or "")
        )
        day_num = 1
        for d in range(1, 20):
            if f"第 {d} 天" in day_msg:
                day_num = d
                break

        text = self._day_responses.get(
            day_num, '{"day": 0, "date": "", "activities": []}'
        )

        yield LLMChunk(type=ChunkType.TEXT_DELTA, content=text)
        yield LLMChunk(type=ChunkType.DONE)

    async def count_tokens(self, messages):
        return 100

    async def get_context_window(self):
        return 200000


class MockToolEngine:
    """Mock ToolEngine that returns empty results."""

    def get_tool(self, name):
        return None

    async def execute_batch(self, calls):
        return [
            ToolResult(tool_call_id=tc.id, status="success", data={}) for tc in calls
        ]


@pytest.mark.asyncio
async def test_parallel_happy_path():
    """All 3 days succeed → daily_plans has 3 entries."""
    plan = _make_plan()
    llm = MockLLM(
        {
            1: _make_dayplan_json(1, "2026-05-01", "明治神宫"),
            2: _make_dayplan_json(2, "2026-05-02", "浅草寺"),
            3: _make_dayplan_json(3, "2026-05-03", "涩谷十字路口"),
        }
    )
    tool_engine = MockToolEngine()
    config = Phase3ParallelConfig(enabled=True, max_workers=3)

    orch = Phase3Orchestrator(
        plan=plan, llm=llm, tool_engine=tool_engine, config=config
    )

    chunks = []
    async for chunk in orch.run():
        chunks.append(chunk)

    # Orchestrator collects results in final_dayplans; plan.daily_plans stays empty
    assert len(orch.final_dayplans) == 3
    assert orch.final_dayplans[0]["day"] == 1
    assert orch.final_dayplans[1]["day"] == 2
    assert orch.final_dayplans[2]["day"] == 3
    assert plan.daily_plans == []

    # DONE is now AgentLoop's responsibility; orchestrator must not emit it
    done_chunks = [c for c in chunks if c.type == ChunkType.DONE]
    assert len(done_chunks) == 0

    progress_chunks = [
        c
        for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    # Sanity: must emit at least one parallel_progress chunk per worker.
    assert len(progress_chunks) >= 3

    # Final progress chunk must carry activity_count for every done worker.
    last_workers = progress_chunks[-1].agent_status["workers"]
    for w in last_workers:
        if w["status"] == "done":
            assert w["activity_count"] is not None, (
                f"day {w['day']} missing activity_count"
            )
            assert (
                "theme" in w
            )  # theme key must be present (value may be None if skeleton slice has no area/theme)


@pytest.mark.asyncio
async def test_parallel_detects_poi_duplicate():
    """Duplicate POI is rejected before handoff instead of being delivered."""
    plan = _make_plan()
    llm = MockLLM(
        {
            1: _make_dayplan_json(1, "2026-05-01", "浅草寺"),
            2: _make_dayplan_json(2, "2026-05-02", "浅草寺"),  # duplicate!
            3: _make_dayplan_json(3, "2026-05-03", "涩谷十字路口"),
        }
    )
    tool_engine = MockToolEngine()
    config = Phase3ParallelConfig(enabled=True, max_workers=3)

    orch = Phase3Orchestrator(
        plan=plan, llm=llm, tool_engine=tool_engine, config=config
    )

    chunks = []
    async for chunk in orch.run():
        chunks.append(chunk)

    # The first accepted owner and the non-conflicting day remain deliverable;
    # the blackboard-rejected duplicate never reaches final_dayplans.
    assert len(orch.final_dayplans) == 2
    assert (
        sum(
            1
            for dayplan in orch.final_dayplans
            for activity in dayplan.get("activities", [])
            if activity.get("name") == "浅草寺"
        )
        == 1
    )
    assert plan.daily_plans == []
