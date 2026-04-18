# backend/tests/test_loop_phase5_routing.py
from agent.loop import AgentLoop
from config import Phase5ParallelConfig


class TestPhase5Routing:
    def test_should_use_parallel_when_enabled(self):
        """Phase 5 + 并行启用 + daily_plans 为空 → 应使用并行模式。"""
        from state.models import TravelPlanState, DateRange, Accommodation

        plan = TravelPlanState(session_id="test-routing")
        plan.phase = 5
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{"id": "plan_A", "days": [{}, {}, {}]}]
        plan.accommodation = Accommodation(area="新宿")
        plan.daily_plans = []

        config = Phase5ParallelConfig(enabled=True)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is True

    def test_should_not_use_parallel_when_disabled(self):
        from state.models import TravelPlanState, DateRange, Accommodation

        plan = TravelPlanState(session_id="test-routing")
        plan.phase = 5
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{"id": "plan_A", "days": [{}, {}, {}]}]
        plan.accommodation = Accommodation(area="新宿")
        plan.daily_plans = []

        config = Phase5ParallelConfig(enabled=False)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is False

    def test_should_not_use_parallel_when_plans_exist(self):
        """daily_plans 已有数据 → 用户在修改，用串行模式。"""
        from state.models import TravelPlanState, DateRange, Accommodation, DayPlan

        plan = TravelPlanState(session_id="test-routing")
        plan.phase = 5
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{"id": "plan_A", "days": [{}, {}, {}]}]
        plan.accommodation = Accommodation(area="新宿")
        plan.daily_plans = [DayPlan(day=1, date="2026-05-01")]

        config = Phase5ParallelConfig(enabled=True)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is False

    def test_should_not_use_parallel_when_not_phase5(self):
        from state.models import TravelPlanState

        plan = TravelPlanState(session_id="test-routing")
        plan.phase = 3

        config = Phase5ParallelConfig(enabled=True)
        assert AgentLoop.should_use_parallel_phase5(plan, config) is False

    def test_should_not_use_parallel_when_plan_is_none(self):
        config = Phase5ParallelConfig(enabled=True)
        assert AgentLoop.should_use_parallel_phase5(None, config) is False

    def test_should_not_use_parallel_when_config_is_none(self):
        from state.models import TravelPlanState

        plan = TravelPlanState(session_id="test-routing")
        plan.phase = 5
        assert AgentLoop.should_use_parallel_phase5(plan, None) is False
