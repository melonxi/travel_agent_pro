# backend/tests/test_worker_prompt.py
import pytest

from agent.phase5.worker_prompt import (
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


def test_build_shared_prefix_contains_fallback_guardrails():
    plan = _make_plan()
    prefix = build_shared_prefix(plan)
    assert "有限次补救" in prefix
    assert "保守版 DayPlan" in prefix
    assert "不得编造具体营业时间" in prefix
    assert "写入 notes" in prefix


def test_build_shared_prefix_prefers_submit_tool_handoff():
    plan = _make_plan()
    prefix = build_shared_prefix(plan)

    assert "submit_day_plan_candidate" in prefix
    assert "不要在自然语言正文中输出完整 DayPlan JSON" in prefix
    assert "不会直接写入最终行程状态" in prefix
    assert "最后一条消息" not in prefix


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
    assert "调用 `submit_day_plan_candidate` 提交候选 DayPlan" in suffix
    assert "最后输出 JSON" not in suffix


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


class TestDayTaskConstraints:
    def test_day_task_has_constraint_fields(self):
        task = DayTask(
            day=1, date="2026-05-01", skeleton_slice={}, pace="balanced",
            locked_pois=["浅草寺"],
            candidate_pois=["上野公園"],
            forbidden_pois=["明治神宫"],
            area_cluster=["浅草", "上野"],
        )
        assert task.locked_pois == ["浅草寺"]
        assert task.forbidden_pois == ["明治神宫"]
        assert task.area_cluster == ["浅草", "上野"]

    def test_day_task_defaults_empty(self):
        task = DayTask(day=1, date="2026-05-01", skeleton_slice={}, pace="balanced")
        assert task.locked_pois == []
        assert task.forbidden_pois == []
        assert task.candidate_pois == []
        assert task.area_cluster == []
        assert task.mobility_envelope == {}
        assert task.fallback_slots == []
        assert task.date_role == "full_day"
        assert task.repair_hints == []

    def test_suffix_contains_constraint_block(self):
        task = DayTask(
            day=2, date="2026-05-02",
            skeleton_slice={"area": "浅草/上野", "theme": "传统文化"},
            pace="balanced",
            locked_pois=["浅草寺"],
            candidate_pois=["仲见世商店街", "上野公園"],
            forbidden_pois=["明治神宫", "涩谷Sky"],
            area_cluster=["浅草", "上野"],
            mobility_envelope={"max_cross_area_hops": 1, "max_transit_leg_min": 35},
        )
        suffix = build_day_suffix(task)
        assert "浅草寺" in suffix
        assert "明治神宫" in suffix
        assert "禁止" in suffix
        assert "候选" in suffix or "允许" in suffix
        assert "35" in suffix

    def test_suffix_contains_repair_hints(self):
        task = DayTask(
            day=1, date="2026-05-01", skeleton_slice={}, pace="balanced",
            repair_hints=["Day 1 时间冲突：A→B 间隔不足"],
        )
        suffix = build_day_suffix(task)
        assert "修复要求" in suffix or "修复" in suffix
        assert "时间冲突" in suffix

    def test_suffix_contains_arrival_day_note(self):
        task = DayTask(
            day=1, date="2026-05-01", skeleton_slice={}, pace="balanced",
            date_role="arrival_day",
        )
        suffix = build_day_suffix(task)
        assert "到达日" in suffix

    def test_suffix_contains_departure_day_note(self):
        task = DayTask(
            day=3, date="2026-05-03", skeleton_slice={}, pace="balanced",
            date_role="departure_day",
        )
        suffix = build_day_suffix(task)
        assert "离开日" in suffix


class TestSplitExtractsNewFields:
    def test_extracts_locked_and_candidate_pois(self):
        plan = _make_plan()
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{
            "id": "plan_A", "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草", "上野"],
                    "theme": "传统文化",
                    "locked_pois": ["浅草寺"],
                    "candidate_pois": ["上野公園", "仲见世商店街"],
                    "core_activities": ["寺庙", "散步"],
                },
            ],
        }]
        tasks = split_skeleton_to_day_tasks(plan.skeleton_plans[0], plan)
        assert tasks[0].locked_pois == ["浅草寺"]
        assert tasks[0].candidate_pois == ["上野公園", "仲见世商店街"]
        assert tasks[0].area_cluster == ["浅草", "上野"]

    def test_missing_new_fields_default_empty(self):
        plan = _make_plan()
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{
            "id": "plan_A", "name": "平衡版",
            "days": [{"area": "新宿", "theme": "购物"}],
        }]
        tasks = split_skeleton_to_day_tasks(plan.skeleton_plans[0], plan)
        assert tasks[0].locked_pois == []
        assert tasks[0].candidate_pois == []
        assert tasks[0].area_cluster == []


def test_dayplan_schema_has_category_enum_and_structural_errors():
    plan = _make_plan()
    prefix = build_shared_prefix(plan)
    assert "枚举之一" in prefix
    assert "常见结构错误" in prefix
    assert "location" in prefix


def test_build_shared_prefix_contains_role():
    plan = _make_plan()
    prefix = build_shared_prefix(plan)
    assert "单日行程落地规划师" in prefix
    assert "完成优于完美" in prefix
    assert "无用户交互" in prefix
    assert "forbidden_pois" in prefix
    assert "唯一合法路径" in prefix


def test_build_shared_prefix_no_soul_md():
    plan = _make_plan()
    prefix = build_shared_prefix(plan)
    assert "一次只问一个问题" not in prefix
    assert "提供 2-3 个选项" not in prefix


def test_build_shared_prefix_stable_ordering():
    """Same plan object produces identical output (stable sorted fields)."""
    plan1 = _make_plan()
    plan2 = _make_plan()
    assert build_shared_prefix(plan1) == build_shared_prefix(plan2)


def test_build_shared_prefix_excludes_soft_constraints():
    plan = _make_plan()
    plan.constraints = [
        Constraint(type="hard", description="不去迪士尼"),
        Constraint(type="soft", description="尽量住民宿"),
    ]
    prefix = build_shared_prefix(plan)
    assert "不去迪士尼" in prefix
    assert "尽量住民宿" not in prefix


def test_day_task_new_fields_default():
    task = DayTask(day=1, date="2026-05-01", skeleton_slice={}, pace="balanced")
    assert task.day_budget is None
    assert task.day_constraints == []
    assert task.arrival_time is None
    assert task.departure_time is None


def test_core_activities_labeled_as_directional():
    task = DayTask(
        day=1, date="2026-05-01",
        skeleton_slice={"area": "新宿", "core_activities": ["购物", "美食"]},
        pace="balanced",
    )
    suffix = build_day_suffix(task)
    assert "方向性活动线索" in suffix
    assert "仅供参考" in suffix


def test_suffix_contains_arrival_departure_day():
    task = DayTask(
        day=1, date="2026-05-01", skeleton_slice={}, pace="balanced",
        date_role="arrival_departure_day",
        arrival_time="10:00",
        departure_time="18:00",
    )
    suffix = build_day_suffix(task)
    assert "到达+离开日" in suffix
    assert "10:00" in suffix
    assert "18:00" in suffix
