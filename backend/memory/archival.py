from __future__ import annotations

from memory.v3_models import ArchivedTripEpisode
from state.models import TravelPlanState


def build_archived_trip_episode(
    *,
    user_id: str,
    session_id: str,
    plan: TravelPlanState,
    now: str,
) -> ArchivedTripEpisode:
    return ArchivedTripEpisode(
        id=f"ep_{plan.trip_id or session_id}",
        user_id=user_id,
        session_id=session_id,
        trip_id=plan.trip_id,
        destination=plan.destination,
        dates=_dates_payload(plan),
        travelers=plan.travelers.to_dict() if plan.travelers else None,
        budget=plan.budget.to_dict() if plan.budget else None,
        selected_skeleton=_selected_skeleton(plan),
        selected_transport=dict(plan.selected_transport)
        if isinstance(plan.selected_transport, dict)
        else None,
        accommodation=plan.accommodation.to_dict() if plan.accommodation else None,
        daily_plan_summary=_daily_plan_summary(plan),
        final_plan_summary=_final_plan_summary(plan),
        decision_log=[dict(item) for item in plan.decision_events],
        lesson_log=[dict(item) for item in plan.lesson_events],
        created_at=now,
        completed_at=now,
    )


def _dates_payload(plan: TravelPlanState) -> dict[str, object]:
    if not plan.dates:
        return {}
    return {
        "start": plan.dates.start,
        "end": plan.dates.end,
        "total_days": plan.dates.total_days,
    }


def _selected_skeleton(plan: TravelPlanState) -> dict[str, object] | None:
    if not plan.selected_skeleton_id:
        return None
    for skeleton in plan.skeleton_plans:
        if not isinstance(skeleton, dict):
            continue
        if skeleton.get("id") == plan.selected_skeleton_id:
            return dict(skeleton)
    return {"id": plan.selected_skeleton_id}


def _daily_plan_summary(plan: TravelPlanState) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for day in plan.daily_plans:
        areas: list[str] = []
        for activity in day.activities:
            area = activity.location.name.strip() if activity.location and activity.location.name else ""
            if area and area not in areas:
                areas.append(area)
        summaries.append(
            {
                "day": day.day,
                "date": day.date,
                "areas": areas,
                "activity_count": len(day.activities),
                "notes": day.notes,
            }
        )
    return summaries


def _final_plan_summary(plan: TravelPlanState) -> str:
    parts: list[str] = []
    if plan.destination:
        parts.append(str(plan.destination))
    if plan.selected_skeleton_id:
        parts.append(f"骨架={plan.selected_skeleton_id}")
    if plan.accommodation:
        parts.append(f"住宿={plan.accommodation.area}")
    if not parts:
        return ""
    return "；".join(parts)
