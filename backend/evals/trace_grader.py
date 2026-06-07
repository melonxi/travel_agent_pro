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


RUBRICS: tuple[
    Callable[[list[TraceEvent], TravelPlanState | None], RubricResult], ...
] = (
    _grade_candidate_search,
    _grade_skeleton_days,
    _grade_state_writer,
    _grade_duplicate_poi,
    _grade_memory_skip,
)


def grade_trace_run(
    *,
    run_id: str,
    events: list[TraceEvent],
    final_plan: TravelPlanState | None,
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
    return grades
