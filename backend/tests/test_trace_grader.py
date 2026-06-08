from __future__ import annotations

from evals.trace_grader import grade_trace_run
from evals.trace_models import TraceEvent
from state.models import Activity, DateRange, DayPlan, Location, TravelPlanState


def _event(
    sequence: int,
    tool_name: str | None,
    payload: dict | None = None,
    *,
    event_type: str = "tool_call",
    status: str | None = None,
) -> TraceEvent:
    resolved_status = status
    if resolved_status is None and event_type == "tool_call":
        resolved_status = "success"
    return TraceEvent(
        event_id=f"evt-{sequence}",
        run_id="run-1",
        sequence=sequence,
        event_type=event_type,
        phase=2 if event_type == "tool_call" else None,
        phase2_step="candidate" if event_type == "tool_call" else None,
        iteration=None,
        tool_name=tool_name,
        llm_provider=None,
        llm_model=None,
        status=resolved_status,
        duration_ms=1.0 if event_type == "tool_call" else None,
        cost_usd=None,
        payload=payload or ({"tool_name": tool_name} if tool_name else {}),
        created_at="2026-06-07T10:00:00+00:00",
    )


def _grade_map(events, final_plan=None, *, run_status=None):
    return {
        grade.rubric_id: grade
        for grade in grade_trace_run(
            run_id="run-1",
            events=events,
            final_plan=final_plan,
            run_status=run_status,
        )
    }


def test_candidate_search_before_shortlist_passes():
    grades = _grade_map(
        [
            _event(1, "web_search"),
            _event(2, "set_shortlist"),
        ]
    )

    assert grades["phase2_candidate_has_search_before_shortlist"].status == "pass"


def test_candidate_search_before_shortlist_fails():
    grades = _grade_map([_event(1, "set_shortlist")])

    grade = grades["phase2_candidate_has_search_before_shortlist"]
    assert grade.status == "fail"
    assert grade.evidence_event_ids == ["evt-1"]


def test_candidate_search_before_shortlist_skips_without_write():
    grades = _grade_map([_event(1, "web_search")])

    assert grades["phase2_candidate_has_search_before_shortlist"].status == "skip"


def test_skeleton_days_match_dates_passes():
    plan = TravelPlanState(session_id="s1")
    plan.dates = DateRange(start="2026-07-01", end="2026-07-03")
    plan.selected_skeleton_id = "sk1"
    plan.skeleton_plans = [
        {"id": "sk1", "days": [{"day": 1}, {"day": 2}, {"day": 3}]}
    ]

    grades = _grade_map([], plan)

    assert grades["phase2_skeleton_days_match_dates"].status == "pass"


def test_skeleton_days_match_dates_fails():
    plan = TravelPlanState(session_id="s1")
    plan.dates = DateRange(start="2026-07-01", end="2026-07-03")
    plan.selected_skeleton_id = "sk1"
    plan.skeleton_plans = [{"id": "sk1", "days": [{"day": 1}, {"day": 2}]}]

    grades = _grade_map([], plan)

    assert grades["phase2_skeleton_days_match_dates"].status == "fail"


def test_skeleton_days_match_dates_skips_without_selected_skeleton():
    plan = TravelPlanState(session_id="s1")
    plan.dates = DateRange(start="2026-07-01", end="2026-07-03")

    grades = _grade_map([], plan)

    assert grades["phase2_skeleton_days_match_dates"].status == "skip"


def test_state_write_uses_plan_writer_fails_for_read_tool_state_change():
    grades = _grade_map(
        [
            _event(
                1,
                "web_search",
                {
                    "tool_name": "web_search",
                    "state_changes": [{"field": "destination"}],
                },
            )
        ]
    )

    assert grades["state_write_uses_plan_writer"].status == "fail"


def test_state_write_uses_plan_writer_passes_for_writer_tool():
    grades = _grade_map(
        [
            _event(
                1,
                "update_trip_basics",
                {
                    "tool_name": "update_trip_basics",
                    "state_changes": [{"field": "destination"}],
                },
            )
        ]
    )

    assert grades["state_write_uses_plan_writer"].status == "pass"


