from __future__ import annotations

from agent.execution.repair_hints import (
    RepairHintOutcome,
    build_phase2_state_repair_message,
    build_phase3_daily_state_repair_message,
)


class _Phase2Plan:
    destination = "成都"
    phase2_step = "brief"
    trip_brief = None


class _Dates:
    total_days = 3


class _Phase3Plan:
    dates = _Dates()
    daily_plans = []


def test_phase2_repair_returns_key_without_mutating_used_set():
    used: set[str] = set()

    outcome = build_phase2_state_repair_message(
        plan=_Phase2Plan(),
        current_phase=2,
        assistant_text="这是一次完整的旅行画像说明，包含偏好、预算、日期和旅行目标。",
        repair_hints_used=used,
    )

    assert isinstance(outcome, RepairHintOutcome)
    assert outcome.key == "p2_brief"
    assert "trip_brief" in outcome.message
    assert used == set()


def test_phase2_repair_respects_already_used_keys():
    used = {"p2_brief", "p2_brief_retry"}

    outcome = build_phase2_state_repair_message(
        plan=_Phase2Plan(),
        current_phase=2,
        assistant_text="这是一次完整的旅行画像说明，包含偏好、预算、日期和旅行目标。",
        repair_hints_used=used,
    )

    assert outcome is None
    assert used == {"p2_brief", "p2_brief_retry"}


def test_phase3_daily_repair_returns_key_without_mutating_used_set():
    used: set[str] = set()

    outcome = build_phase3_daily_state_repair_message(
        plan=_Phase3Plan(),
        current_phase=3,
        assistant_text="第 1 天 09:00 出发安排景点，下午继续活动，晚上安排餐厅。",
        repair_hints_used=used,
    )

    assert isinstance(outcome, RepairHintOutcome)
    assert outcome.key == "p3_daily"
    assert "daily_plans" in outcome.message
    assert used == set()


def test_phase3_daily_repair_respects_already_used_key():
    used = {"p3_daily"}

    outcome = build_phase3_daily_state_repair_message(
        plan=_Phase3Plan(),
        current_phase=3,
        assistant_text="第 1 天 09:00 出发安排景点，下午继续活动，晚上安排餐厅。",
        repair_hints_used=used,
    )

    assert outcome is None
    assert used == {"p3_daily"}
