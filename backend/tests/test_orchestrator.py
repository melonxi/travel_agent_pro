# backend/tests/test_orchestrator.py
import pytest

from agent.orchestrator import Phase5Orchestrator, GlobalValidationIssue
from agent.worker_prompt import DayTask
from state.models import (
    TravelPlanState,
    DateRange,
    Travelers,
    Accommodation,
    Budget,
    DayPlan,
    Activity,
    Location,
)


def _make_plan_with_skeleton() -> TravelPlanState:
    plan = TravelPlanState(session_id="test-orch")
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
                    "core_activities": ["明治神宫", "竹下通"],
                    "fatigue": "低",
                },
                {
                    "area": "浅草/上野",
                    "theme": "传统文化",
                    "core_activities": ["浅草寺", "上野公园"],
                    "fatigue": "中等",
                },
                {
                    "area": "涩谷/银座",
                    "theme": "购物",
                    "core_activities": ["涩谷十字路口", "银座六丁目"],
                    "fatigue": "中等",
                },
            ],
        }
    ]
    return plan


class TestSplitTasks:
    def test_split_produces_correct_day_count(self):
        plan = _make_plan_with_skeleton()
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        assert len(tasks) == 3

    def test_split_assigns_correct_dates(self):
        plan = _make_plan_with_skeleton()
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        assert tasks[0].date == "2026-05-01"
        assert tasks[1].date == "2026-05-02"
        assert tasks[2].date == "2026-05-03"

    def test_split_preserves_skeleton_data(self):
        plan = _make_plan_with_skeleton()
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        assert tasks[0].skeleton_slice["area"] == "新宿/原宿"
        assert tasks[1].skeleton_slice["area"] == "浅草/上野"

    def test_split_raises_if_no_skeleton(self):
        plan = _make_plan_with_skeleton()
        plan.selected_skeleton_id = None
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        with pytest.raises(ValueError, match="未找到已选骨架"):
            orch._split_tasks()


class TestGlobalValidation:
    def _make_dayplan_dict(self, day: int, date: str, activities: list[dict]) -> dict:
        return {"day": day, "date": date, "notes": "", "activities": activities}

    def _make_activity(self, name: str, cost: float = 0) -> dict:
        return {
            "name": name,
            "location": {"name": name, "lat": 35.0, "lng": 139.0},
            "start_time": "09:00",
            "end_time": "10:00",
            "category": "activity",
            "cost": cost,
        }

    def test_no_issues_when_valid(self):
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_activity("A", 5000)]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("B", 5000)]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C", 5000)]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        assert len(issues) == 0

    def test_detects_poi_duplicate(self):
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_activity("浅草寺")]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("浅草寺")]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C")]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        poi_issues = [i for i in issues if i.issue_type == "poi_duplicate"]
        assert len(poi_issues) >= 1

    def test_detects_budget_overrun(self):
        plan = _make_plan_with_skeleton()
        plan.budget = Budget(total=100, currency="CNY")
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_activity("A", 50)]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("B", 50)]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C", 50)]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        budget_issues = [i for i in issues if i.issue_type == "budget_overrun"]
        assert len(budget_issues) >= 1