def test_state_write_uses_plan_writer_skips_without_state_changes():
    grades = _grade_map([_event(1, "web_search")])

    assert grades["state_write_uses_plan_writer"].status == "skip"


def test_phase3_duplicate_poi_across_days_fails():
    plan = TravelPlanState(session_id="s1")
    plan.daily_plans = [
        DayPlan(
            day=1,
            date="2026-07-01",
            activities=[
                Activity(
                    name="清水寺",
                    location=Location(lat=0, lng=0, name="清水寺"),
                    start_time="09:00",
                    end_time="10:00",
                    category="shrine",
                )
            ],
        ),
        DayPlan(
            day=2,
            date="2026-07-02",
            activities=[
                Activity(
                    name="清 水 寺",
                    location=Location(lat=0, lng=0, name="清 水 寺"),
                    start_time="09:00",
                    end_time="10:00",
                    category="shrine",
                )
            ],
        ),
    ]

    grades = _grade_map([_event(1, "replace_all_day_plans")], plan)

    assert grades["phase3_no_duplicate_poi_across_days"].status == "fail"


def test_phase3_duplicate_poi_across_days_passes():
    plan = TravelPlanState(session_id="s1")
    plan.daily_plans = [
        DayPlan(
            day=1,
            date="2026-07-01",
            activities=[
                Activity(
                    name="清水寺",
                    location=Location(lat=0, lng=0, name="清水寺"),
                    start_time="09:00",
                    end_time="10:00",
                    category="shrine",
                )
            ],
        ),
        DayPlan(
            day=2,
            date="2026-07-02",
            activities=[
                Activity(
                    name="伏见稻荷",
                    location=Location(lat=0, lng=0, name="伏见稻荷"),
                    start_time="09:00",
                    end_time="10:00",
                    category="shrine",
                )
            ],
        ),
    ]

    grades = _grade_map([_event(1, "replace_all_day_plans")], plan)

    assert grades["phase3_no_duplicate_poi_across_days"].status == "pass"


def test_phase3_duplicate_poi_across_days_skips_without_daily_plans():
    grades = _grade_map([])

    assert grades["phase3_no_duplicate_poi_across_days"].status == "skip"


def test_memory_current_trip_fact_skip_recall_passes():
    grades = _grade_map(
        [
            _event(
                1,
                None,
                {
                    "stage0_matched_rule": "P3_current_trip_fact",
                    "final_recall_decision": "skip_recall",
                },
                event_type="memory_recall",
            )
        ]
    )

    assert grades["memory_current_trip_fact_skip_recall"].status == "pass"


def test_memory_current_trip_fact_skip_recall_fails():
    grades = _grade_map(
        [
            _event(
                1,
                None,
                {
                    "stage0_matched_rule": "P3_current_trip_fact",
                    "final_recall_decision": "query_recall_enabled",
                },
                event_type="memory_recall",
            )
        ]
    )

    assert grades["memory_current_trip_fact_skip_recall"].status == "fail"


def test_memory_current_trip_fact_skip_recall_skips_unrelated_memory():
    grades = _grade_map(
        [
            _event(
                1,
                None,
                {
                    "stage0_matched_rule": "P2_profile_preference",
                    "final_recall_decision": "query_recall_enabled",
                },
                event_type="memory_recall",
            )
        ]
    )

    assert grades["memory_current_trip_fact_skip_recall"].status == "skip"


