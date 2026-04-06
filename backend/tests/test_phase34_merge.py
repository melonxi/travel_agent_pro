# backend/tests/test_phase34_merge.py
"""
TDD tests for Phase 3+4 merge.
Phase 3 now covers both date/rhythm planning AND accommodation selection.
Phase 4 no longer exists as a separate phase.
"""
from __future__ import annotations

import pytest

from phase.router import PhaseRouter
from phase.prompts import PHASE_PROMPTS, PHASE_CONTROL_MODE
from state.models import (
    Accommodation,
    DateRange,
    DayPlan,
    TravelPlanState,
    _PHASE_DOWNSTREAM,
)
from tools.base import ToolDef, tool
from tools.engine import ToolEngine


# ---------------------------------------------------------------------------
# 1. PhaseRouter.infer_phase — Phase 4 no longer returned
# ---------------------------------------------------------------------------


class TestInferPhaseAfterMerge:
    @pytest.fixture
    def router(self):
        return PhaseRouter()

    def test_destination_only_returns_phase3(self, router):
        plan = TravelPlanState(session_id="s", destination="Tokyo")
        assert router.infer_phase(plan) == 3

    def test_destination_and_dates_still_phase3(self, router):
        """After merge: dates set but no accommodation → still phase 3."""
        plan = TravelPlanState(
            session_id="s",
            destination="Tokyo",
            dates=DateRange(start="2026-05-01", end="2026-05-05"),
        )
        assert router.infer_phase(plan) == 3

    def test_destination_dates_and_accommodation_goes_to_phase5(self, router):
        plan = TravelPlanState(
            session_id="s",
            destination="Tokyo",
            dates=DateRange(start="2026-05-01", end="2026-05-05"),
            accommodation=Accommodation(area="新宿"),
        )
        assert router.infer_phase(plan) == 5

    def test_infer_phase_never_returns_4(self, router):
        """Phase 4 should never be returned by infer_phase."""
        combos = [
            TravelPlanState(session_id="s"),
            TravelPlanState(session_id="s", destination="A"),
            TravelPlanState(session_id="s", destination="A",
                            dates=DateRange(start="2026-01-01", end="2026-01-03")),
            TravelPlanState(session_id="s", destination="A",
                            dates=DateRange(start="2026-01-01", end="2026-01-03"),
                            accommodation=Accommodation(area="X")),
            TravelPlanState(session_id="s", destination="A",
                            dates=DateRange(start="2026-01-01", end="2026-01-03"),
                            accommodation=Accommodation(area="X"),
                            daily_plans=[DayPlan(day=i, date=f"2026-01-0{i}") for i in range(1, 3)]),
        ]
        for plan in combos:
            assert router.infer_phase(plan) != 4, f"phase 4 returned for {plan}"


# ---------------------------------------------------------------------------
# 2. PHASE_PROMPTS and PHASE_CONTROL_MODE — no key 4
# ---------------------------------------------------------------------------


class TestPromptsAfterMerge:
    def test_phase4_not_in_prompts(self):
        assert 4 not in PHASE_PROMPTS

    def test_phase4_not_in_control_mode(self):
        assert 4 not in PHASE_CONTROL_MODE

    def test_phase3_prompt_covers_accommodation(self):
        """Merged phase 3 prompt must mention accommodation."""
        prompt = PHASE_PROMPTS[3]
        assert "住宿" in prompt

    def test_phase3_prompt_covers_dates(self):
        prompt = PHASE_PROMPTS[3]
        assert "日期" in prompt

    def test_remaining_phases_still_exist(self):
        for phase in [1, 3, 5, 7]:
            assert phase in PHASE_PROMPTS
            assert phase in PHASE_CONTROL_MODE


# ---------------------------------------------------------------------------
# 3. _PHASE_DOWNSTREAM — merged phase 3 clears dates+accommodation+daily_plans
# ---------------------------------------------------------------------------


class TestDownstreamAfterMerge:
    def test_phase4_not_in_downstream(self):
        assert 4 not in _PHASE_DOWNSTREAM

    def test_phase3_downstream_includes_accommodation(self):
        assert "accommodation" in _PHASE_DOWNSTREAM[3]

    def test_phase3_downstream_includes_dates(self):
        assert "dates" in _PHASE_DOWNSTREAM[3]

    def test_phase3_downstream_includes_daily_plans(self):
        assert "daily_plans" in _PHASE_DOWNSTREAM[3]


# ---------------------------------------------------------------------------
# 4. Tool phases — no tool should have phase 4
# ---------------------------------------------------------------------------


