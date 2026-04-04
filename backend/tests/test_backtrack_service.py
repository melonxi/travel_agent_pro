# backend/tests/test_backtrack_service.py
from __future__ import annotations

import pytest

from phase.backtrack import BacktrackService
from state.models import (
    Accommodation,
    DateRange,
    DayPlan,
    TravelPlanState,
)


def _make_plan(phase: int = 5) -> TravelPlanState:
    """Create a populated plan at the given phase for testing."""
    return TravelPlanState(
        session_id="test-session",
        phase=phase,
        destination="Tokyo",
        destination_candidates=[{"name": "Tokyo"}, {"name": "Osaka"}],
        dates=DateRange(start="2025-08-01", end="2025-08-05"),
        accommodation=Accommodation(area="Shinjuku", hotel="Hotel A"),
        daily_plans=[
            DayPlan(day=1, date="2025-08-01"),
            DayPlan(day=2, date="2025-08-02"),
        ],
    )


class TestBacktrackService:
    def setup_method(self) -> None:
        self.service = BacktrackService()

    def test_normal_backtrack_phase_5_to_3(self) -> None:
        """正常回退 phase 5 → 3：phase 改变、history 记录、下游清除。"""
        plan = _make_plan(phase=5)
        self.service.execute(
            plan, to_phase=3, reason="日期变更", snapshot_path="/snap/1"
        )

        assert plan.phase == 3
        assert len(plan.backtrack_history) == 1
        event = plan.backtrack_history[0]
        assert event.from_phase == 5
        assert event.to_phase == 3
        assert event.reason == "日期变更"
        assert event.snapshot_path == "/snap/1"

        # phase 3 清除: dates, accommodation, daily_plans
        assert plan.dates is None
        assert plan.accommodation is None
        assert plan.daily_plans == []

        # destination 和 destination_candidates 保留
        assert plan.destination == "Tokyo"
        assert len(plan.destination_candidates) == 2

    def test_illegal_backtrack_same_phase(self) -> None:
        """非法回退：to_phase == plan.phase 抛出 ValueError。"""
        plan = _make_plan(phase=3)
        with pytest.raises(ValueError, match="只能回退到更早的阶段"):
            self.service.execute(
                plan, to_phase=3, reason="no-op", snapshot_path="/snap/x"
            )

    def test_illegal_backtrack_forward(self) -> None:
        """非法回退：to_phase > plan.phase 抛出 ValueError。"""
        plan = _make_plan(phase=3)
        with pytest.raises(ValueError, match="只能回退到更早的阶段"):
            self.service.execute(
                plan, to_phase=5, reason="forward", snapshot_path="/snap/x"
            )

    def test_backtrack_to_phase_2_clears_destination(self) -> None:
        """回退到 phase 2 时 destination 被清除。"""
        plan = _make_plan(phase=5)
        self.service.execute(
            plan, to_phase=2, reason="重新选目的地", snapshot_path="/snap/2"
        )

        assert plan.phase == 2
        assert plan.destination is None
        assert plan.dates is None
        assert plan.accommodation is None
        assert plan.daily_plans == []

        # destination_candidates 保留（不在 phase 2 的下游列表中）
        assert len(plan.destination_candidates) == 2

    def test_backtrack_to_phase_1_clears_all(self) -> None:
        """回退到 phase 1 时所有下游字段被清除。"""
        plan = _make_plan(phase=5)
        self.service.execute(
            plan, to_phase=1, reason="从头开始", snapshot_path="/snap/3"
        )

        assert plan.phase == 1
        assert plan.destination is None
        assert plan.destination_candidates == []
        assert plan.dates is None
        assert plan.accommodation is None
        assert plan.daily_plans == []

    def test_backtrack_to_phase_4_clears_accommodation_and_daily_plans(self) -> None:
        """回退到 phase 4 时清除 accommodation 和 daily_plans，保留 dates 和 destination。"""
        plan = _make_plan(phase=5)
        self.service.execute(plan, to_phase=4, reason="换酒店", snapshot_path="/snap/4")

        assert plan.phase == 4
        # phase 4 下游: accommodation, daily_plans
        assert plan.accommodation is None
        assert plan.daily_plans == []

        # 保留
        assert plan.destination == "Tokyo"
        assert len(plan.destination_candidates) == 2
        assert plan.dates is not None
        assert plan.dates.start == "2025-08-01"
