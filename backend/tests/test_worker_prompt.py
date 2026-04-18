# backend/tests/test_worker_prompt.py
import pytest

from agent.worker_prompt import (
    build_shared_prefix,
    build_day_suffix,
    DayTask,
    split_skeleton_to_day_tasks,
)
from state.models import (
    TravelPlanState,
    DateRange,
    Travelers,
    Accommodation,
    Preference,
    Constraint,
)


def _make_plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="test-session")
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-05")
    plan.travelers = Travelers(adults=2, children=0)
    plan.trip_brief = {
        "goal": "文化探索",
        "pace": "balanced",
        "departure_city": "上海",
    }
    plan.accommodation = Accommodation(area="新宿", hotel="新宿华盛顿酒店")
    plan.preferences = [Preference(key="must_do", value="浅草寺")]
    plan.constraints = [Constraint(type="hard", description="不去迪士尼")]
    return plan


def test_build_shared_prefix_contains_destination():
    plan = _make_plan()
    prefix = build_shared_prefix(plan)
    assert "东京" in prefix
    assert "新宿" in prefix
    assert "balanced" in prefix
    assert "浅草寺" in prefix
    assert "不去迪士尼" in prefix


def test_build_shared_prefix_stable_across_calls():
    """共享 prefix 应在多次调用间完全相同（KV-Cache 友好）。"""
    plan = _make_plan()
    prefix1 = build_shared_prefix(plan)
    prefix2 = build_shared_prefix(plan)
    assert prefix1 == prefix2


def test_build_day_suffix():
    task = DayTask(
        day=3,
        date="2026-05-03",
        skeleton_slice={
            "area": "浅草/上野",
            "theme": "文化体验",
            "core_activities": ["浅草寺", "上野公园"],
            "fatigue": "中等",
        },
        pace="balanced",
    )
    suffix = build_day_suffix(task)
    assert "第 3 天" in suffix
    assert "2026-05-03" in suffix
    assert "浅草/上野" in suffix
    assert "浅草寺" in suffix


def test_day_suffix_differs_per_day():
    """不同天的后缀必须不同。"""
    task_a = DayTask(
        day=1, date="2026-05-01", skeleton_slice={"area": "新宿"}, pace="balanced"
    )
    task_b = DayTask(
        day=2, date="2026-05-02", skeleton_slice={"area": "浅草"}, pace="balanced"
    )
    suffix_a = build_day_suffix(task_a)
    suffix_b = build_day_suffix(task_b)
    assert suffix_a != suffix_b


def test_split_skeleton_to_day_tasks():
    plan = _make_plan()
    skeleton = {
        "id": "plan_A",
        "days": [
            {"area": "新宿", "theme": "潮流"},
            {"area": "浅草", "theme": "文化"},
        ],
    }
    tasks = split_skeleton_to_day_tasks(skeleton, plan)
    assert len(tasks) == 2
    assert tasks[0].day == 1
    assert tasks[0].date == "2026-05-01"
    assert tasks[0].skeleton_slice["area"] == "新宿"
    assert tasks[0].pace == "balanced"
    assert tasks[1].day == 2
    assert tasks[1].date == "2026-05-02"


def test_split_skeleton_no_dates():
    """没有 dates 时应使用 day-N 格式。"""
    plan = _make_plan()
    plan.dates = None
    skeleton = {"days": [{"area": "A"}, {"area": "B"}]}
    tasks = split_skeleton_to_day_tasks(skeleton, plan)
    assert tasks[0].date == "day-1"
    assert tasks[1].date == "day-2"
