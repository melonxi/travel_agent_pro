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
        for event in _tool_events(events)
        if event.tool_name == "submit_day_plan_candidate"
        and event.status == "success"
    ]
    if not candidate_events:
        return _result(rubric_id, "skip", "No Phase 3 candidate submissions found.")

    replace_events = [
        event
        for event in _tool_events(events)
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
    tools = _tool_events(events)
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
        for event in _tool_events(events)
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
