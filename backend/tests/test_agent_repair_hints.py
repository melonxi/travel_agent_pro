from __future__ import annotations

from agent.execution.repair_hints import (
    RepairHintOutcome,
    build_phase3_state_repair_message,
    build_phase5_state_repair_message,
)


class _Plan:
    destination = "成都"
    phase3_step = "brief"
    trip_brief = None


class _Dates:
    total_days = 3


class _Phase5Plan:
    dates = _Dates()
    daily_plans = []


def test_phase3_repair_returns_key_without_mutating_used_set():
    used: set[str] = set()

    outcome = build_phase3_state_repair_message(
        plan=_Plan(),
        current_phase=3,
        assistant_text="这是一次完整的旅行画像说明，包含偏好、预算、日期和旅行目标。",
        repair_hints_used=used,
    )

    assert isinstance(outcome, RepairHintOutcome)
    assert outcome.key == "p3_brief"
    assert "trip_brief" in outcome.message
    assert used == set()


def test_phase3_repair_respects_already_used_keys():
    used = {"p3_brief", "p3_brief_retry"}

    outcome = build_phase3_state_repair_message(
        plan=_Plan(),
        current_phase=3,
        assistant_text="这是一次完整的旅行画像说明，包含偏好、预算、日期和旅行目标。",
        repair_hints_used=used,
    )

    assert outcome is None
    assert used == {"p3_brief", "p3_brief_retry"}


def test_phase5_repair_returns_key_without_mutating_used_set():
    used: set[str] = set()

    outcome = build_phase5_state_repair_message(
        plan=_Phase5Plan(),
        current_phase=5,
        assistant_text="第 1 天 09:00 出发安排景点，下午继续活动，晚上安排餐厅。",
        repair_hints_used=used,
    )

    assert isinstance(outcome, RepairHintOutcome)
    assert outcome.key == "p5_daily"
    assert "daily_plans" in outcome.message
    assert used == set()


def test_phase5_repair_respects_already_used_key():
    used = {"p5_daily"}

    outcome = build_phase5_state_repair_message(
        plan=_Phase5Plan(),
        current_phase=5,
        assistant_text="第 1 天 09:00 出发安排景点，下午继续活动，晚上安排餐厅。",
        repair_hints_used=used,
    )

    assert outcome is None
    assert used == {"p5_daily"}
