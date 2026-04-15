"""Unit tests for state/plan_writers.py — pure data mutation functions."""
from __future__ import annotations

import pytest

from state.models import TravelPlanState


@pytest.fixture
def plan():
    return TravelPlanState(session_id="pw-test")


# --- Category A: structured writes ---


class TestWriteSkeletonPlans:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_skeleton_plans

        plan.skeleton_plans = [{"id": "old"}]
        write_skeleton_plans(plan, [{"id": "a"}, {"id": "b"}])
        assert len(plan.skeleton_plans) == 2
        assert plan.skeleton_plans[0]["id"] == "a"

    def test_asserts_on_non_list(self, plan):
        from state.plan_writers import write_skeleton_plans

        with pytest.raises(AssertionError):
            write_skeleton_plans(plan, "not a list")


class TestWriteSelectedSkeletonId:
    def test_sets_id(self, plan):
        from state.plan_writers import write_selected_skeleton_id

        write_selected_skeleton_id(plan, "plan_a")
        assert plan.selected_skeleton_id == "plan_a"

    def test_asserts_on_non_str(self, plan):
        from state.plan_writers import write_selected_skeleton_id

        with pytest.raises(AssertionError):
            write_selected_skeleton_id(plan, 123)


class TestWriteCandidatePool:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_candidate_pool

        write_candidate_pool(plan, [{"name": "A"}, {"name": "B"}])
        assert len(plan.candidate_pool) == 2

    def test_asserts_on_non_list(self, plan):
        from state.plan_writers import write_candidate_pool

        with pytest.raises(AssertionError):
            write_candidate_pool(plan, {"name": "A"})


class TestWriteShortlist:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_shortlist

        write_shortlist(plan, [{"name": "A"}])
        assert len(plan.shortlist) == 1


class TestWriteTransportOptions:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_transport_options

        write_transport_options(plan, [{"type": "flight"}, {"type": "train"}])
        assert len(plan.transport_options) == 2


class TestWriteSelectedTransport:
    def test_sets_dict(self, plan):
        from state.plan_writers import write_selected_transport

        write_selected_transport(plan, {"type": "flight", "price": 1200})
        assert plan.selected_transport["type"] == "flight"

    def test_asserts_on_non_dict(self, plan):
        from state.plan_writers import write_selected_transport

        with pytest.raises(AssertionError):
            write_selected_transport(plan, "flight")


class TestWriteAccommodationOptions:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_accommodation_options

        write_accommodation_options(plan, [{"name": "Hotel A"}])
        assert len(plan.accommodation_options) == 1


class TestWriteAccommodation:
    def test_sets_area_and_hotel(self, plan):
        from state.plan_writers import write_accommodation

        write_accommodation(plan, area="新宿", hotel="Hyatt")
        assert plan.accommodation.area == "新宿"
        assert plan.accommodation.hotel == "Hyatt"

    def test_sets_area_only(self, plan):
        from state.plan_writers import write_accommodation

        write_accommodation(plan, area="银座")
        assert plan.accommodation.area == "银座"
        assert plan.accommodation.hotel is None


class TestWriteRisks:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_risks

        write_risks(plan, [{"type": "weather", "desc": "台风"}])
        assert len(plan.risks) == 1


class TestWriteAlternatives:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_alternatives

        write_alternatives(plan, [{"name": "备选A"}])
        assert len(plan.alternatives) == 1


class TestWriteTripBrief:
    def test_merges_into_existing(self, plan):
        from state.plan_writers import write_trip_brief

        plan.trip_brief = {"goal": "old"}
        write_trip_brief(plan, {"pace": "relaxed"})
        assert plan.trip_brief == {"goal": "old", "pace": "relaxed"}

    def test_overwrites_key(self, plan):
        from state.plan_writers import write_trip_brief

        plan.trip_brief = {"goal": "old"}
        write_trip_brief(plan, {"goal": "new"})
        assert plan.trip_brief["goal"] == "new"


# --- Category A: daily plans ---


class TestAppendOneDayPlan:
    def test_appends_day(self, plan):
        from state.plan_writers import append_one_day_plan

        append_one_day_plan(
            plan,
            {
                "day": 1,
                "date": "2026-05-01",
                "activities": [],
            },
        )
        assert len(plan.daily_plans) == 1
        assert plan.daily_plans[0].day == 1

    def test_asserts_on_non_dict(self, plan):
        from state.plan_writers import append_one_day_plan

        with pytest.raises(AssertionError):
            append_one_day_plan(plan, "day 1")


class TestReplaceAllDailyPlans:
    def test_replaces_all(self, plan):
        from state.plan_writers import append_one_day_plan, replace_all_daily_plans

        append_one_day_plan(
            plan, {"day": 1, "date": "2026-05-01", "activities": []}
        )
        replace_all_daily_plans(
            plan,
            [
                {"day": 1, "date": "2026-05-01", "activities": []},
                {"day": 2, "date": "2026-05-02", "activities": []},
            ],
        )
        assert len(plan.daily_plans) == 2


# --- Category B: phrase-tolerant ---


class TestWriteDestination:
    def test_string(self, plan):
        from state.plan_writers import write_destination

        write_destination(plan, "东京")
        assert plan.destination == "东京"

    def test_dict_with_name(self, plan):
        from state.plan_writers import write_destination

        write_destination(plan, {"name": "京都", "country": "日本"})
        assert plan.destination == "京都"


class TestWriteDates:
    def test_structured(self, plan):
        from state.plan_writers import write_dates

        write_dates(plan, {"start": "2026-05-01", "end": "2026-05-05"})
        assert plan.dates is not None
        assert plan.dates.total_days == 4

    def test_phrase_style_string(self, plan):
        from state.plan_writers import write_dates

        write_dates(plan, "五一假期去4天")
        assert plan.dates is not None
        assert plan.dates.total_days == 4


