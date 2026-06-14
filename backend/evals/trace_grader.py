from __future__ import annotations

import re
from collections.abc import Callable

from evals.trace_models import RubricResult, RubricStatus, TraceEvent
from state.models import TravelPlanState
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES

SEARCH_TOOLS = {
    "web_search",
    "quick_travel_search",
    "xiaohongshu_search_notes",
    "xiaohongshu_read_note",
    "xiaohongshu_get_comments",
    "search_destinations",
}

SHORTLIST_WRITE_TOOLS = {"set_shortlist", "set_candidate_pool"}
STATE_WRITER_TOOLS = set(PLAN_WRITER_TOOL_NAMES) | {"generate_summary"}
DAY_PLAN_WRITE_TOOLS = {"replace_all_day_plans", "save_day_plan"}
SKIP_RECALL_VALUES = {"", "skip", "skip_recall", "false", "none"}
LOCK_DIFF_FIELDS = {
    "selected_skeleton_id",
    "selected_transport",
    "selected_accommodation",
}
LOCK_TOOL_NAMES = {
    "select_skeleton",
    "lock_skeleton",
    "set_selected_skeleton",
    "lock_transport",
    "select_transport",
    "lock_accommodation",
    "select_accommodation",
}


def _result(
    rubric_id: str,
    status: RubricStatus,
    reason: str,
    evidence_event_ids: list[str] | None = None,
) -> RubricResult:
    return RubricResult(
        rubric_id=rubric_id,
        status=status,
        score=1 if status == "pass" else 0,
        reason=reason,
        evidence_event_ids=evidence_event_ids or [],
    )


def _tool_events(events: list[TraceEvent]) -> list[TraceEvent]:
    return [event for event in events if event.event_type == "tool_call"]


def _tool_result_events(events: list[TraceEvent]) -> list[TraceEvent]:
    return [event for event in events if event.event_type == "tool_result"]


def _state_diff_events(events: list[TraceEvent]) -> list[TraceEvent]:
    return [event for event in events if event.event_type == "state_diff"]


def _payload_tool_call_id(event: TraceEvent) -> str | None:
    value = event.payload.get("tool_call_id")
    return value if isinstance(value, str) and value else None


def _tool_pairs(events: list[TraceEvent]) -> list[tuple[TraceEvent, TraceEvent | None]]:
    results_by_call_id = {
        call_id: event
        for event in _tool_result_events(events)
        if (call_id := _payload_tool_call_id(event))
    }
    return [
        (event, results_by_call_id.get(_payload_tool_call_id(event)))
        for event in _tool_events(events)
    ]


def _tool_execution_events(events: list[TraceEvent]) -> list[TraceEvent]:
    results = _tool_result_events(events)
    return results if results else _tool_events(events)


def _state_changes(event: TraceEvent) -> list[dict]:
    value = event.payload.get("state_changes")
    return value if isinstance(value, list) else []


