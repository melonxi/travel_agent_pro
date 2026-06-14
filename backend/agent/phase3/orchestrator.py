# backend/agent/orchestrator.py
"""Phase 3 Orchestrator: parallel Day Worker dispatch and result collection.

The orchestrator is pure Python (not an LLM agent). It:
1. Splits the selected skeleton into per-day tasks
2. Builds a shared prompt prefix (maximizing KV-Cache hits)
3. Spawns N Day Workers in parallel via asyncio
4. Collects results and performs global validation
5. Exposes validated DayPlans as ``final_dayplans`` for AgentLoop to commit via the standard write-tool path
6. Retries or falls back to serial on failures
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from math import radians, sin, cos, sqrt, atan2
from typing import Any, AsyncIterator

from opentelemetry import trace

from agent.phase3.candidate_store import Phase3CandidateStore
from agent.phase3.day_worker import DayWorkerResult, run_day_worker
from agent.phase3.worker_prompt import (
    DayTask,
    build_shared_prefix,
    max_core_activities_for_pace,
    split_skeleton_to_day_tasks,
)
from config import Phase3ParallelConfig
from llm.base import LLMProvider
from llm.types import ChunkType, LLMChunk
from state.models import TravelPlanState, normalize_pace_value
from storage.trace_redaction import stable_content_hash
from telemetry.trace_recorder import TraceContext, TraceRecorder
from tools.engine import ToolEngine

logger = logging.getLogger(__name__)

_LOCAL_REPAIR_ISSUE_TYPES = frozenset(
    {"pace_mismatch", "time_conflict", "transport_connection"}
)


def _derive_theme(slice_: dict) -> str | None:
    area = str(slice_.get("area") or "").strip()
    theme = str(slice_.get("theme") or "").strip()
    if area and theme:
        return f"{area} · {theme}"
    return area or theme or None


def _format_error(raw: str | None) -> str | None:
    if not raw:
        return None
    if len(raw) > 80:
        return raw[:77] + "..."
    return raw


@dataclass
class GlobalValidationIssue:
    issue_type: str  # "poi_duplicate" | "budget_overrun" | "coverage_gap"
                     # | "time_conflict" | "transport_connection" | "semantic_duplicate" | "pace_mismatch"
    description: str
    affected_days: list[int] = field(default_factory=list)
    severity: str = "warning"  # "error" | "warning"


def build_unresolved_constraint_notice(
    issues: list[GlobalValidationIssue],
) -> str | None:
    """Render a user-facing notice for error-severity issues that survived
    re-dispatch.

    Phase 3 still ships the best available plan (we deliberately do not block the
    deliverable on a soft preference like pace), but the user must be *told* that
    an explicit constraint was not fully met — instead of the previous silent
    pass that only emitted a log line.
    """
    blocking = [issue for issue in issues if issue.severity == "error"]
    if not blocking:
        return None
    lines = [
        "⚠️ 你明确要求的以下约束，自动重排后仍未完全满足，已按当前最优方案交付："
    ]
    for issue in blocking:
        lines.append(f"- {issue.description}")
    lines.append("如需我进一步调整（例如删减某天的活动），告诉我即可。")
    return "\n".join(lines)


def _time_to_minutes(t: str) -> int | None:
    try:
        if isinstance(t, str):
            match = re.search(r"(?:[01]\d|2[0-3]):[0-5]\d", t)
            if match:
                t = match.group(0)
        h, m = map(int, t.split(":"))
        return h * 60 + m
    except (ValueError, AttributeError):
        return None


def _transport_payload_for_direction(
    transport: dict[str, Any],
    direction: str,
) -> dict[str, Any] | None:
    keys = ("outbound", "going") if direction == "outbound" else ("return", "inbound")
    for key in keys:
        value = transport.get(key)
        if isinstance(value, dict):
            payload = dict(value)
            payload.setdefault("direction", direction)
            return payload
    return None


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    )
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]


def _names_similar(a: str, b: str) -> bool:
    a_norm = a.lower().strip()
    b_norm = b.lower().strip()
    if not a_norm or not b_norm:
        return False
    if a_norm in b_norm or b_norm in a_norm:
        return True
    return _levenshtein(a_norm, b_norm) <= 2


def _extract_transport_time(transport: dict[str, Any], direction: str) -> int | None:
    """Extract arrival/departure time from selected_transport dict.

    direction: 'outbound' → last segment arrival_time (final destination),
               'return'   → first segment departure_time (earliest departure)
    """
    nested = _transport_payload_for_direction(transport, direction)
    if isinstance(nested, dict):
        if direction == "outbound":
            return _time_to_minutes(
                nested.get("arrival_time", "")
                or nested.get("arr_time", "")
                or nested.get("arrival", "")
            )
        return _time_to_minutes(
            nested.get("departure_time", "")
            or nested.get("dep_time", "")
            or nested.get("departure", "")
        )

    segments = transport.get("segments")
    if isinstance(segments, list):
        result: int | None = None
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_dir = seg.get("direction", "")
            if seg_dir == direction:
                if direction == "outbound":
                    # Use last outbound segment's arrival (final destination)
                    val = _time_to_minutes(seg.get("arrival_time", ""))
                    if val is not None:
                        result = val
                else:
                    # Use first return segment's departure (earliest leave time)
                    val = _time_to_minutes(seg.get("departure_time", ""))
                    if val is not None:
                        return val
        if result is not None:
            return result
    # Fallback: single-segment transport
    if direction == "outbound":
        return _time_to_minutes(transport.get("arrival_time", ""))
    return _time_to_minutes(transport.get("departure_time", ""))


def _extract_transport_segment(
    transport: dict[str, Any],
    direction: str,
) -> dict[str, Any] | None:
    nested = _transport_payload_for_direction(transport, direction)
    if isinstance(nested, dict):
        return nested

    segments = transport.get("segments")
    if isinstance(segments, list):
        matches = [
            seg for seg in segments
            if isinstance(seg, dict) and seg.get("direction") == direction
        ]
        if matches:
            if direction == "outbound":
                return dict(matches[-1])
            return dict(matches[0])
    if direction == "outbound" and transport.get("arrival_time"):
        return dict(transport)
    if direction == "return" and transport.get("departure_time"):
        return dict(transport)
    return None


class Phase3Orchestrator:
    def __init__(
        self,
        *,
        plan: TravelPlanState,
        llm: LLMProvider | None,
        tool_engine: ToolEngine | None,
        config: Phase3ParallelConfig | None,
        stats: Any | None = None,
        trace_recorder: TraceRecorder | None = None,
        trace_context: TraceContext | None = None,
    ):
        self.plan = plan
        self.llm = llm
        self.tool_engine = tool_engine
        self.config = config or Phase3ParallelConfig()
        self.stats = stats
        self.trace_recorder = trace_recorder
        self.trace_context = trace_context
        self.final_dayplans: list[dict[str, Any]] = []
        self.final_issues: list[GlobalValidationIssue] = []
        self._trace_orchestrator_event_id: str | None = None

    def _trace_context(
        self,
        *,
        actor: str = "phase3_orchestrator",
        correlation_id: str | None = None,
        parent_event_id: str | None = None,
        root_event_id: str | None = None,
    ) -> TraceContext | None:
        if self.trace_context is None:
            return None
        return TraceContext(
            run_id=self.trace_context.run_id,
            session_id=self.trace_context.session_id,
            trip_id=self.trace_context.trip_id,
            context_epoch=self.trace_context.context_epoch,
            phase=3,
            phase2_step=getattr(self.plan, "phase2_step", None),
            parent_event_id=parent_event_id,
            root_event_id=root_event_id,
            correlation_id=correlation_id or self.trace_context.correlation_id,
            actor=actor,
            metadata=dict(self.trace_context.metadata),
        )

    async def _emit_trace_event(
        self,
        *,
        event_type: str,
        status: str,
        payload: dict[str, Any],
        actor: str = "phase3_orchestrator",
        correlation_id: str | None = None,
        parent_event_id: str | None = None,
        root_event_id: str | None = None,
    ) -> Any | None:
        if self.trace_recorder is None:
            return None
        context = self._trace_context(
            actor=actor,
            correlation_id=correlation_id,
            parent_event_id=parent_event_id,
            root_event_id=root_event_id,
        )
        if context is None:
            return None
        return await self.trace_recorder.emit_event(
            context,
            event_type=event_type,
            status=status,
            actor=actor,
            parent_event_id=parent_event_id,
            root_event_id=root_event_id,
            correlation_id=correlation_id,
            payload=payload,
        )

    async def _attach_trace_artifact(
        self,
        *,
        event_id: str | None,
        kind: str,
        content: Any,
    ) -> Any | None:
        if self.trace_recorder is None or self.trace_context is None:
            return None
        context = self._trace_context()
        if context is None:
            return None
        return await self.trace_recorder.attach_artifact(
            context,
            event_id=event_id,
            kind=kind,
            content=content,
        )

    async def _emit_worker_start_trace(
        self,
        *,
        task: DayTask,
        run_id: str,
        attempt: int,
    ) -> str | None:
        event = await self._emit_trace_event(
            event_type="phase3_worker",
            status="started",
            actor="phase3_worker",
            correlation_id=f"phase3_worker:{run_id}:day:{task.day}:attempt:{attempt}",
            parent_event_id=self._trace_orchestrator_event_id,
            root_event_id=self._trace_orchestrator_event_id,
            payload={
                "stage": "start",
                "phase3_run_id": run_id,
                "day": task.day,
                "date": task.date,
                "attempt": attempt,
                "constraints_hash": stable_content_hash(
                    {
                        "skeleton_slice": task.skeleton_slice,
                        "pace": task.pace,
                        "locked_pois": task.locked_pois,
                        "candidate_pois": task.candidate_pois,
                        "forbidden_pois": task.forbidden_pois,
                        "mobility_envelope": task.mobility_envelope,
                        "repair_hints": task.repair_hints,
                    }
                ),
                "locked_poi_count": len(task.locked_pois),
                "candidate_poi_count": len(task.candidate_pois),
                "repair_hint_count": len(task.repair_hints),
            },
        )
        return event.event_id if event is not None else None

    async def _emit_worker_result_trace(
        self,
        *,
        task: DayTask,
        result: DayWorkerResult,
        run_id: str,
        attempt: int,
        parent_event_id: str | None,
    ) -> None:
        candidate_metadata = None
        if result.dayplan is not None:
            candidate_metadata = await self._attach_trace_artifact(
                event_id=None,
                kind="phase3_candidate",
                content=result.dayplan,
            )
        event = await self._emit_trace_event(
            event_type="phase3_worker",
            status="success" if result.success else "error",
            actor="phase3_worker",
            correlation_id=f"phase3_worker:{run_id}:day:{task.day}:attempt:{attempt}",
            parent_event_id=parent_event_id,
            root_event_id=self._trace_orchestrator_event_id or parent_event_id,
            payload={
                "stage": "result",
                "phase3_run_id": run_id,
                "day": task.day,
                "date": task.date,
                "attempt": attempt,
                "success": result.success,
                "iterations": result.iterations,
                "activity_count": (
                    len(result.dayplan.get("activities", []))
                    if isinstance(result.dayplan, dict)
                    else 0
                ),
                "candidate_submission_hash": (
                    stable_content_hash(result.dayplan)
                    if result.dayplan is not None
                    else None
                ),
                "candidate_submission_artifact_id": (
                    candidate_metadata.artifact_id
                    if candidate_metadata is not None
                    else None
                ),
                "error_code": result.error_code,
                "error": _format_error(result.error),
            },
        )

    def _find_selected_skeleton(self) -> dict[str, Any] | None:
        if not self.plan.selected_skeleton_id or not self.plan.skeleton_plans:
            return None
        sid = self.plan.selected_skeleton_id
        for skeleton in self.plan.skeleton_plans:
            if not isinstance(skeleton, dict):
                continue
            if skeleton.get("id") == sid or skeleton.get("name") == sid:
                return skeleton
        valid = [s for s in self.plan.skeleton_plans if isinstance(s, dict)]
        if len(valid) == 1:
            return valid[0]
        return None

    def _split_tasks(self) -> list[DayTask]:
        skeleton = self._find_selected_skeleton()
        if skeleton is None:
            raise ValueError("未找到已选骨架方案")
        return split_skeleton_to_day_tasks(skeleton, self.plan)

    def _compile_day_tasks(self, tasks: list[DayTask]) -> list[DayTask]:
        """Enrich DayTasks with cross-day constraints derived from skeleton."""

        # 0. Make POI constraints fit the activity budget before dispatching.
        # Otherwise a relaxed day with too many locked POIs is impossible for
        # the worker to satisfy and will trigger a slow global re-dispatch.
        for t in tasks:
            max_core = max_core_activities_for_pace(t.pace)
            t.max_core_activities = max_core
            if len(t.locked_pois) > max_core:
                demoted = t.locked_pois[max_core:]
                logger.warning(
                    "Day %d has %d locked POIs over %s pace limit %d; "
                    "demoting extras to candidate_pois: %s",
                    t.day,
                    len(t.locked_pois),
                    t.pace,
                    max_core,
                    demoted,
                )
                t.locked_pois = t.locked_pois[:max_core]
                t.demoted_locked_pois = list(demoted)
                existing_candidates = set(t.candidate_pois)
                t.candidate_pois = [
                    *[poi for poi in demoted if poi not in existing_candidates],
                    *t.candidate_pois,
                ]
            t.candidate_activity_slots = max(max_core - len(t.locked_pois), 0)

        # 1. Build global POI ownership map (locked only)
        poi_owner: dict[str, int] = {}
        for t in tasks:
            for poi in t.locked_pois:
                if poi in poi_owner:
                    logger.warning(
                        "POI '%s' locked by both Day %d and Day %d",
                        poi, poi_owner[poi], t.day,
                    )
                poi_owner[poi] = t.day

        # 2. Derive forbidden_pois for each day
        for t in tasks:
            t.forbidden_pois = [
                poi for poi, owner_day in poi_owner.items()
                if owner_day != t.day
            ]

        # 3. Fill mobility_envelope defaults (only if skeleton didn't provide)
        pace_defaults = {
            "relaxed": {"max_cross_area_hops": 1, "max_transit_leg_min": 30},
            "balanced": {"max_cross_area_hops": 2, "max_transit_leg_min": 40},
            "intensive": {"max_cross_area_hops": 3, "max_transit_leg_min": 50},
        }
        for t in tasks:
            if not t.mobility_envelope:
                t.mobility_envelope = dict(
                    pace_defaults.get(t.pace, pace_defaults["balanced"])
                )

        # 4. Derive date_role (if skeleton didn't set it)
        if tasks:
            sorted_tasks = sorted(tasks, key=lambda x: x.day)
            if len(sorted_tasks) == 1:
                if sorted_tasks[0].date_role == "full_day":
                    sorted_tasks[0].date_role = "arrival_departure_day"
            else:
                if sorted_tasks[0].date_role == "full_day":
                    sorted_tasks[0].date_role = "arrival_day"
                if sorted_tasks[-1].date_role == "full_day":
                    sorted_tasks[-1].date_role = "departure_day"

        # 5. Inject day budget (soft hint)
        if self.plan.budget and self.plan.dates:
            total_days = self.plan.dates.total_days
            if total_days > 0:
                daily_avg = round(self.plan.budget.total / total_days)
                for t in tasks:
                    t.day_budget = daily_avg

        # 5b. Inject day-level (non-hard) constraints
        if self.plan.constraints:
            day_level = [
                {"type": c.type, "description": c.description}
                for c in self.plan.constraints
                if c.type != "hard"
            ]
            if day_level:
                for t in tasks:
                    t.day_constraints = day_level

        # 6. Inject arrival/departure times from transport
        transport = self.plan.selected_transport
        if isinstance(transport, dict) and tasks:
            arrival_min = _extract_transport_time(transport, "outbound")
            departure_min = _extract_transport_time(transport, "return")
            arrival_segment = _extract_transport_segment(transport, "outbound")
            departure_segment = _extract_transport_segment(transport, "return")
            sorted_tasks = sorted(tasks, key=lambda x: x.day)
            if (
                arrival_min is not None
                and sorted_tasks[0].date_role == "arrival_day"
            ):
                hh, mm = divmod(arrival_min, 60)
                sorted_tasks[0].arrival_time = f"{hh:02d}:{mm:02d}"
                sorted_tasks[0].arrival_transport = arrival_segment
            if (
                departure_min is not None
                and sorted_tasks[-1].date_role == "departure_day"
            ):
                hh, mm = divmod(departure_min, 60)
                sorted_tasks[-1].departure_time = f"{hh:02d}:{mm:02d}"
                sorted_tasks[-1].departure_transport = departure_segment
            # Handle arrival_departure_day (single-day trips)
            if (
                len(sorted_tasks) == 1
                and sorted_tasks[0].date_role == "arrival_departure_day"
            ):
                if arrival_min is not None:
                    hh, mm = divmod(arrival_min, 60)
                    sorted_tasks[0].arrival_time = f"{hh:02d}:{mm:02d}"
                    sorted_tasks[0].arrival_transport = arrival_segment
                if departure_min is not None:
                    hh, mm = divmod(departure_min, 60)
                    sorted_tasks[0].departure_time = f"{hh:02d}:{mm:02d}"
                    sorted_tasks[0].departure_transport = departure_segment

        return tasks

    def _global_validate(
        self, dayplans: list[dict[str, Any]]
    ) -> list[GlobalValidationIssue]:
        issues: list[GlobalValidationIssue] = []

        # 1. POI 去重
        poi_to_days: dict[str, list[int]] = {}
        for dp in dayplans:
            day_num = dp.get("day", 0)
            for act in dp.get("activities", []):
                name = act.get("name", "")
                if name:
                    poi_to_days.setdefault(name, []).append(day_num)
        for poi_name, days in poi_to_days.items():
            if len(days) > 1:
                issues.append(
                    GlobalValidationIssue(
                        issue_type="poi_duplicate",
                        description=f"POI '{poi_name}' 出现在多天: {days}",
                        affected_days=days[1:],
                        severity="error",
                    )
                )

        # 2. 预算检查
        if self.plan.budget:
            total_cost = sum(
                act.get("cost", 0)
                for dp in dayplans
                for act in dp.get("activities", [])
            )
            if total_cost > self.plan.budget.total:
                day_costs = []
                for dp in dayplans:
                    day_cost = sum(
                        act.get("cost", 0) for act in dp.get("activities", [])
                    )
                    day_costs.append((dp.get("day", 0), day_cost))
                day_costs.sort(key=lambda x: x[1], reverse=True)
                issues.append(
                    GlobalValidationIssue(
                        issue_type="budget_overrun",
                        description=(
                            f"总花费 {total_cost} 超出预算 "
                            f"{self.plan.budget.total} {self.plan.budget.currency}"
                        ),
                        affected_days=[d for d, _ in day_costs[:2]],
                        severity="warning",
                    )
                )

        # 3. 天数覆盖检查
        if self.plan.dates:
            expected_days = set(range(1, self.plan.dates.total_days + 1))
            actual_days = {dp.get("day", 0) for dp in dayplans}
            missing = expected_days - actual_days
            if missing:
                issues.append(
                    GlobalValidationIssue(
                        issue_type="coverage_gap",
                        description=f"缺少天数: {sorted(missing)}",
                        affected_days=sorted(missing),
                        severity="warning",
                    )
                )

        # 4. Time conflicts
        issues.extend(self._validate_time_conflicts(dayplans))

        # 5. Semantic duplicates
        issues.extend(self._validate_semantic_duplicates(dayplans))

        # 6. Transport connection
        issues.extend(self._validate_transport_connection(dayplans))

        # 7. Pace check
        issues.extend(self._validate_pace(dayplans))

        return issues

    def _validate_semantic_duplicates(
        self, dayplans: list[dict[str, Any]]
    ) -> list[GlobalValidationIssue]:
        issues: list[GlobalValidationIssue] = []
        all_pois: list[tuple[int, str, float, float]] = []
        for dp in dayplans:
            day = dp.get("day", 0)
            for act in dp.get("activities", []):
                loc = act.get("location", {})
                if not isinstance(loc, dict):
                    continue
                lat = loc.get("lat")
                lng = loc.get("lng")
                name = act.get("name", "")
                if name and lat is not None and lng is not None:
                    all_pois.append((day, name, float(lat), float(lng)))

        seen_pairs: set[tuple[int, int]] = set()
        for i, (day_a, name_a, lat_a, lng_a) in enumerate(all_pois):
            for j, (day_b, name_b, lat_b, lng_b) in enumerate(all_pois):
                if i >= j or day_a == day_b:
                    continue
                pair = (i, j)
                if pair in seen_pairs:
                    continue
                dist = _haversine_meters(lat_a, lng_a, lat_b, lng_b)
                if dist < 200 and _names_similar(name_a, name_b):
                    seen_pairs.add(pair)
                    issues.append(GlobalValidationIssue(
                        issue_type="semantic_duplicate",
                        description=(
                            f"'{name_a}'(Day {day_a}) 与 '{name_b}'(Day {day_b}) "
                            f"疑似同一地点（距离 {dist:.0f}m）"
                        ),
                        affected_days=[day_b],
                        severity="error",
                    ))
        return issues

    def _validate_time_conflicts(
        self, dayplans: list[dict[str, Any]]
    ) -> list[GlobalValidationIssue]:
        issues: list[GlobalValidationIssue] = []
        for dp in dayplans:
            day = dp.get("day", 0)
            activities = dp.get("activities", [])
            for i in range(1, len(activities)):
                prev = activities[i - 1]
                curr = activities[i]
                prev_end = _time_to_minutes(prev.get("end_time", ""))
                curr_start = _time_to_minutes(curr.get("start_time", ""))
                travel = curr.get("transport_duration_min", 0) or 0
                if prev_end is not None and curr_start is not None:
                    # Handle midnight crossing: large backward jump (>12h) means next day
                    effective_start = curr_start
                    if prev_end - curr_start > 720:
                        effective_start = curr_start + 1440
                    if prev_end + travel > effective_start:
                        issues.append(GlobalValidationIssue(
                            issue_type="time_conflict",
                            description=(
                                f"Day {day}: '{prev.get('name')}'→'{curr.get('name')}' "
                                f"时间冲突（{prev.get('end_time')} 结束 + 交通 {travel}min "
                                f"> {curr.get('start_time')} 开始）"
                            ),
                            affected_days=[day],
                            severity="error",
                        ))
        return issues

    def _validate_transport_connection(self, dayplans: list[dict[str, Any]]) -> list[GlobalValidationIssue]:
        issues: list[GlobalValidationIssue] = []
        transport = self.plan.selected_transport
        if not isinstance(transport, dict):
            return issues

        sorted_days = sorted(dayplans, key=lambda d: d.get("day", 0))
        if not sorted_days:
            return issues

        arrival_min = _extract_transport_time(transport, "outbound")
        if arrival_min is not None:
            first_day = sorted_days[0]
            acts = first_day.get("activities", [])
            if acts:
                first_start = _time_to_minutes(acts[0].get("start_time", ""))
                if first_start is not None and first_start < arrival_min + 120:
                    issues.append(GlobalValidationIssue(
                        issue_type="transport_connection",
                        description=(
                            f"Day {first_day.get('day', 1)} 首活动开始时间过早，"
                            f"距到达不足 2 小时"
                        ),
                        affected_days=[first_day.get("day", 1)],
                        severity="error",
                    ))

        departure_min = _extract_transport_time(transport, "return")
        if departure_min is not None:
            last_day = sorted_days[-1]
            acts = last_day.get("activities", [])
            if acts:
                last_end = _time_to_minutes(acts[-1].get("end_time", ""))
                if last_end is not None and last_end > departure_min - 180:
                    issues.append(GlobalValidationIssue(
                        issue_type="transport_connection",
                        description=(
                            f"Day {last_day.get('day', len(sorted_days))} 末活动结束过晚，"
                            f"距离开不足 3 小时"
                        ),
                        affected_days=[last_day.get("day", len(sorted_days))],
                        severity="error",
                    ))

        return issues

    def _validate_pace(self, dayplans: list[dict[str, Any]]) -> list[GlobalValidationIssue]:
        issues: list[GlobalValidationIssue] = []
        raw_pace = (self.plan.trip_brief or {}).get("pace")
        normalized_pace = normalize_pace_value(raw_pace)
        explicit_pace = normalized_pace is not None
        pace = normalized_pace or "balanced"
        max_activities = max_core_activities_for_pace(pace)
        severity = "error" if explicit_pace else "warning"

        for dp in dayplans:
            day = dp.get("day", 0)
            act_count = len(dp.get("activities", []))
            if act_count > max_activities:
                issues.append(GlobalValidationIssue(
                    issue_type="pace_mismatch",
                    description=(
                        f"Day {day}: {act_count} 个活动超出 {pace} 节奏上限 {max_activities}"
                    ),
                    affected_days=[day],
                    severity=severity,
                ))
        return issues

    def _build_progress_chunk(
        self,
        worker_statuses: list[dict[str, Any]],
        total_days: int,
        hint: str,
    ) -> LLMChunk:
        """Build a parallel_progress AGENT_STATUS chunk with per-worker status."""
        return LLMChunk(
            type=ChunkType.AGENT_STATUS,
            agent_status={
                "stage": "parallel_progress",
                "hint": hint,
                "total_days": total_days,
                "workers": [dict(w) for w in worker_statuses],
            },
        )

    async def run(self) -> AsyncIterator[LLMChunk]:
        """Execute parallel Phase 3 generation.

        Yields LLMChunk events for frontend progress display, including
        real-time per-worker status updates via ``parallel_progress`` events.
        """
        tracer = trace.get_tracer("phase3-orchestrator")

        with tracer.start_as_current_span("orchestrator.run") as span:
            # 1. Split tasks
            yield LLMChunk(
                type=ChunkType.AGENT_STATUS,
                agent_status={"stage": "planning", "hint": "正在分解行程任务..."},
            )
            tasks = self._split_tasks()
            tasks = self._compile_day_tasks(tasks)
            total_days = len(tasks)
            span.set_attribute("total_days", total_days)

            # 2. Build shared prefix
            shared_prefix = build_shared_prefix(self.plan)
            run_id = f"phase3_{uuid.uuid4().hex[:12]}"
            candidate_store = Phase3CandidateStore(self.config.artifact_root)
            day_task_payload = [dict(task.__dict__) for task in tasks]
            start_event = await self._emit_trace_event(
                event_type="phase3_orchestrator",
                status="started",
                correlation_id=f"phase3_orchestrator:{run_id}",
                payload={
                    "stage": "start",
                    "phase3_run_id": run_id,
                    "day_task_count": total_days,
                    "max_workers": self.config.max_workers,
                    "fallback_to_serial": self.config.fallback_to_serial,
                    "worker_max_iterations": self.config.worker_max_iterations,
                    "worker_timeout_seconds": self.config.worker_timeout_seconds,
                    "compiled_day_tasks_hash": stable_content_hash(day_task_payload),
                    "shared_prefix_hash": stable_content_hash(shared_prefix),
                },
            )
            self._trace_orchestrator_event_id = (
                start_event.event_id if start_event is not None else None
            )
            await self._attach_trace_artifact(
                event_id=self._trace_orchestrator_event_id,
                kind="context_snapshot",
                content={
                    "phase3_run_id": run_id,
                    "shared_prefix": shared_prefix,
                    "day_tasks": day_task_payload,
                },
            )

            # 3. Initialize per-worker status tracking
            worker_statuses: list[dict[str, Any]] = [
                {
                    "day": t.day,
                    "status": "running",
                    "theme": _derive_theme(t.skeleton_slice),
                    "iteration": None,
                    "max_iterations": None,
                    "current_tool": None,
                    "activity_count": None,
                    "error": None,
                    "error_code": None,
                }
                for t in tasks
            ]

            def _find_worker_idx(day: int) -> int:
                return next(
                    i for i, w in enumerate(worker_statuses) if w["day"] == day
                )

            yield self._build_progress_chunk(
                worker_statuses,
                total_days,
                f"正在并行规划 {total_days} 天行程...",
            )

            # 4. Spawn workers with concurrency control
            semaphore = asyncio.Semaphore(self.config.max_workers)
            progress_queue: asyncio.Queue = asyncio.Queue()
            worker_start_event_ids: dict[tuple[int, int], str | None] = {}

            def _make_progress_cb(idx: int):
                def _on_progress(day: int, kind: str, payload: dict) -> None:
                    try:
                        if kind == "iter_start":
                            worker_statuses[idx]["iteration"] = payload["iteration"]
                            worker_statuses[idx]["max_iterations"] = payload["max"]
                            worker_statuses[idx]["current_tool"] = None
                        elif kind == "tool_start":
                            worker_statuses[idx]["current_tool"] = (
                                payload.get("human_label") or payload.get("tool")
                            )
                        progress_queue.put_nowait({"day": day, "kind": kind})
                    except Exception as exc:
                        logger.warning(
                            "orchestrator progress callback failed: %s", exc
                        )
                return _on_progress

            async def _run_with_semaphore(task: DayTask) -> DayWorkerResult:
                idx = _find_worker_idx(task.day)
                async with semaphore:
                    return await run_day_worker(
                        llm=self.llm,
                        tool_engine=self.tool_engine,
                        plan=self.plan,
                        task=task,
                        shared_prefix=shared_prefix,
                        max_iterations=self.config.worker_max_iterations,
                        timeout_seconds=self.config.worker_timeout_seconds,
                        on_progress=_make_progress_cb(idx),
                        candidate_store=candidate_store,
                        run_id=run_id,
                        attempt=1,
                        stats=self.stats,
                        trace_recorder=self.trace_recorder,
                        trace_context=self.trace_context,
                        trace_parent_event_id=worker_start_event_ids.get(
                            (task.day, 1)
                        ),
                    )

            pending: dict[asyncio.Task, DayTask] = {}
            for task in tasks:
                worker_start_event_ids[(task.day, 1)] = (
                    await self._emit_worker_start_trace(
                        task=task,
                        run_id=run_id,
                        attempt=1,
                    )
                )
                atask = asyncio.create_task(_run_with_semaphore(task))
                pending[atask] = task

            # 5. Collect results as each worker finishes (real-time progress)
            successes: list[DayWorkerResult] = []
            failures: list[tuple[DayTask, str]] = []

            getter_task: asyncio.Task | None = None
            while pending:
                if getter_task is None:
                    getter_task = asyncio.create_task(progress_queue.get())
                wait_set: set[asyncio.Task] = set(pending.keys()) | {getter_task}
                done_set, _ = await asyncio.wait(
                    wait_set, return_when=asyncio.FIRST_COMPLETED
                )

                if getter_task in done_set:
                    _ = getter_task.result()
                    getter_task = None
                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        f"正在并行规划 {total_days} 天行程...",
                    )
                    continue

                for completed in done_set:
                    day_task = pending.pop(completed)
                    idx = _find_worker_idx(day_task.day)
                    try:
                        result = completed.result()
                        if result.success:
                            successes.append(result)
                            worker_statuses[idx]["status"] = "done"
                            worker_statuses[idx]["current_tool"] = None
                            if result.dayplan:
                                worker_statuses[idx]["activity_count"] = len(
                                    result.dayplan.get("activities", [])
                                )
                        else:
                            failures.append(
                                (day_task, result.error or "Unknown error")
                            )
                            worker_statuses[idx]["status"] = "failed"
                            worker_statuses[idx]["current_tool"] = None
                            worker_statuses[idx]["error"] = _format_error(
                                result.error
                            )
                            worker_statuses[idx]["error_code"] = result.error_code
                            logger.warning(
                                "Day %d worker failed [%s]: %s",
                                day_task.day,
                                result.error_code,
                                result.error,
                            )
                        await self._emit_worker_result_trace(
                            task=day_task,
                            result=result,
                            run_id=run_id,
                            attempt=1,
                            parent_event_id=worker_start_event_ids.get(
                                (day_task.day, 1)
                            ),
                        )
                    except Exception as e:
                        failures.append((day_task, f"Exception: {e}"))
                        worker_statuses[idx]["status"] = "failed"
                        worker_statuses[idx]["current_tool"] = None
                        worker_statuses[idx]["error"] = _format_error(
                            f"Exception: {e}"
                        )
                        worker_statuses[idx]["error_code"] = "EXCEPTION"
                        logger.error(
                            "Day %d worker exception: %s", day_task.day, e
                        )
                        await self._emit_worker_result_trace(
                            task=day_task,
                            result=DayWorkerResult(
                                day=day_task.day,
                                date=day_task.date,
                                success=False,
                                dayplan=None,
                                error=str(e),
                                error_code="EXCEPTION",
                            ),
                            run_id=run_id,
                            attempt=1,
                            parent_event_id=worker_start_event_ids.get(
                                (day_task.day, 1)
                            ),
                        )

                done_count = sum(
                    1
                    for w in worker_statuses
                    if w["status"] in ("done", "failed")
                )
                yield self._build_progress_chunk(
                    worker_statuses,
                    total_days,
                    f"已完成 {done_count}/{total_days} 天...",
                )

            if getter_task and not getter_task.done():
                getter_task.cancel()
                try:
                    await getter_task
                except (asyncio.CancelledError, Exception):
                    pass

            span.set_attribute("successes", len(successes))
            span.set_attribute("failures", len(failures))

            # 6. Check if we should fall back to serial
            if self.config.fallback_to_serial and len(failures) > len(tasks) / 2:
                logger.warning(
                    "Parallel mode failure rate %.0f%%, falling back to serial",
                    len(failures) / len(tasks) * 100,
                )
                await self._emit_trace_event(
                    event_type="phase3_orchestrator",
                    status="fallback",
                    correlation_id=f"phase3_orchestrator:{run_id}",
                    parent_event_id=self._trace_orchestrator_event_id,
                    root_event_id=self._trace_orchestrator_event_id,
                    payload={
                        "stage": "fallback",
                        "phase3_run_id": run_id,
                        "reason": "parallel_failure_rate",
                        "failure_count": len(failures),
                        "day_task_count": len(tasks),
                    },
                )
                yield self._build_progress_chunk(
                    worker_statuses,
                    total_days,
                    "并行模式失败率过高，切换到串行模式...",
                )
                return

            # 7. Retry failed days (one at a time)
            for task, error_msg in failures:
                idx = _find_worker_idx(task.day)
                worker_statuses[idx].update({
                    "status": "retrying",
                    "iteration": None,
                    "current_tool": None,
                    "error": None,
                    "error_code": None,
                    "activity_count": None,
                })
                yield self._build_progress_chunk(
                    worker_statuses,
                    total_days,
                    f"重试第 {task.day} 天...",
                )
                logger.info(
                    "Retrying day %d (previous error: %s)", task.day, error_msg
                )
                worker_start_event_ids[(task.day, 2)] = (
                    await self._emit_worker_start_trace(
                        task=task,
                        run_id=run_id,
                        attempt=2,
                    )
                )
                retry_result = await run_day_worker(
                    llm=self.llm,
                    tool_engine=self.tool_engine,
                    plan=self.plan,
                    task=task,
                    shared_prefix=shared_prefix,
                    max_iterations=self.config.worker_max_iterations,
                    timeout_seconds=self.config.worker_timeout_seconds,
                    on_progress=_make_progress_cb(idx),
                    candidate_store=candidate_store,
                    run_id=run_id,
                    attempt=2,
                    stats=self.stats,
                    trace_recorder=self.trace_recorder,
                    trace_context=self.trace_context,
                    trace_parent_event_id=worker_start_event_ids.get((task.day, 2)),
                )
                await self._emit_worker_result_trace(
                    task=task,
                    result=retry_result,
                    run_id=run_id,
                    attempt=2,
                    parent_event_id=worker_start_event_ids.get((task.day, 2)),
                )
                if retry_result.success:
                    successes.append(retry_result)
                    worker_statuses[idx]["status"] = "done"
                    worker_statuses[idx]["current_tool"] = None
                    if retry_result.dayplan:
                        worker_statuses[idx]["activity_count"] = len(
                            retry_result.dayplan.get("activities", [])
                        )
                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        f"第 {retry_result.day} 天（重试）规划完成",
                    )
                else:
                    worker_statuses[idx]["status"] = "failed"
                    worker_statuses[idx]["current_tool"] = None
                    worker_statuses[idx]["error"] = _format_error(
                        retry_result.error
                    )
                    worker_statuses[idx]["error_code"] = retry_result.error_code
                    logger.error(
                        "Day %d retry also failed [%s]: %s",
                        task.day,
                        retry_result.error_code,
                        retry_result.error,
                    )
                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        f"第 {task.day} 天重试失败",
                    )

            # 7b. Check for NEEDS_PHASE3_REPLAN from any worker
            all_replan_errors: list[str] = []
            for ws in worker_statuses:
                if ws.get("error_code") == "NEEDS_PHASE3_REPLAN":
                    all_replan_errors.append(
                        f"Day {ws['day']}: {ws.get('error', 'unknown')}"
                    )

            if all_replan_errors:
                reason = (
                    "骨架分配失败，以下天数无法按当前骨架展开:\n"
                    + "\n".join(all_replan_errors)
                )
                await self._emit_trace_event(
                    event_type="phase3_orchestrator",
                    status="error",
                    correlation_id=f"phase3_orchestrator:{run_id}",
                    parent_event_id=self._trace_orchestrator_event_id,
                    root_event_id=self._trace_orchestrator_event_id,
                    payload={
                        "stage": "needs_replan",
                        "phase3_run_id": run_id,
                        "error_code": "NEEDS_PHASE3_REPLAN",
                        "errors": all_replan_errors,
                    },
                )
                yield LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content=f"\n\n⚠️ {reason}\n需要回退到 Phase 2 重新调整骨架方案。\n",
                )
                # final_dayplans stays [] on this error path.
                # AgentLoop owns the terminal DONE for all parallel Phase 3 paths.
                return

            # 8. Sort and validate
            artifact_candidates = candidate_store.load_latest_candidates(
                self.plan.session_id, run_id
            )
            dayplans = sorted(
                (
                    [c["dayplan"] for c in artifact_candidates if c.get("dayplan")]
                    if artifact_candidates
                    else [r.dayplan for r in successes if r.dayplan]
                ),
                key=lambda dp: dp.get("day", 0),
            )

            yield self._build_progress_chunk(
                worker_statuses, total_days, "正在做最终验证..."
            )
            issues = self._global_validate(dayplans)
            for issue in issues:
                logger.warning("Global validation [%s]: %s", issue.severity, issue.description)
            await self._emit_trace_event(
                event_type="validation",
                status="fail" if any(i.severity == "error" for i in issues) else "pass",
                actor="phase3_orchestrator",
                correlation_id=f"phase3_orchestrator:{run_id}:validation",
                parent_event_id=self._trace_orchestrator_event_id,
                root_event_id=self._trace_orchestrator_event_id,
                payload={
                    "validation_rule_id": "phase3_global_validation",
                    "stage": "initial_global_validation",
                    "phase3_run_id": run_id,
                    "status": (
                        "fail"
                        if any(i.severity == "error" for i in issues)
                        else "pass"
                    ),
                    "issue_count": len(issues),
                    "issue_counts_by_type": {
                        issue_type: sum(1 for issue in issues if issue.issue_type == issue_type)
                        for issue_type in sorted({issue.issue_type for issue in issues})
                    },
                    "issues": [dict(issue.__dict__) for issue in issues],
                    "re_dispatch_hints": [
                        issue.description for issue in issues if issue.severity == "error"
                    ],
                },
            )

            # 8b. Re-dispatch for error-severity issues (max 1 round)
            error_issues = [i for i in issues if i.severity == "error"]
            if error_issues:
                task_by_day = {t.day: t for t in tasks}

                def _affected_days(issue_list: list[GlobalValidationIssue]) -> set[int]:
                    days: set[int] = set()
                    for issue in issue_list:
                        days.update(issue.affected_days)
                    return days

                def _repair_hints_for_day(
                    issue_list: list[GlobalValidationIssue],
                    day: int,
                ) -> list[str]:
                    return [
                        issue.description
                        for issue in issue_list
                        if day in issue.affected_days
                    ]

                def _apply_repair_result(
                    *,
                    rd_day: int,
                    rd_result: DayWorkerResult,
                ) -> None:
                    idx = _find_worker_idx(rd_day)
                    if rd_result.success and rd_result.dayplan:
                        latest_by_day = {
                            c["day"]: c["dayplan"]
                            for c in candidate_store.load_latest_candidates(
                                self.plan.session_id, run_id
                            )
                            if c.get("dayplan")
                        }
                        replacement_dayplan = latest_by_day.get(
                            rd_day, rd_result.dayplan
                        )
                        dayplans[:] = [
                            dp for dp in dayplans if dp.get("day") != rd_day
                        ]
                        dayplans.append(replacement_dayplan)
                        dayplans.sort(key=lambda dp: dp.get("day", 0))
                        worker_statuses[idx]["status"] = "done"
                        worker_statuses[idx]["current_tool"] = None
                        worker_statuses[idx]["error"] = None
                        worker_statuses[idx]["error_code"] = None
                        worker_statuses[idx]["activity_count"] = len(
                            replacement_dayplan.get("activities", [])
                        )
                    else:
                        worker_statuses[idx]["status"] = "failed"
                        worker_statuses[idx]["current_tool"] = None
                        worker_statuses[idx]["error"] = _format_error(
                            rd_result.error
                        )
                        worker_statuses[idx]["error_code"] = rd_result.error_code

                async def _run_repair_worker(
                    *,
                    rd_day: int,
                    repair_issues: list[GlobalValidationIssue],
                ) -> tuple[int, DayWorkerResult]:
                    rd_task = task_by_day.get(rd_day)
                    if rd_task is None:
                        return (
                            rd_day,
                            DayWorkerResult(
                                day=rd_day,
                                date="",
                                success=False,
                                dayplan=None,
                                error=f"No task found for day {rd_day}",
                                error_code="MISSING_DAY_TASK",
                            ),
                        )
                    rd_task.repair_hints = _repair_hints_for_day(
                        repair_issues, rd_day
                    )
                    idx = _find_worker_idx(rd_day)
                    worker_statuses[idx].update({
                        "status": "redispatch",
                        "iteration": None,
                        "current_tool": None,
                        "error": None,
                        "error_code": None,
                        "activity_count": None,
                    })
                    try:
                        worker_start_event_ids[(rd_day, 3)] = (
                            await self._emit_worker_start_trace(
                                task=rd_task,
                                run_id=run_id,
                                attempt=3,
                            )
                        )
                        rd_result = await run_day_worker(
                            llm=self.llm,
                            tool_engine=self.tool_engine,
                            plan=self.plan,
                            task=rd_task,
                            shared_prefix=shared_prefix,
                            max_iterations=self.config.worker_max_iterations,
                            timeout_seconds=self.config.worker_timeout_seconds,
                            on_progress=_make_progress_cb(idx),
                            candidate_store=candidate_store,
                            run_id=run_id,
                            attempt=3,
                            stats=self.stats,
                            trace_recorder=self.trace_recorder,
                            trace_context=self.trace_context,
                            trace_parent_event_id=worker_start_event_ids.get(
                                (rd_day, 3)
                            ),
                        )
                    except Exception as exc:
                        logger.warning(
                            "re-dispatch failed day=%s",
                            rd_day,
                            exc_info=True,
                        )
                        rd_result = DayWorkerResult(
                            day=rd_day,
                            date=rd_task.date,
                            success=False,
                            dayplan=None,
                            error=str(exc),
                            error_code="EXCEPTION",
                        )
                    await self._emit_worker_result_trace(
                        task=rd_task,
                        result=rd_result,
                        run_id=run_id,
                        attempt=3,
                        parent_event_id=worker_start_event_ids.get((rd_day, 3)),
                    )
                    return rd_day, rd_result

                local_issues = [
                    issue
                    for issue in error_issues
                    if issue.issue_type in _LOCAL_REPAIR_ISSUE_TYPES
                ]
                cross_day_issues = [
                    issue
                    for issue in error_issues
                    if issue.issue_type not in _LOCAL_REPAIR_ISSUE_TYPES
                ]
                cross_day_days = _affected_days(cross_day_issues)
                local_days = sorted(_affected_days(local_issues) - cross_day_days)
                repaired_days: set[int] = set()

                if local_days:
                    for rd_day in local_days:
                        idx = _find_worker_idx(rd_day)
                        worker_statuses[idx].update({
                            "status": "redispatch",
                            "iteration": None,
                            "current_tool": None,
                            "error": None,
                            "error_code": None,
                            "activity_count": None,
                        })
                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        "校验发现局部问题，正在并行重新规划第 "
                        f"{', '.join(str(day) for day in local_days)} 天...",
                    )
                    parallel_results = await asyncio.gather(
                        *[
                            _run_repair_worker(
                                rd_day=rd_day,
                                repair_issues=local_issues,
                            )
                            for rd_day in local_days
                        ],
                        return_exceptions=True,
                    )
                    for result in parallel_results:
                        if isinstance(result, Exception):
                            logger.warning(
                                "parallel local re-dispatch failed: %s",
                                result,
                            )
                            continue
                        rd_day, rd_result = result
                        _apply_repair_result(rd_day=rd_day, rd_result=rd_result)
                        repaired_days.add(rd_day)
                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        "局部问题并行重排完成",
                    )

                # Cross-day issues need conservative ownership semantics. Re-run
                # them one by one after local repairs, using the latest validation
                # result so resolved conflicts are not repaired unnecessarily.
                issues = self._global_validate(dayplans)
                remaining_errors = [
                    issue for issue in issues if issue.severity == "error"
                ]
                serial_issues = [
                    issue
                    for issue in remaining_errors
                    if issue.issue_type not in _LOCAL_REPAIR_ISSUE_TYPES
                ]
                serial_days = sorted(_affected_days(serial_issues) - repaired_days)

                for rd_day in serial_days:
                    idx = _find_worker_idx(rd_day)
                    worker_statuses[idx].update({
                        "status": "redispatch",
                        "iteration": None,
                        "current_tool": None,
                        "error": None,
                        "error_code": None,
                        "activity_count": None,
                    })
                    yield self._build_progress_chunk(
                        worker_statuses, total_days,
                        f"校验发现跨天冲突，保守重排第 {rd_day} 天...",
                    )
                    _, rd_result = await _run_repair_worker(
                        rd_day=rd_day,
                        repair_issues=remaining_errors,
                    )
                    _apply_repair_result(rd_day=rd_day, rd_result=rd_result)
                    repaired_days.add(rd_day)

                    yield self._build_progress_chunk(
                        worker_statuses, total_days,
                        f"第 {rd_day} 天重新规划{'完成' if rd_result.success else '失败'}",
                    )

                # Re-validate after re-dispatch
                issues = self._global_validate(dayplans)
                unresolved = [i for i in issues if i.severity == "error"]
                if unresolved:
                    for ui in unresolved:
                        logger.warning("Unresolved after re-dispatch: %s", ui.description)
            await self._emit_trace_event(
                event_type="validation",
                status="fail" if any(i.severity == "error" for i in issues) else "pass",
                actor="phase3_orchestrator",
                correlation_id=f"phase3_orchestrator:{run_id}:final_validation",
                parent_event_id=self._trace_orchestrator_event_id,
                root_event_id=self._trace_orchestrator_event_id,
                payload={
                    "validation_rule_id": "phase3_global_validation",
                    "stage": "final_global_validation",
                    "phase3_run_id": run_id,
                    "status": (
                        "fail"
                        if any(i.severity == "error" for i in issues)
                        else "pass"
                    ),
                    "issue_count": len(issues),
                    "issue_counts_by_type": {
                        issue_type: sum(1 for issue in issues if issue.issue_type == issue_type)
                        for issue_type in sorted({issue.issue_type for issue in issues})
                    },
                    "issues": [dict(issue.__dict__) for issue in issues],
                    "re_dispatch_hints": [],
                },
            )

            # 9. Expose results for AgentLoop to commit via the standard write-tool path.
            self.final_dayplans = list(dayplans)
            self.final_issues = list(issues)
            if dayplans:
                await self._emit_trace_event(
                    event_type="phase3_orchestrator",
                    status="success",
                    correlation_id=f"phase3_orchestrator:{run_id}",
                    parent_event_id=self._trace_orchestrator_event_id,
                    root_event_id=self._trace_orchestrator_event_id,
                    payload={
                        "stage": "handoff",
                        "phase3_run_id": run_id,
                        "handoff_tool_name": "replace_all_day_plans",
                        "dayplan_count": len(dayplans),
                        "dayplans_hash": stable_content_hash(dayplans),
                        "issue_count": len(issues),
                    },
                )
                yield self._build_progress_chunk(
                    worker_statuses,
                    total_days,
                    f"已生成 {len(dayplans)} 天行程，准备写入规划状态...",
                )

            # 10. Generate summary text
            summary_lines = [f"已完成 {len(dayplans)}/{len(tasks)} 天的行程规划。\n"]
            for dp in dayplans:
                day_num = dp.get("day", "?")
                tips = dp.get("tips", "") or dp.get("notes", "")
                acts = dp.get("activities", [])
                act_names = [a.get("name", "") for a in acts[:5]]
                summary_lines.append(
                    f"**第 {day_num} 天**：{tips or ''}  \n{'→'.join(act_names)}\n"
                )
            if issues:
                summary_lines.append("\n⚠️ 发现以下问题需要关注：")
                for issue in issues:
                    summary_lines.append(f"- {issue.description}")

            summary_text = "\n".join(summary_lines)
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content=summary_text)
