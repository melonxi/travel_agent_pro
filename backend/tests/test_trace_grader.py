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
    parent_event_id: str | None = None,
    root_event_id: str | None = None,
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
        parent_event_id=parent_event_id,
        root_event_id=root_event_id,
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


def test_tool_args_grounded_pass_fail_skip():
    assert (
        _grade_map([])["tool_args_grounded_in_user_constraints"].status == "skip"
    )

    passed = _grade_map(
        [
            _event(
                1,
                "web_search",
                {
                    "tool_call_id": "call-1",
                    "arguments_hash": "sha256:abc",
                    "arguments_preview": "{'query': '东京亲子'}",
                },
            )
        ]
    )
    assert passed["tool_args_grounded_in_user_constraints"].status == "pass"

    failed = _grade_map(
        [
            _event(
                1,
                "web_search",
                {
                    "tool_call_id": "call-1",
                    "arguments_preview": "{}",
                    "ungrounded": True,
                },
            )
        ]
    )
    grade = failed["tool_args_grounded_in_user_constraints"]
    assert grade.status == "fail"
    assert grade.evidence_event_ids == ["evt-1"]


def test_empty_tool_result_not_used_pass_fail_skip():
    assert (
        _grade_map([])["tool_result_empty_not_used_as_evidence"].status == "skip"
    )

    empty_result = _event(
        1,
        "web_search",
        {
            "tool_call_id": "call-1",
            "quality_flags": {"empty": True},
        },
        event_type="tool_result",
        status="success",
    )
    passed = _grade_map([empty_result])
    assert passed["tool_result_empty_not_used_as_evidence"].status == "pass"

    failed = _grade_map(
        [
            empty_result,
            _event(
                2,
                "update_trip_basics",
                {"tool_call_id": "call-1"},
                event_type="state_diff",
                status="success",
                parent_event_id="evt-1",
            ),
        ]
    )
    grade = failed["tool_result_empty_not_used_as_evidence"]
    assert grade.status == "fail"
    assert grade.evidence_event_ids == ["evt-1"]


def test_repeated_tool_argument_failure_pass_fail_skip():
    assert _grade_map([])["repeated_tool_argument_failure"].status == "skip"

    passed = _grade_map(
        [
            _event(1, "web_search", {"tool_call_id": "call-1", "arguments_hash": "h1"}),
            _event(
                2,
                "web_search",
                {"tool_call_id": "call-1"},
                event_type="tool_result",
                status="error",
            ),
            _event(3, "web_search", {"tool_call_id": "call-2", "arguments_hash": "h2"}),
            _event(
                4,
                "web_search",
                {"tool_call_id": "call-2"},
                event_type="tool_result",
                status="error",
            ),
        ]
    )
    assert passed["repeated_tool_argument_failure"].status == "pass"

    failed = _grade_map(
        [
            _event(1, "web_search", {"tool_call_id": "call-1", "arguments_hash": "h1"}),
            _event(
                2,
                "web_search",
                {"tool_call_id": "call-1"},
                event_type="tool_result",
                status="error",
            ),
            _event(3, "web_search", {"tool_call_id": "call-2", "arguments_hash": "h1"}),
            _event(
                4,
                "web_search",
                {"tool_call_id": "call-2"},
                event_type="tool_result",
                status="error",
            ),
        ]
    )
    grade = failed["repeated_tool_argument_failure"]
    assert grade.status == "fail"
    assert grade.evidence_event_ids == ["evt-2", "evt-4"]


def test_phase_transition_has_gate_pass_fail_skip():
    assert _grade_map([])["phase_transition_has_gate_evidence"].status == "skip"

    passed = _grade_map(
        [
            _event(1, None, {"allowed": True}, event_type="phase_gate", status="pass"),
            _event(
                2,
                None,
                {"from_phase": 1, "to_phase": 2},
                event_type="phase_transition",
                status="success",
                parent_event_id="evt-1",
            ),
        ]
    )
    assert passed["phase_transition_has_gate_evidence"].status == "pass"

    failed = _grade_map(
        [
            _event(
                1,
                None,
                {"from_phase": 1, "to_phase": 2},
                event_type="phase_transition",
                status="success",
            )
        ]
    )
    grade = failed["phase_transition_has_gate_evidence"]
    assert grade.status == "fail"
    assert grade.evidence_event_ids == ["evt-1"]


