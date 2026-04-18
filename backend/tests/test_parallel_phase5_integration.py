# backend/tests/test_parallel_phase5_integration.py
"""Integration tests for Phase 5 parallel orchestrator mode.

Uses mock LLM that returns pre-built DayPlan JSON to verify
the end-to-end flow: split → spawn → collect → validate → write.
"""

import json
import pytest

from agent.orchestrator import Phase5Orchestrator
from agent.types import ToolResult
from config import Phase5ParallelConfig
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
    plan.phase = 5
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
        # Extract day number from system message
        system_msg = messages[0].content or ""
        day_num = 1
        for d in range(1, 20):
            if f"第 {d} 天" in system_msg:
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
    config = Phase5ParallelConfig(enabled=True, max_workers=3)

    orch = Phase5Orchestrator(
        plan=plan, llm=llm, tool_engine=tool_engine, config=config
    )

    chunks = []
    async for chunk in orch.run():
        chunks.append(chunk)

    # Verify daily_plans were written
    assert len(plan.daily_plans) == 3
    assert plan.daily_plans[0].day == 1
    assert plan.daily_plans[1].day == 2
    assert plan.daily_plans[2].day == 3

    # Verify DONE chunk was emitted
    done_chunks = [c for c in chunks if c.type == ChunkType.DONE]
    assert len(done_chunks) == 1


@pytest.mark.asyncio
async def test_parallel_detects_poi_duplicate():
    """Duplicate POI across days should be detected in global validation."""
    plan = _make_plan()
    llm = MockLLM(
        {
            1: _make_dayplan_json(1, "2026-05-01", "浅草寺"),
            2: _make_dayplan_json(2, "2026-05-02", "浅草寺"),  # duplicate!
            3: _make_dayplan_json(3, "2026-05-03", "涩谷十字路口"),
        }
    )
    tool_engine = MockToolEngine()
    config = Phase5ParallelConfig(enabled=True, max_workers=3)

    orch = Phase5Orchestrator(
        plan=plan, llm=llm, tool_engine=tool_engine, config=config
    )

    chunks = []
    async for chunk in orch.run():
        chunks.append(chunk)

    # Plans still written (validation is advisory for now)
    assert len(plan.daily_plans) == 3

    # Check that summary mentions the duplicate issue specifically
    text_chunks = [c for c in chunks if c.type == ChunkType.TEXT_DELTA]
    summary = "".join(c.content or "" for c in text_chunks)
    assert "浅草寺" in summary
    assert "出现在多天" in summary  # confirms dedup logic, not just POI name