class TestToolPhasesAfterMerge:
    def test_tools_with_former_phase34_now_phase3(self):
        """Tools that were [3,4] should now be [3]."""
        # We test by creating tools and checking; the actual tool files
        # are validated by importing them.
        from tools.search_flights import make_search_flights_tool
        from tools.search_trains import make_search_trains_tool
        from tools.search_accommodations import make_search_accommodations_tool
        from config import ApiKeysConfig

        keys = ApiKeysConfig()

        flight_tool = make_search_flights_tool(keys)
        assert 4 not in flight_tool.phases
        assert 3 in flight_tool.phases

        train_tool = make_search_trains_tool(None)
        assert 4 not in train_tool.phases
        assert 3 in train_tool.phases

        accom_tool = make_search_accommodations_tool(keys)
        assert 4 not in accom_tool.phases
        assert 3 in accom_tool.phases

    def test_tools_with_former_phase45_now_phase35(self):
        """Tools that were [4,5] should now be [3,5]."""
        from tools.calculate_route import make_calculate_route_tool
        from tools.check_availability import make_check_availability_tool
        from tools.assemble_day_plan import make_assemble_day_plan_tool
        from config import ApiKeysConfig

        keys = ApiKeysConfig()

        route_tool = make_calculate_route_tool(keys)
        assert 4 not in route_tool.phases
        assert 3 in route_tool.phases
        assert 5 in route_tool.phases

        avail_tool = make_check_availability_tool(keys)
        assert 4 not in avail_tool.phases
        assert 3 in avail_tool.phases
        assert 5 in avail_tool.phases

        assemble_tool = make_assemble_day_plan_tool()
        assert 4 not in assemble_tool.phases
        assert 3 in assemble_tool.phases
        assert 5 in assemble_tool.phases

    def test_tools_with_former_phase345_now_phase35(self):
        """get_poi_info was [3,4,5], should now be [3,5]."""
        from tools.get_poi_info import make_get_poi_info_tool
        from config import ApiKeysConfig

        keys = ApiKeysConfig()
        poi_tool = make_get_poi_info_tool(keys)
        assert 4 not in poi_tool.phases
        assert 3 in poi_tool.phases
        assert 5 in poi_tool.phases

    def test_universal_tools_no_phase4(self):
        """xiaohongshu_search and update_plan_state should not include phase 4."""
        from tools.xiaohongshu_search import make_xiaohongshu_search_tool
        from tools.update_plan_state import make_update_plan_state_tool
        from config import XhsConfig

        xhs_tool = make_xiaohongshu_search_tool(XhsConfig())
        assert 4 not in xhs_tool.phases

        plan = TravelPlanState(session_id="s")
        ups_tool = make_update_plan_state_tool(plan)
        assert 4 not in ups_tool.phases

    def test_engine_returns_no_tools_for_phase4(self):
        """ToolEngine.get_tools_for_phase(4) should return empty list."""
        @tool(name="t1", description="d", phases=[3], parameters={})
        async def t1():
            return {}

        @tool(name="t2", description="d", phases=[5], parameters={})
        async def t2():
            return {}

        engine = ToolEngine()
        engine.register(t1)
        engine.register(t2)
        assert engine.get_tools_for_phase(4) == []


# ---------------------------------------------------------------------------
# 5. Backtrack — phase 3 clears accommodation now
# ---------------------------------------------------------------------------


class TestBacktrackAfterMerge:
    def test_backtrack_to_phase3_clears_dates_and_accommodation(self):
        from phase.backtrack import BacktrackService

        plan = TravelPlanState(
            session_id="s",
            phase=5,
            destination="Tokyo",
            dates=DateRange(start="2026-05-01", end="2026-05-05"),
            accommodation=Accommodation(area="新宿"),
            daily_plans=[DayPlan(day=1, date="2026-05-01")],
        )
        BacktrackService().execute(plan, to_phase=3, reason="改日期", snapshot_path="")

        assert plan.phase == 3
        assert plan.dates is None
        assert plan.accommodation is None
        assert plan.daily_plans == []
        assert plan.destination == "Tokyo"  # preserved


# ---------------------------------------------------------------------------
# 6. from_dict phase 4 migration — old sessions that saved phase=4
# ---------------------------------------------------------------------------


class TestPhase4MigrationInFromDict:
    def test_from_dict_migrates_phase4_to_phase3(self):
        """Old sessions saved with phase=4 should load as phase=3."""
        raw = {
            "session_id": "old-session",
            "phase": 4,
            "destination": "Tokyo",
            "dates": {"start": "2026-05-01", "end": "2026-05-05"},
        }
        plan = TravelPlanState.from_dict(raw)
        assert plan.phase == 3
