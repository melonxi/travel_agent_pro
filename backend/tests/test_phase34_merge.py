# backend/tests/test_phase34_merge.py
"""Regression tests for the current Phase 1/2/3/4 routing surface."""

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
# 1. PhaseRouter.infer_phase — current routing returns 1/2/3/4
# ---------------------------------------------------------------------------


class TestInferPhaseAfterMerge:
    @pytest.fixture
    def router(self):
        return PhaseRouter()

    def test_destination_only_returns_phase2(self, router):
        plan = TravelPlanState(session_id="s", destination="Tokyo")
        assert router.infer_phase(plan) == 2

    def test_destination_and_dates_still_phase2(self, router):
        """After merge: dates set but no accommodation → still phase 2."""
        plan = TravelPlanState(
            session_id="s",
            destination="Tokyo",
            dates=DateRange(start="2026-05-01", end="2026-05-05"),
        )
        assert router.infer_phase(plan) == 2

    def test_destination_dates_and_accommodation_goes_to_phase3(self, router):
        plan = TravelPlanState(
            session_id="s",
            destination="Tokyo",
            dates=DateRange(start="2026-05-01", end="2026-05-05"),
            selected_skeleton_id="balanced",
            accommodation=Accommodation(area="新宿"),
        )
        assert router.infer_phase(plan) == 3

    def test_infer_phase_never_returns_legacy_ids(self, router):
        """Legacy phase ids should never be returned by infer_phase."""
        combos = [
            TravelPlanState(session_id="s"),
            TravelPlanState(session_id="s", destination="A"),
            TravelPlanState(
                session_id="s",
                destination="A",
                dates=DateRange(start="2026-01-01", end="2026-01-03"),
            ),
            TravelPlanState(
                session_id="s",
                destination="A",
                dates=DateRange(start="2026-01-01", end="2026-01-03"),
                selected_skeleton_id="balanced",
                accommodation=Accommodation(area="X"),
            ),
            TravelPlanState(
                session_id="s",
                destination="A",
                dates=DateRange(start="2026-01-01", end="2026-01-03"),
                selected_skeleton_id="balanced",
                accommodation=Accommodation(area="X"),
                daily_plans=[DayPlan(day=i, date=f"2026-01-0{i}") for i in range(1, 3)],
            ),
        ]
        for plan in combos:
            assert router.infer_phase(plan) not in {5, 7}, (
                f"legacy phase returned for {plan}"
            )


# ---------------------------------------------------------------------------
# 2. PHASE_PROMPTS and PHASE_CONTROL_MODE — contiguous keys
# ---------------------------------------------------------------------------


class TestPromptsAfterMerge:
    def test_prompt_keys_are_contiguous(self):
        assert set(PHASE_PROMPTS) == {1, 2, 3, 4}

    def test_control_mode_keys_are_contiguous(self):
        assert set(PHASE_CONTROL_MODE) == {1, 2, 3, 4}

    def test_phase2_prompt_covers_accommodation(self):
        """Merged phase 2 prompt must mention accommodation."""
        prompt = PHASE_PROMPTS[2]
        assert "住宿" in prompt

    def test_phase2_prompt_covers_dates(self):
        prompt = PHASE_PROMPTS[2]
        assert "日期" in prompt

    def test_phase2_prompt_covers_skeleton(self):
        prompt = PHASE_PROMPTS[2]
        assert "骨架" in prompt
        assert "candidate" in prompt

    def test_remaining_phases_still_exist(self):
        for phase in [1, 2, 3, 4]:
            assert phase in PHASE_PROMPTS
            assert phase in PHASE_CONTROL_MODE


# ---------------------------------------------------------------------------
# 3. _PHASE_DOWNSTREAM — merged phase 2 clears dates+accommodation+daily_plans
# ---------------------------------------------------------------------------


class TestDownstreamAfterMerge:
    def test_phase4_has_no_downstream_fields(self):
        assert 4 not in _PHASE_DOWNSTREAM

    def test_phase2_downstream_includes_accommodation(self):
        assert "accommodation" in _PHASE_DOWNSTREAM[2]

    def test_phase2_downstream_includes_dates(self):
        assert "dates" in _PHASE_DOWNSTREAM[2]

    def test_phase3_downstream_includes_daily_plans(self):
        assert "daily_plans" in _PHASE_DOWNSTREAM[3]

    def test_phase2_downstream_includes_skeleton_fields(self):
        assert "skeleton_plans" in _PHASE_DOWNSTREAM[2]
        assert "selected_skeleton_id" in _PHASE_DOWNSTREAM[2]


# ---------------------------------------------------------------------------
# 4. Tool phases — current routing surface uses 1/2/3/4
# ---------------------------------------------------------------------------