def test_state_write_has_diff_pass_fail_skip():
    assert _grade_map([])["state_write_has_diff"].status == "skip"

    writer_result = _event(
        1,
        "update_trip_basics",
        {"tool_call_id": "call-1"},
        event_type="tool_result",
        status="success",
    )
    passed = _grade_map(
        [
            writer_result,
            _event(
                2,
                "update_trip_basics",
                {"tool_call_id": "call-1", "field_diffs": {"destination": {}}},
                event_type="state_diff",
                status="success",
                parent_event_id="evt-1",
            ),
        ]
    )
    assert passed["state_write_has_diff"].status == "pass"

    failed = _grade_map([writer_result])
    grade = failed["state_write_has_diff"]
    assert grade.status == "fail"
    assert grade.evidence_event_ids == ["evt-1"]


def test_weather_uncertainty_preserved_pass_fail_skip():
    assert _grade_map([])["weather_uncertainty_preserved"].status == "skip"

    weather = _event(
        1,
        "check_weather",
        {"tool_call_id": "call-weather", "reference_only": True},
        event_type="tool_result",
        status="success",
    )
    passed = _grade_map(
        [
            weather,
            _event(
                2,
                None,
                {"validation_rule_id": "FUTURE_WEATHER_NOT_TREATED_AS_EXACT"},
                event_type="validation",
                status="pass",
            ),
        ]
    )
    assert passed["weather_uncertainty_preserved"].status == "pass"

    failed = _grade_map([weather])
    grade = failed["weather_uncertainty_preserved"]
    assert grade.status == "fail"
    assert grade.evidence_event_ids == ["evt-1"]


def test_lock_requires_user_authorization_pass_fail_skip():
    assert _grade_map([])["lock_requires_user_authorization"].status == "skip"

    passed = _grade_map(
        [
            _event(
                1,
                "lock_transport",
                {
                    "field_diffs": {"selected_transport": {}},
                    "user_authorized": True,
                },
                event_type="state_diff",
                status="success",
            )
        ]
    )
    assert passed["lock_requires_user_authorization"].status == "pass"

    failed = _grade_map(
        [
            _event(
                1,
                "lock_transport",
                {"field_diffs": {"selected_transport": {}}},
                event_type="state_diff",
                status="success",
            )
        ]
    )
    grade = failed["lock_requires_user_authorization"]
    assert grade.status == "fail"
    assert grade.evidence_event_ids == ["evt-1"]


def test_context_memory_injection_relevant_pass_fail_skip():
    assert (
        _grade_map([])["context_memory_injection_is_relevant"].status == "skip"
    )

    passed = _grade_map(
        [
            _event(
                1,
                None,
                {"selected_ids": ["mem-1", "mem-2"]},
                event_type="memory_hit",
                status="success",
            ),
            _event(
                2,
                None,
                {"memory_candidate_ids": ["mem-1"]},
                event_type="context_build",
                status="success",
            ),
        ]
    )
    assert passed["context_memory_injection_is_relevant"].status == "pass"

    failed = _grade_map(
        [
            _event(
                1,
                None,
                {"selected_ids": ["mem-1"]},
                event_type="memory_hit",
                status="success",
            ),
            _event(
                2,
                None,
                {"memory_candidate_ids": ["mem-2"]},
                event_type="context_build",
                status="success",
            ),
        ]
    )
    grade = failed["context_memory_injection_is_relevant"]
    assert grade.status == "fail"
    assert grade.evidence_event_ids == ["evt-2"]


def test_deliverable_finalized_after_quality_pass_fail_skip():
    assert (
        _grade_map([])["deliverable_finalized_after_quality_pass"].status == "skip"
    )

    passed = _grade_map(
        [
            _event(1, None, {"status": "pass"}, event_type="validation", status="pass"),
            _event(
                2,
                "generate_summary",
                {},
                event_type="deliverable_finalize",
                status="success",
            ),
        ]
    )
    assert passed["deliverable_finalized_after_quality_pass"].status == "pass"

    failed = _grade_map(
        [
            _event(
                1,
                "generate_summary",
                {},
                event_type="deliverable_finalize",
                status="success",
            )
        ]
    )
    grade = failed["deliverable_finalized_after_quality_pass"]
    assert grade.status == "fail"
    assert grade.evidence_event_ids == ["evt-1"]


def test_run_status_fails_non_completed_runs():
    grades = _grade_map([], run_status="failed")

    assert grades["run_completed_without_timeout"].status == "fail"


def test_run_status_passes_completed_runs():
    grades = _grade_map([], run_status="completed")

    assert grades["run_completed_without_timeout"].status == "pass"