def test_phase3_daily_plan_coverage_passes():
    plan = TravelPlanState(session_id="s1", phase=4)
    plan.dates = DateRange(start="2026-07-01", end="2026-07-02")
    plan.daily_plans = [
        DayPlan(
            day=1,
            date="2026-07-01",
            activities=[
                Activity(
                    name="清水寺",
                    location=Location(lat=0, lng=0, name="清水寺"),
                    start_time="09:00",
                    end_time="10:00",
                    category="shrine",
                )
            ],
        ),
        DayPlan(
            day=2,
            date="2026-07-02",
            activities=[
                Activity(
                    name="伏见稻荷",
                    location=Location(lat=0, lng=0, name="伏见稻荷"),
                    start_time="09:00",
                    end_time="10:00",
                    category="shrine",
                )
            ],
        ),
    ]

    grades = _grade_map([_event(1, "replace_all_day_plans")], plan)

    assert grades["phase3_daily_plans_cover_trip_dates"].status == "pass"


def test_phase3_daily_plan_coverage_fails_on_empty_activity_day():
    plan = TravelPlanState(session_id="s1", phase=4)
    plan.dates = DateRange(start="2026-07-01", end="2026-07-02")
    plan.daily_plans = [
        DayPlan(
            day=1,
            date="2026-07-01",
            activities=[
                Activity(
                    name="清水寺",
                    location=Location(lat=0, lng=0, name="清水寺"),
                    start_time="09:00",
                    end_time="10:00",
                    category="shrine",
                )
            ],
        ),
        DayPlan(day=2, date="2026-07-02"),
    ]

    grades = _grade_map([_event(1, "replace_all_day_plans")], plan)

    assert grades["phase3_daily_plans_cover_trip_dates"].status == "fail"


def test_phase3_parallel_candidates_require_final_replace():
    plan = TravelPlanState(session_id="s1", phase=3)
    plan.dates = DateRange(start="2026-07-01", end="2026-07-02")

    grades = _grade_map(
        [
            _event(1, "submit_day_plan_candidate"),
            _event(2, "submit_day_plan_candidate"),
        ],
        plan,
    )

    assert grades["phase3_parallel_candidates_finalized"].status == "fail"


def test_phase3_parallel_candidates_finalized_passes():
    plan = TravelPlanState(session_id="s1", phase=4)
    plan.dates = DateRange(start="2026-07-01", end="2026-07-02")

    grades = _grade_map(
        [
            _event(1, "submit_day_plan_candidate"),
            _event(2, "submit_day_plan_candidate"),
            _event(3, "replace_all_day_plans"),
        ],
        plan,
    )

    assert grades["phase3_parallel_candidates_finalized"].status == "pass"


def test_tool_error_rate_fails_at_half_or_above():
    grades = _grade_map(
        [
            _event(1, "web_search", status="error"),
            _event(2, "get_poi_info", status="error"),
            _event(3, "calculate_route", status="success"),
            _event(4, "check_weather", status="success"),
        ]
    )

    assert grades["tool_error_rate_below_half"].status == "fail"


def test_tool_error_rate_passes_below_half():
    grades = _grade_map(
        [
            _event(1, "web_search", status="error"),
            _event(2, "get_poi_info", status="success"),
            _event(3, "calculate_route", status="success"),
        ]
    )

    assert grades["tool_error_rate_below_half"].status == "pass"


def test_phase4_deliverables_fail_after_generate_summary_without_files():
    plan = TravelPlanState(session_id="s1", phase=4)

    grades = _grade_map([_event(1, "generate_summary")], plan)

    assert grades["phase4_generate_summary_freezes_deliverables"].status == "fail"


def test_phase4_deliverables_pass_when_frozen():
    plan = TravelPlanState(session_id="s1", phase=4)
    plan.deliverables = {
        "travel_plan_md": "travel_plan.md",
        "checklist_md": "checklist.md",
        "generated_at": "2026-07-01T00:00:00+00:00",
    }

    grades = _grade_map([_event(1, "generate_summary")], plan)

    assert grades["phase4_generate_summary_freezes_deliverables"].status == "pass"


def test_run_status_fails_non_completed_runs():
    grades = _grade_map([], run_status="failed")

    assert grades["run_completed_without_timeout"].status == "fail"


def test_run_status_passes_completed_runs():
    grades = _grade_map([], run_status="completed")

    assert grades["run_completed_without_timeout"].status == "pass"