class TestWriteTravelers:
    def test_structured(self, plan):
        from state.plan_writers import write_travelers

        write_travelers(plan, {"adults": 2, "children": 1})
        assert plan.travelers.adults == 2
        assert plan.travelers.children == 1

    def test_phrase_style_string(self, plan):
        from state.plan_writers import write_travelers

        write_travelers(plan, "2个大人1个小孩")
        assert plan.travelers.adults == 2
        assert plan.travelers.children == 1


class TestWriteBudget:
    def test_structured(self, plan):
        from state.plan_writers import write_budget

        write_budget(plan, {"total": 15000, "currency": "CNY"})
        assert plan.budget.total == 15000

    def test_phrase_style_string(self, plan):
        from state.plan_writers import write_budget

        write_budget(plan, "预算 1.5 万人民币")
        assert plan.budget.total == 15000
        assert plan.budget.currency == "CNY"


class TestWriteDepartureCity:
    def test_writes_into_trip_brief(self, plan):
        from state.plan_writers import write_departure_city

        write_departure_city(plan, "上海")
        assert plan.trip_brief["departure_city"] == "上海"

    def test_extracts_city_from_object(self, plan):
        from state.plan_writers import write_departure_city

        write_departure_city(plan, {"city": "杭州"})
        assert plan.trip_brief["departure_city"] == "杭州"


# --- Category C: append ---


class TestAppendPreferences:
    def test_append_string_items(self, plan):
        from state.plan_writers import append_preferences

        append_preferences(plan, ["美食", "自然风光"])
        assert len(plan.preferences) == 2
        assert plan.preferences[0].key == "美食"

    def test_append_dict_items(self, plan):
        from state.plan_writers import append_preferences

        append_preferences(plan, [{"key": "cuisine", "value": "日料"}])
        assert len(plan.preferences) == 1
        assert plan.preferences[0].key == "cuisine"

    def test_accepts_single_dict_as_one_logical_item(self, plan):
        from state.plan_writers import append_preferences

        append_preferences(plan, {"key": "pace", "value": "慢节奏"})
        assert len(plan.preferences) == 1
        assert plan.preferences[0].key == "pace"
        assert plan.preferences[0].value == "慢节奏"

    def test_accepts_single_string_as_one_logical_item(self, plan):
        from state.plan_writers import append_preferences

        append_preferences(plan, "美食")
        assert len(plan.preferences) == 1
        assert plan.preferences[0].key == "美食"
        assert plan.preferences[0].value == ""

    def test_rejects_unsupported_iterable_container(self, plan):
        from state.plan_writers import append_preferences

        with pytest.raises(AssertionError, match="Expected appendable item or list"):
            append_preferences(plan, ("美食", "自然风光"))


class TestAppendConstraints:
    def test_append_dict_items(self, plan):
        from state.plan_writers import append_constraints

        append_constraints(
            plan,
            [
                {"type": "hard", "description": "不坐红眼航班"},
            ],
        )
        assert len(plan.constraints) == 1
        assert plan.constraints[0].type == "hard"

    def test_accepts_single_dict_as_one_logical_item(self, plan):
        from state.plan_writers import append_constraints

        append_constraints(plan, {"type": "hard", "description": "不坐红眼航班"})
        assert len(plan.constraints) == 1
        assert plan.constraints[0].type == "hard"
        assert plan.constraints[0].description == "不坐红眼航班"

    def test_accepts_single_string_as_one_logical_item(self, plan):
        from state.plan_writers import append_constraints

        append_constraints(plan, "避开周末高峰")
        assert len(plan.constraints) == 1
        assert plan.constraints[0].type == "soft"
        assert plan.constraints[0].description == "避开周末高峰"

    def test_rejects_unsupported_iterable_container(self, plan):
        from state.plan_writers import append_constraints

        with pytest.raises(AssertionError, match="Expected appendable item or list"):
            append_constraints(plan, ("不早起", "不赶路"))


class TestAppendDestinationCandidate:
    def test_appends_one(self, plan):
        from state.plan_writers import append_destination_candidate

        append_destination_candidate(plan, {"name": "东京", "score": 0.9})
        assert len(plan.destination_candidates) == 1


class TestReplaceDestinationCandidates:
    def test_replaces_all(self, plan):
        from state.plan_writers import replace_destination_candidates

        plan.destination_candidates = [{"name": "old"}]
        replace_destination_candidates(plan, [{"name": "A"}, {"name": "B"}])
        assert len(plan.destination_candidates) == 2


# --- Category D: backtrack ---


class TestExecuteBacktrack:
    def test_backtrack_from_3_to_1(self, plan):
        from state.plan_writers import execute_backtrack

        plan.phase = 3
        plan.destination = "东京"
        result = execute_backtrack(plan, to_phase=1, reason="换目的地")
        assert result["backtracked"] is True
        assert result["from_phase"] == 3
        assert result["to_phase"] == 1
        assert plan.phase == 1
        assert plan.destination is None

    def test_backtrack_to_same_phase_raises(self, plan):
        from state.plan_writers import execute_backtrack

        plan.phase = 3
        with pytest.raises(ValueError, match="只能回退到更早的阶段"):
            execute_backtrack(plan, to_phase=3, reason="test")

    def test_backtrack_phase2_normalizes_to_1(self, plan):
        from state.plan_writers import execute_backtrack

        plan.phase = 3
        result = execute_backtrack(plan, to_phase=2, reason="test")
        assert result["to_phase"] == 1