class TestToolPhasesAfterMerge:
    def test_transport_and_accommodation_tools_are_phase2(self):
        """Transport and accommodation search tools belong to Phase 2."""
        # We test by creating tools and checking; the actual tool files
        # are validated by importing them.
        from tools.search_flights import make_search_flights_tool
        from tools.search_trains import make_search_trains_tool
        from tools.search_accommodations import make_search_accommodations_tool
        from config import ApiKeysConfig

        keys = ApiKeysConfig()

        flight_tool = make_search_flights_tool(keys)
        assert flight_tool.phases == [2]

        train_tool = make_search_trains_tool(None)
        assert train_tool.phases == [2]

        accom_tool = make_search_accommodations_tool(keys)
        assert accom_tool.phases == [2]

    def test_route_and_availability_tools_are_phase2_and_phase3(self):
        """Route and availability tools are available while framing and assembling."""
        from tools.calculate_route import make_calculate_route_tool
        from tools.check_availability import make_check_availability_tool
        from tools.assemble_day_plan import make_assemble_day_plan_tool
        from config import ApiKeysConfig

        keys = ApiKeysConfig()

        route_tool = make_calculate_route_tool(keys)
        assert route_tool.phases == [2, 3]

        avail_tool = make_check_availability_tool(keys)
        assert avail_tool.phases == [2, 3]

        assemble_tool = make_assemble_day_plan_tool()
        assert assemble_tool.phases == [2]

    def test_get_poi_info_is_phase2_and_phase3(self):
        """POI details are available while framing and assembling."""
        from tools.get_poi_info import make_get_poi_info_tool
        from config import ApiKeysConfig

        keys = ApiKeysConfig()
        poi_tool = make_get_poi_info_tool(keys)
        assert poi_tool.phases == [2, 3]

    def test_universal_and_basics_tool_phases(self):
        """Universal browse tools span current phases; basics updates stay early."""
        from tools.xiaohongshu_search import (
            make_xiaohongshu_get_comments_tool,
            make_xiaohongshu_read_note_tool,
            make_xiaohongshu_search_notes_tool,
        )
        from tools.plan_tools.trip_basics import make_update_trip_basics_tool
        from config import XhsConfig

        for xhs_tool in [
            make_xiaohongshu_search_notes_tool(XhsConfig()),
            make_xiaohongshu_read_note_tool(XhsConfig()),
            make_xiaohongshu_get_comments_tool(XhsConfig()),
        ]:
            assert xhs_tool.phases == [1, 2, 3, 4]

        plan = TravelPlanState(session_id="s")
        utb_tool = make_update_trip_basics_tool(plan)
        assert utb_tool.phases == [1, 2]

    def test_engine_returns_no_tools_for_legacy_phase_id(self):
        """ToolEngine should return no tools for a retired phase id."""

        @tool(name="t1", description="d", phases=[3], parameters={})
        async def t1():
            return {}

        @tool(name="t2", description="d", phases=[5], parameters={})
        async def t2():
            return {}

        engine = ToolEngine()
        engine.register(t1)
        engine.register(t2)
        assert engine.get_tools_for_phase(5) == []


# ---------------------------------------------------------------------------
# 5. Backtrack — phase 2 clears accommodation now
# ---------------------------------------------------------------------------


class TestBacktrackAfterMerge:
    def test_backtrack_to_phase3_clears_dates_and_accommodation(self):
        from phase.backtrack import BacktrackService

        plan = TravelPlanState(
            session_id="s",
            phase=3,
            destination="Tokyo",
            dates=DateRange(start="2026-05-01", end="2026-05-05"),
            selected_skeleton_id="balanced",
            skeleton_plans=[{"id": "balanced"}],
            accommodation=Accommodation(area="新宿"),
            daily_plans=[DayPlan(day=1, date="2026-05-01")],
        )
        BacktrackService().execute(plan, to_phase=2, reason="改日期", snapshot_path="")

        assert plan.phase == 2
        assert plan.dates is None
        assert plan.accommodation is None
        assert plan.daily_plans == []
        assert plan.selected_skeleton_id is None
        assert plan.skeleton_plans == []
        assert plan.destination == "Tokyo"  # preserved


# ---------------------------------------------------------------------------
# 6. from_dict loads saved phase numbers without legacy remapping
# ---------------------------------------------------------------------------


class TestPhase4MigrationInFromDict:
    def test_from_dict_keeps_saved_phase_number(self):
        raw = {
            "session_id": "saved-session",
            "phase": 4,
            "destination": "Tokyo",
            "dates": {"start": "2026-05-01", "end": "2026-05-05"},
        }
        plan = TravelPlanState.from_dict(raw)
        assert plan.phase == 4