def _grade_candidate_search(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "phase2_candidate_has_search_before_shortlist"
    tools = _tool_events(events)
    first_write = next(
        (event for event in tools if event.tool_name in SHORTLIST_WRITE_TOOLS),
        None,
    )
    if first_write is None:
        return _result(
            rubric_id,
            "skip",
            "No shortlist or candidate-pool write event found.",
        )

    prior_search = [
        event
        for event in tools
        if event.sequence < first_write.sequence and event.tool_name in SEARCH_TOOLS
    ]
    if prior_search:
        return _result(
            rubric_id,
            "pass",
            "Candidate write happened after search evidence.",
            [prior_search[-1].event_id, first_write.event_id],
        )
    return _result(
        rubric_id,
        "fail",
        "Candidate write happened without earlier search evidence.",
        [first_write.event_id],
    )


def _selected_skeleton(plan: TravelPlanState) -> dict | None:
    selected = plan.selected_skeleton_id
    if not selected:
        return None
    for skeleton in plan.skeleton_plans:
        if not isinstance(skeleton, dict):
            continue
        if skeleton.get("id") == selected or skeleton.get("name") == selected:
            return skeleton
    return None


def _grade_skeleton_days(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "phase2_skeleton_days_match_dates"
    if final_plan is None or not final_plan.dates or not final_plan.selected_skeleton_id:
        return _result(
            rubric_id,
            "skip",
            "Final plan lacks dates or selected skeleton.",
        )

    skeleton = _selected_skeleton(final_plan)
    if skeleton is None:
        return _result(rubric_id, "fail", "Selected skeleton id does not resolve.")

    days = skeleton.get("days") or []
    if not isinstance(days, list):
        return _result(rubric_id, "fail", "Selected skeleton days is not a list.")

    expected = final_plan.dates.total_days
    actual = len(days)
    if actual == expected:
        return _result(rubric_id, "pass", f"Selected skeleton has {actual} days.")
    return _result(
        rubric_id,
        "fail",
        f"Selected skeleton has {actual} days but trip dates require {expected}.",
    )


def _grade_state_writer(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "state_write_uses_plan_writer"
    diff_events = _state_diff_events(events)
    if diff_events:
        bad_diffs = [
            event
            for event in diff_events
            if event.tool_name not in STATE_WRITER_TOOLS
        ]
        if bad_diffs:
            return _result(
                rubric_id,
                "fail",
                "A non-writer tool produced state_diff.",
                [event.event_id for event in bad_diffs],
            )
        return _result(
            rubric_id,
            "pass",
            "All state_diff events came from writer tools.",
            [event.event_id for event in diff_events],
        )

    changed = [event for event in _tool_events(events) if _state_changes(event)]
    if not changed:
        return _result(rubric_id, "skip", "No state changes found in trace events.")

    bad = [event for event in changed if event.tool_name not in STATE_WRITER_TOOLS]
    if bad:
        return _result(
            rubric_id,
            "fail",
            "A non-writer tool carried state_changes.",
            [event.event_id for event in bad],
        )
    return _result(
        rubric_id,
        "pass",
        "All state changes came from writer tools.",
        [event.event_id for event in changed],
    )


def _normalize_poi(value: str) -> str:
    return re.sub(r"[\s,，.。·・、:：;；()（）\-—_]+", "", value.strip().lower())


def _grade_duplicate_poi(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "phase3_no_duplicate_poi_across_days"
    if final_plan is None or not final_plan.daily_plans:
        return _result(rubric_id, "skip", "Final plan lacks daily plans.")

    seen: dict[str, int] = {}
    for day_plan in final_plan.daily_plans:
        for activity in day_plan.activities:
            raw_name = activity.location.name or activity.name
            key = _normalize_poi(raw_name)
            if not key:
                continue
            prior_day = seen.get(key)
            if prior_day is not None and prior_day != day_plan.day:
                evidence = [
                    event.event_id
                    for event in _tool_events(events)
                    if event.tool_name in DAY_PLAN_WRITE_TOOLS
                ]
                return _result(
                    rubric_id,
                    "fail",
                    f"Normalized POI '{key}' appears in day {prior_day} "
                    f"and day {day_plan.day}.",
                    evidence,
                )
            seen[key] = day_plan.day
    return _result(rubric_id, "pass", "No duplicate normalized POI across days.")


def _grade_memory_skip(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "memory_current_trip_fact_skip_recall"
    memory_events = [event for event in events if event.event_type == "memory_recall"]
    if not memory_events:
        return _result(rubric_id, "skip", "No memory recall telemetry found.")

    current_fact_events = [
        event
        for event in memory_events
        if "current_trip_fact" in str(event.payload.get("stage0_matched_rule", ""))
        or event.payload.get("gate_intent_type") == "current_trip_fact"
    ]
    if not current_fact_events:
        return _result(
            rubric_id,
            "skip",
            "Memory telemetry does not indicate a current-trip fact case.",
        )

    recalled = [
        event
        for event in current_fact_events
        if str(event.payload.get("final_recall_decision", "")).lower()
        not in SKIP_RECALL_VALUES
    ]
    if recalled:
        return _result(
            rubric_id,
            "fail",
            "Current-trip fact telemetry resulted in recall.",
            [event.event_id for event in recalled],
        )
    return _result(
        rubric_id,
        "pass",
        "Current-trip fact telemetry skipped recall.",
        [event.event_id for event in current_fact_events],
    )


def _grade_phase3_daily_plan_coverage(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "phase3_daily_plans_cover_trip_dates"
    if final_plan is None or final_plan.phase < 3:
        return _result(rubric_id, "skip", "Final plan has not reached Phase 3.")
    if not final_plan.dates:
        return _result(rubric_id, "skip", "Final plan lacks trip dates.")

    expected = final_plan.dates.total_days
    actual = len(final_plan.daily_plans)
    writer_events = [
        event.event_id
        for event in _tool_events(events)
        if event.tool_name in DAY_PLAN_WRITE_TOOLS
    ]
    if actual != expected:
        return _result(
            rubric_id,
            "fail",
            f"Expected {expected} daily plans, got {actual}.",
            writer_events,
        )

    empty_days = [
        day.day for day in final_plan.daily_plans if len(day.activities) == 0
    ]
    if empty_days:
        return _result(
            rubric_id,
            "fail",
            f"Daily plans with no activities: {empty_days}.",
            writer_events,
        )
    return _result(
        rubric_id,
        "pass",
        f"All {actual} daily plans cover trip dates and contain activities.",
        writer_events,
    )


def _grade_phase3_parallel_finalized(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "phase3_parallel_candidates_finalized"
    candidate_events = [
        event
        for event in _tool_execution_events(events)
        if event.tool_name == "submit_day_plan_candidate"
        and event.status == "success"
    ]
    if not candidate_events:
        return _result(rubric_id, "skip", "No Phase 3 candidate submissions found.")

    replace_events = [
        event
        for event in _tool_execution_events(events)
        if event.tool_name == "replace_all_day_plans"
        and event.status == "success"
    ]
    evidence = [event.event_id for event in [*candidate_events, *replace_events]]
    if not replace_events:
        return _result(
            rubric_id,
            "fail",
            "Phase 3 worker candidates were submitted but final daily plans were not replaced.",
            evidence,
        )
    if final_plan is not None and final_plan.phase < 4:
        return _result(
            rubric_id,
            "fail",
            f"Candidates were finalized but final phase is {final_plan.phase}.",
            evidence,
        )
    return _result(
        rubric_id,
        "pass",
        "Phase 3 worker candidates were finalized through replace_all_day_plans.",
        evidence,
    )


def _grade_tool_error_rate(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "tool_error_rate_below_half"
    tools = _tool_execution_events(events)
    if len(tools) < 3:
        return _result(rubric_id, "skip", "Fewer than 3 tool calls.")

    errors = [event for event in tools if event.status == "error"]
    rate = len(errors) / len(tools)
    if rate >= 0.5:
        return _result(
            rubric_id,
            "fail",
            f"Tool error rate is {len(errors)}/{len(tools)} ({rate:.0%}).",
            [event.event_id for event in errors],
        )
    return _result(
        rubric_id,
        "pass",
        f"Tool error rate is {len(errors)}/{len(tools)} ({rate:.0%}).",
        [event.event_id for event in errors],
    )


def _grade_phase4_deliverables(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "phase4_generate_summary_freezes_deliverables"
    summary_events = [
        event
        for event in _tool_execution_events(events)
        if event.tool_name == "generate_summary"
    ]
    if not summary_events and not (
        final_plan is not None and final_plan.deliverables
    ):
        return _result(rubric_id, "skip", "No Phase 4 summary generation found.")
    if final_plan is None:
        return _result(rubric_id, "fail", "Final plan is unavailable.")

    deliverables = final_plan.deliverables or {}
    missing = [
        key
        for key in ("travel_plan_md", "checklist_md", "generated_at")
        if not deliverables.get(key)
    ]
    if missing:
        return _result(
            rubric_id,
            "fail",
            f"Deliverables missing fields: {missing}.",
            [event.event_id for event in summary_events],
        )
    return _result(
        rubric_id,
        "pass",
        "Phase 4 deliverables are frozen with travel plan and checklist files.",
        [event.event_id for event in summary_events],
    )


def _grade_tool_args_grounded(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "tool_args_grounded_in_user_constraints"
    pairs = _tool_pairs(events)
    if not pairs:
        return _result(rubric_id, "skip", "No tool_call evidence found.")
    evaluable = [
        call
        for call, _ in pairs
        if call.tool_name in SEARCH_TOOLS
        or call.tool_name in STATE_WRITER_TOOLS
        or call.tool_name in LOCK_TOOL_NAMES
    ]
    if not evaluable:
        return _result(rubric_id, "skip", "No constraint-sensitive tool calls found.")
    bad = []
    for call in evaluable:
        payload = call.payload
        preview = str(payload.get("arguments_preview") or "").strip()
        explicit_grounding = payload.get("grounded_in_user_constraints")
        if explicit_grounding is False or payload.get("ungrounded") is True:
            bad.append(call)
            continue
        if not payload.get("arguments_hash") and not preview:
            bad.append(call)
            continue
        if preview in {"", "{}", "[]", "None", "null"} and call.tool_name in SEARCH_TOOLS:
            bad.append(call)
    if bad:
        return _result(
            rubric_id,
            "fail",
            "Some constraint-sensitive tool calls lack grounded argument evidence.",
            [event.event_id for event in bad],
        )
    return _result(
        rubric_id,
        "pass",
        "Constraint-sensitive tool calls include argument hashes/previews and no ungrounded flags.",
        [event.event_id for event in evaluable],
    )


def _grade_empty_tool_result_not_used(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "tool_result_empty_not_used_as_evidence"
    results = _tool_result_events(events)
    if not results:
        return _result(rubric_id, "skip", "No tool_result evidence found.")
    empty_results = [
        event
        for event in results
        if (
            isinstance(event.payload.get("quality_flags"), dict)
            and event.payload["quality_flags"].get("empty") is True
        )
    ]
    if not empty_results:
        return _result(rubric_id, "pass", "No empty successful tool results found.")
    bad = []
    for empty in empty_results:
        later_state_use = [
            event
            for event in _state_diff_events(events)
            if event.sequence > empty.sequence
            and (
                event.parent_event_id == empty.event_id
                or event.payload.get("tool_call_id") == empty.payload.get("tool_call_id")
            )
        ]
        later_finalize = [
            event
            for event in events
            if event.sequence > empty.sequence
            and event.event_type in {"deliverable_draft", "deliverable_finalize"}
        ]
        if later_state_use or later_finalize:
            bad.append(empty)
    if bad:
        return _result(
            rubric_id,
            "fail",
            "Empty tool results were followed by state writes or deliverable finalization.",
            [event.event_id for event in bad],
        )
    return _result(
        rubric_id,
        "pass",
        "Empty tool results were not used as write/finalization evidence.",
        [event.event_id for event in empty_results],
    )


def _grade_repeated_tool_argument_failure(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "repeated_tool_argument_failure"
    pairs = [
        (call, result)
        for call, result in _tool_pairs(events)
        if result is not None and result.status == "error"
    ]
    if not pairs:
        return _result(rubric_id, "skip", "No errored tool_result pairs found.")
    grouped: dict[tuple[str | None, str | None], list[TraceEvent]] = {}
    for call, result in pairs:
        key = (call.tool_name, call.payload.get("arguments_hash"))
        grouped.setdefault(key, []).append(result)
    repeated = [
        result
        for key, group in grouped.items()
        if key[1] and len(group) >= 2
        for result in group
    ]
    if repeated:
        return _result(
            rubric_id,
            "fail",
            "Same tool arguments failed repeatedly.",
            [event.event_id for event in repeated],
        )
    return _result(
        rubric_id,
        "pass",
        "Errored tool calls did not repeat the same argument hash.",
        [result.event_id for _, result in pairs if result is not None],
    )


def _grade_phase_transition_has_gate(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "phase_transition_has_gate_evidence"
    transitions = [event for event in events if event.event_type == "phase_transition"]
    if not transitions:
        return _result(rubric_id, "skip", "No phase_transition events found.")
    gates = [event for event in events if event.event_type == "phase_gate"]
    gate_ids = {event.event_id for event in gates}
    bad = [
        transition
        for transition in transitions
        if transition.parent_event_id not in gate_ids
        and not any(gate.sequence < transition.sequence for gate in gates)
    ]
    if bad:
        return _result(
            rubric_id,
            "fail",
            "Phase transitions lack linked phase_gate evidence.",
            [event.event_id for event in bad],
        )
    return _result(
        rubric_id,
        "pass",
        "Every phase transition has linked or prior phase_gate evidence.",
        [event.event_id for event in transitions],
    )


def _grade_state_write_has_diff(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "state_write_has_diff"
    writer_results = [
        event
        for event in _tool_execution_events(events)
        if event.tool_name in STATE_WRITER_TOOLS and event.status == "success"
    ]
    if not writer_results:
        return _result(rubric_id, "skip", "No successful writer tool_result evidence found.")
    diffs = _state_diff_events(events)
    bad = []
    for result in writer_results:
        tool_call_id = result.payload.get("tool_call_id")
        matched = [
            diff
            for diff in diffs
            if diff.parent_event_id == result.event_id
            or (
                tool_call_id
                and diff.payload.get("tool_call_id") == tool_call_id
            )
        ]
        if not matched:
            bad.append(result)
    if bad:
        return _result(
            rubric_id,
            "fail",
            "Successful writer tool results lack linked state_diff events.",
            [event.event_id for event in bad],
        )
    return _result(
        rubric_id,
        "pass",
        "Successful writer tool results have linked state_diff events.",
        [event.event_id for event in writer_results],
    )


def _grade_weather_uncertainty_preserved(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "weather_uncertainty_preserved"
    weather_results = [
        event
        for event in _tool_execution_events(events)
        if event.tool_name == "check_weather"
    ]
    uncertain = [
        event
        for event in weather_results
        if any(
            marker in str(event.payload).lower()
            for marker in ("reference_only", "future_weather", "forecast_reference", "仅供参考", "临近出发")
        )
    ]
    if not uncertain:
        return _result(rubric_id, "skip", "No uncertain weather evidence found.")
    preservation_events = [
        event
        for event in events
        if (
            event.event_type == "validation"
            and any(
                marker in str(event.payload)
                for marker in (
                    "FUTURE_WEATHER_NOT_TREATED_AS_EXACT",
                    "weather_uncertainty",
                    "临近出发",
                    "仅供参考",
                )
            )
        )
        or (
            event.event_type in {"deliverable_draft", "deliverable_finalize"}
            and any(
                marker in str(event.payload)
                for marker in ("weather_uncertainty_preserved", "临近出发", "仅供参考")
            )
        )
    ]
    if preservation_events:
        return _result(
            rubric_id,
            "pass",
            "Weather uncertainty was preserved by validation or deliverable evidence.",
            [uncertain[0].event_id, preservation_events[-1].event_id],
        )
    return _result(
        rubric_id,
        "fail",
        "Uncertain weather evidence exists without validation/deliverable preservation evidence.",
        [event.event_id for event in uncertain],
    )


def _grade_lock_requires_user_authorization(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "lock_requires_user_authorization"
    lock_diffs = [
        event
        for event in _state_diff_events(events)
        if any(field in event.payload.get("field_diffs", {}) for field in LOCK_DIFF_FIELDS)
    ]
    if not lock_diffs:
        return _result(rubric_id, "skip", "No lock state_diff events found.")
    bad = []
    for diff in lock_diffs:
        payload_text = str(diff.payload).lower()
        if (
            diff.payload.get("user_authorized") is True
            or diff.payload.get("authorization_source") in {"user", "explicit_user"}
            or "user_authorized" in payload_text
            or "explicit_user" in payload_text
        ):
            continue
        bad.append(diff)
    if bad:
        return _result(
            rubric_id,
            "fail",
            "Lock state diffs lack explicit user authorization evidence.",
            [event.event_id for event in bad],
        )
    return _result(
        rubric_id,
        "pass",
        "Lock state diffs include explicit user authorization evidence.",
        [event.event_id for event in lock_diffs],
    )


def _grade_context_memory_injection_relevant(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "context_memory_injection_is_relevant"
    context_events = [
        event
        for event in events
        if event.event_type == "context_build"
        and event.payload.get("memory_candidate_ids")
    ]
    if not context_events:
        return _result(rubric_id, "skip", "No context_build memory injection evidence found.")
    memory_hits = [event for event in events if event.event_type == "memory_hit"]
    selected_ids = {
        item
        for hit in memory_hits
        for item in (
            hit.payload.get("selected_ids")
            or hit.payload.get("injected_context_ids")
            or []
        )
    }
    bad = []
    for context in context_events:
        injected = set(context.payload.get("memory_candidate_ids") or [])
        if not injected:
            continue
        if not selected_ids or not injected.issubset(selected_ids):
            bad.append(context)
    if bad:
        return _result(
            rubric_id,
            "fail",
            "Context injected memory ids that are not backed by memory_hit selected ids.",
            [event.event_id for event in bad],
        )
    return _result(
        rubric_id,
        "pass",
        "Context memory ids are backed by memory_hit selected ids.",
        [event.event_id for event in context_events],
    )


def _grade_deliverable_after_quality_pass(
    events: list[TraceEvent], final_plan: TravelPlanState | None
) -> RubricResult:
    rubric_id = "deliverable_finalized_after_quality_pass"
    finalize_events = [
        event for event in events if event.event_type == "deliverable_finalize"
    ]
    if not finalize_events:
        return _result(rubric_id, "skip", "No deliverable_finalize events found.")
    quality_events = [
        event
        for event in events
        if event.event_type in {"soft_judge", "quality_gate", "validation"}
        and event.sequence < finalize_events[-1].sequence
    ]
    approved_payload = any(
        isinstance(event.payload.get("quality_decision"), dict)
        and event.payload["quality_decision"].get("status") == "approved"
        for event in finalize_events
    )
    passing_quality = [
        event
        for event in quality_events
        if event.status in {"success", "pass", "skipped"}
        or event.payload.get("status") in {"pass", "approved"}
        or event.payload.get("final_action") == "allow"
    ]
    if approved_payload or passing_quality:
        evidence = [finalize_events[-1].event_id]
        if passing_quality:
            evidence.insert(0, passing_quality[-1].event_id)
        return _result(
            rubric_id,
            "pass",
            "Deliverables finalized after quality pass/approval evidence.",
            evidence,
        )
    return _result(
        rubric_id,
        "fail",
        "Deliverables finalized without prior quality pass/approval evidence.",
        [event.event_id for event in finalize_events],
    )


def _grade_run_status(run_status: str | None) -> RubricResult:
    rubric_id = "run_completed_without_timeout"
    if not run_status:
        return _result(rubric_id, "skip", "Run status not provided.")
    if run_status == "completed":
        return _result(rubric_id, "pass", "Run completed.")
    return _result(rubric_id, "fail", f"Run ended with status={run_status}.")


RUBRICS: tuple[
    Callable[[list[TraceEvent], TravelPlanState | None], RubricResult], ...
] = (
    _grade_candidate_search,
    _grade_skeleton_days,
    _grade_state_writer,
    _grade_duplicate_poi,
    _grade_memory_skip,
    _grade_phase3_daily_plan_coverage,
    _grade_phase3_parallel_finalized,
    _grade_tool_error_rate,
    _grade_phase4_deliverables,
    _grade_tool_args_grounded,
    _grade_empty_tool_result_not_used,
    _grade_repeated_tool_argument_failure,
    _grade_phase_transition_has_gate,
    _grade_state_write_has_diff,
    _grade_weather_uncertainty_preserved,
    _grade_lock_requires_user_authorization,
    _grade_context_memory_injection_relevant,
    _grade_deliverable_after_quality_pass,
)


def grade_trace_run(
    *,
    run_id: str,
    events: list[TraceEvent],
    final_plan: TravelPlanState | None,
    run_status: str | None = None,
) -> list[RubricResult]:
    grades: list[RubricResult] = []
    for rubric in RUBRICS:
        try:
            grades.append(rubric(events, final_plan))
        except Exception as exc:
            grades.append(
                _result(
                    getattr(rubric, "__name__", "unknown_rubric"),
                    "skip",
                    f"Rubric raised {type(exc).__name__}.",
                )
            )
    grades.append(_grade_run_status(run_status))
    return grades
