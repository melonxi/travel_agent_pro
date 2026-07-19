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
import copy
import logging
import re
import uuid
from dataclasses import dataclass, field
from math import radians, sin, cos, sqrt, atan2
from typing import Any, AsyncIterator

from opentelemetry import trace

from agent.phase3.candidate_store import Phase3CandidateStore
from agent.phase3.day_worker import DayWorkerResult, run_day_worker
from agent.phase3.renegotiation import (
    Phase3Blackboard,
    ReplanRequest,
    RenegotiationOutcome,
    renegotiate_skeleton,
)
from agent.phase3.worker_prompt import (
    DayTask,
    build_shared_prefix,
    max_core_activities_for_pace,
    split_skeleton_to_day_tasks,
)
from config import Phase3ParallelConfig
from harness.validator import (
    _selected_accommodation_nightly_price,
    _selected_transport_cost,
    _trip_nights,
)
from llm.base import LLMProvider
from llm.types import ChunkType, LLMChunk
from state.models import TravelPlanState, normalize_pace_value
from storage.trace_redaction import stable_content_hash
from telemetry.trace_recorder import TraceContext, TraceRecorder
from tools.engine import ToolEngine
from tools.source_registry import SourceRegistry

logger = logging.getLogger(__name__)

_LOCAL_REPAIR_ISSUE_TYPES = frozenset(
    {"pace_mismatch", "time_conflict", "transport_connection", "locked_poi_missing"}
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


@dataclass
class _RedispatchRollback:
    """C1：重派前乐观作废的旧版本引用。

    attempt 是作废前磁盘上 accepted 候选的 attempt（无 artifact 时为 None）；
    result 是被移出 successes 的旧成功结果（该天此前未成功时为 None）。
    """

    day: int
    attempt: int | None = None
    result: "DayWorkerResult | None" = None


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
    lines = ["⚠️ 你明确要求的以下约束，自动重排后仍未完全满足，已按当前最优方案交付："]
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
            seg
            for seg in segments
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
        steer_queue: Any | None = None,
    ):
        self.plan = plan
        self.llm = llm
        self.tool_engine = tool_engine
        self.config = config or Phase3ParallelConfig()
        self.stats = stats
        self.trace_recorder = trace_recorder
        self.trace_context = trace_context
        self.steer_queue = steer_queue  # D4
        self.final_dayplans: list[dict[str, Any]] = []
        self.final_issues: list[GlobalValidationIssue] = []
        self._locked_pois_by_day: dict[int, list[str]] = {}
        self._trace_orchestrator_event_id: str | None = None
        # D3：再协商 + 共享黑板（run 级状态）
        self._renegotiate_count: dict[int, int] = {}
        self._skeleton_copy: dict[str, Any] | None = None
        self._skeleton_amendments: list[Any] = []
        self._blackboard: Phase3Blackboard = Phase3Blackboard()
        # D4：day → 用户引导原文列表；已完成天可触发重派
        self._pending_steer_hints: dict[int, list[str]] = {}
        self._steer_redispatch_days: set[int] = set()

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
        await self._emit_trace_event(
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

    def _init_blackboard(self, tasks: list[DayTask]) -> None:
        """D3 黑板：seed locked POI + 预算口径（活动外已锁定交通/住宿）。"""
        transport_cost = _selected_transport_cost(self.plan)
        accommodation_cost = (
            _selected_accommodation_nightly_price(self.plan) * _trip_nights(self.plan)
            if self.plan.accommodation
            else 0.0
        )
        total = None
        if self.plan.budget and self.plan.budget.total:
            total = float(self.plan.budget.total)
        self._blackboard = Phase3Blackboard(
            total_budget=total,
            precommitted_cost=transport_cost + accommodation_cost,
        )
        self._blackboard.seed_from_locked({t.day: list(t.locked_pois) for t in tasks})

    def _accept_worker_dayplan(
        self,
        result: DayWorkerResult,
        *,
        candidate_store: Phase3CandidateStore | None = None,
        run_id: str | None = None,
        attempt: int | None = None,
    ) -> DayWorkerResult:
        """D3：候选收集时查表即拒，通过则登记黑板。"""
        if not result.success:
            return result
        if not result.dayplan:
            if (
                candidate_store is not None
                and run_id is not None
                and attempt is not None
            ):
                candidate_store.update_candidate_status(
                    self.plan.session_id,
                    run_id,
                    result.day,
                    attempt,
                    status="accepted",
                )
            return result
        ok, reason = self._blackboard.try_accept_dayplan(result.day, result.dayplan)
        if ok:
            if (
                candidate_store is not None
                and run_id is not None
                and attempt is not None
            ):
                candidate_store.update_candidate_status(
                    self.plan.session_id,
                    run_id,
                    result.day,
                    attempt,
                    status="accepted",
                )
            return result
        if candidate_store is not None and run_id is not None and attempt is not None:
            candidate_store.update_candidate_status(
                self.plan.session_id,
                run_id,
                result.day,
                attempt,
                status="rejected",
                reason=reason,
            )
        logger.warning("Blackboard rejected day %d candidate: %s", result.day, reason)
        return DayWorkerResult(
            day=result.day,
            date=result.date,
            success=False,
            dayplan=None,
            error=reason,
            error_code="BLACKBOARD_REJECT",
            iterations=result.iterations,
        )

    def _invalidate_day_for_redispatch(
        self,
        *,
        candidate_store: Phase3CandidateStore,
        run_id: str,
        day: int,
        reason: str,
        prev_result: DayWorkerResult | None = None,
    ) -> "_RedispatchRollback":
        """C1：重派前乐观作废旧候选，并记下旧版本引用供失败回滚。"""
        prev_attempt: int | None = None
        payload = candidate_store.get_latest_candidate(
            self.plan.session_id, run_id, day
        )
        if payload is not None and payload.get("status") == "accepted":
            try:
                prev_attempt = int(payload.get("attempt", 0))
            except (TypeError, ValueError):
                prev_attempt = None
        candidate_store.update_latest_candidate_status(
            self.plan.session_id,
            run_id,
            day,
            status="rejected",
            reason=reason,
        )
        self._blackboard.release_day(day)
        return _RedispatchRollback(day=day, attempt=prev_attempt, result=prev_result)

    def _handle_redispatch_failure(
        self,
        *,
        candidate_store: Phase3CandidateStore,
        run_id: str,
        rollback: "_RedispatchRollback | None",
        successes: list[DayWorkerResult],
    ) -> bool:
        """C1：重派失败时回滚乐观作废——恢复旧版本总比静默丢天好。

        磁盘：把作废前的 accepted 候选重新标 accepted 并 bump 到最新 seq；
        内存：旧成功结果放回 successes 并重新登记黑板。
        """
        if rollback is None:
            return False
        restored = False
        if rollback.attempt is not None:
            restored = candidate_store.restore_candidate_as_latest(
                self.plan.session_id,
                run_id,
                rollback.day,
                rollback.attempt,
                reason="restored after redispatch failure",
            )
        if rollback.result is not None:
            if rollback.result.dayplan:
                ok, reason = self._blackboard.try_accept_dayplan(
                    rollback.day, rollback.result.dayplan
                )
                if not ok:
                    logger.warning(
                        "Day %d rollback could not re-register blackboard: %s",
                        rollback.day,
                        reason,
                    )
            if all(s.day != rollback.day for s in successes):
                successes.append(rollback.result)
            restored = True
        if restored:
            logger.warning(
                "Redispatch of day %d failed; restored previous accepted version",
                rollback.day,
            )
        return restored

    def _renegotiate_skeleton(
        self, requests: list[ReplanRequest]
    ) -> RenegotiationOutcome:
        """D3 A2：确定性再协商，只改骨架副本。"""
        skeleton = self._skeleton_copy or self._find_selected_skeleton() or {"days": []}
        raw_pace = (self.plan.trip_brief or {}).get("pace")
        pace = normalize_pace_value(raw_pace) or "balanced"
        total_days = (
            self.plan.dates.total_days
            if self.plan.dates
            else len(skeleton.get("days") or [])
        )
        outcome = renegotiate_skeleton(
            skeleton=skeleton,
            requests=requests,
            pace=pace,
            renegotiate_count=self._renegotiate_count,
            total_days=max(total_days, 1),
        )
        self._skeleton_copy = outcome.skeleton_copy
        self._skeleton_amendments.extend(outcome.amendments)
        return outcome

    def _rebuild_tasks_for_days(
        self, days: set[int], base_tasks: list[DayTask]
    ) -> list[DayTask]:
        """从骨架副本重拆并 compile，只返回受影响天的 DayTask。"""
        skeleton = self._skeleton_copy or self._find_selected_skeleton()
        if skeleton is None:
            return []
        all_tasks = split_skeleton_to_day_tasks(skeleton, self.plan)
        # 保留未受影响天的已有 task 引用不需要；只 compile 全集以重建 forbidden
        compiled = self._compile_day_tasks(all_tasks)
        # 刷新 locked 记录与黑板 seed（释放后重 seed）
        self._locked_pois_by_day = {t.day: list(t.locked_pois) for t in compiled}
        for task in compiled:
            self._refresh_task_blackboard_snapshot(task)
        return [t for t in compiled if t.day in days]

    def _refresh_task_blackboard_snapshot(self, task: DayTask) -> None:
        """把运行时已认领 POI 作为只读 forbidden 快照下发给重派 worker。"""
        forbidden = list(task.forbidden_pois or [])
        for poi in self._blackboard.snapshot_forbidden_for_day(task.day):
            if poi not in forbidden:
                forbidden.append(poi)
        task.forbidden_pois = forbidden

    def _apply_steer_hints_to_task(self, task: DayTask) -> None:
        """把挂起的用户引导挂到 repair_hints，供重派/重试 worker 使用。"""
        hints = self._pending_steer_hints.get(task.day) or []
        if not hints:
            return
        from agent.steering import format_steer_notice

        existing = list(task.repair_hints or [])
        for h in hints:
            notice = format_steer_notice(h)
            if notice not in existing:
                existing.append(notice)
        task.repair_hints = existing

    def _drain_steering(
        self,
        *,
        tasks: list[DayTask],
        successes: list[DayWorkerResult],
        total_days: int,
        active_days: set[int] | None = None,
        terminal_message: str | None = None,
    ) -> list[LLMChunk]:
        """D4：在 worker 收集循环安全点 drain steering。

        - 解析「第 N 天」；无天号则作用于尚未成功的天
        - 已成功天：标记 redispatch（不回滚已 yield 的进度，稍后重派）
        - 未成功天：挂 repair_hint，供后续 retry/redispatch 使用
        """
        from agent.steering import (
            drain_steer_queue,
            format_steer_notice,
            parse_steer_day_targets,
            steering_ack_chunk,
        )

        texts = drain_steer_queue(self.steer_queue)
        if not texts:
            return []

        done_days = {s.day for s in successes}
        active_days = active_days or set()
        all_days = {t.day for t in tasks}
        incomplete = sorted(all_days - done_days)
        task_by_day = {t.day: t for t in tasks}
        acks: list[LLMChunk] = []

        for text in texts:
            targets = parse_steer_day_targets(text)
            if not targets:
                targets = incomplete or sorted(all_days)
            targets = [d for d in targets if d in all_days]
            if terminal_message is not None:
                acks.append(
                    steering_ack_chunk(
                        text,
                        days=targets or None,
                        message=terminal_message,
                    )
                )
                continue
            acks.append(steering_ack_chunk(text, days=targets or None))
            for day in targets:
                self._pending_steer_hints.setdefault(day, []).append(text)
                if day in done_days or day in active_days:
                    self._steer_redispatch_days.add(day)
                else:
                    task = task_by_day.get(day)
                    if task is not None:
                        notice = format_steer_notice(text)
                        if notice not in (task.repair_hints or []):
                            task.repair_hints = list(task.repair_hints or []) + [notice]
            if targets:
                acks.append(
                    LLMChunk(
                        type=ChunkType.TEXT_DELTA,
                        content=(
                            f"\n\n↪️ 因用户引导将调整第 "
                            f"{'、'.join(str(d) for d in targets)} 天…\n"
                        ),
                    )
                )
        return acks

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

        # 1. Build global POI ownership map (locked only).
        # P2-3：同一 POI 被多天锁定会造成自相矛盾（既在某天 locked 又因归属
        # 另一天而进入该天 forbidden）。保留首个锁定天为唯一 owner，并从后续
        # 天的 locked_pois 移除重复项，避免下发互斥约束给 worker。
        poi_owner: dict[str, int] = {}
        for t in tasks:
            deduped_locked: list[str] = []
            for poi in t.locked_pois:
                if poi in poi_owner:
                    logger.warning(
                        "POI '%s' locked by both Day %d and Day %d; "
                        "keeping Day %d and dropping the duplicate",
                        poi,
                        poi_owner[poi],
                        t.day,
                        poi_owner[poi],
                    )
                    continue
                poi_owner[poi] = t.day
                deduped_locked.append(poi)
            t.locked_pois = deduped_locked

        # 2. Derive forbidden_pois for each day
        for t in tasks:
            t.forbidden_pois = [
                poi for poi, owner_day in poi_owner.items() if owner_day != t.day
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
            if arrival_min is not None and sorted_tasks[0].date_role == "arrival_day":
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

        # 8. Locked POI 落位校验（P2-1）：每天编排的必去项必须出现在 activities。
        issues.extend(self._validate_locked_pois_present(dayplans))

        return issues

    def _validate_locked_pois_present(
        self, dayplans: list[dict[str, Any]]
    ) -> list[GlobalValidationIssue]:
        issues: list[GlobalValidationIssue] = []
        if not self._locked_pois_by_day:
            return issues
        by_day = {dp.get("day", 0): dp for dp in dayplans}
        for day, locked in self._locked_pois_by_day.items():
            if not locked:
                continue
            dp = by_day.get(day)
            if dp is None:
                continue
            act_names = [a.get("name", "") for a in dp.get("activities", [])]
            missing = [
                poi
                for poi in locked
                if not any(_names_similar(poi, name) for name in act_names if name)
            ]
            if missing:
                issues.append(
                    GlobalValidationIssue(
                        issue_type="locked_poi_missing",
                        description=(
                            f"第 {day} 天缺少必去项: {missing}；"
                            "locked POI 必须出现在当天 activities 中"
                        ),
                        affected_days=[day],
                        severity="error",
                    )
                )
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
                    issues.append(
                        GlobalValidationIssue(
                            issue_type="semantic_duplicate",
                            description=(
                                f"'{name_a}'(Day {day_a}) 与 '{name_b}'(Day {day_b}) "
                                f"疑似同一地点（距离 {dist:.0f}m）"
                            ),
                            affected_days=[day_b],
                            severity="error",
                        )
                    )
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
                        issues.append(
                            GlobalValidationIssue(
                                issue_type="time_conflict",
                                description=(
                                    f"Day {day}: '{prev.get('name')}'→'{curr.get('name')}' "
                                    f"时间冲突（{prev.get('end_time')} 结束 + 交通 {travel}min "
                                    f"> {curr.get('start_time')} 开始）"
                                ),
                                affected_days=[day],
                                severity="error",
                            )
                        )
        return issues

    def _validate_transport_connection(
        self, dayplans: list[dict[str, Any]]
    ) -> list[GlobalValidationIssue]:
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
                    issues.append(
                        GlobalValidationIssue(
                            issue_type="transport_connection",
                            description=(
                                f"Day {first_day.get('day', 1)} 首活动开始时间过早，"
                                f"距到达不足 2 小时"
                            ),
                            affected_days=[first_day.get("day", 1)],
                            severity="error",
                        )
                    )

        departure_min = _extract_transport_time(transport, "return")
        if departure_min is not None:
            last_day = sorted_days[-1]
            acts = last_day.get("activities", [])
            if acts:
                last_end = _time_to_minutes(acts[-1].get("end_time", ""))
                if last_end is not None and last_end > departure_min - 180:
                    issues.append(
                        GlobalValidationIssue(
                            issue_type="transport_connection",
                            description=(
                                f"Day {last_day.get('day', len(sorted_days))} 末活动结束过晚，"
                                f"距离开不足 3 小时"
                            ),
                            affected_days=[last_day.get("day", len(sorted_days))],
                            severity="error",
                        )
                    )

        return issues

    def _validate_pace(
        self, dayplans: list[dict[str, Any]]
    ) -> list[GlobalValidationIssue]:
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
                issues.append(
                    GlobalValidationIssue(
                        issue_type="pace_mismatch",
                        description=(
                            f"Day {day}: {act_count} 个活动超出 {pace} 节奏上限 {max_activities}"
                        ),
                        affected_days=[day],
                        severity=severity,
                    )
                )
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
            try:
                tasks = self._split_tasks()
            except ValueError as exc:
                # P2-6：骨架失配转为可对话提示，而非异常穿透成硬砖。
                message = (
                    f"\n\n⚠️ 无法启动并行精排：{exc}。"
                    "请先确认已用 `select_skeleton` 锁定骨架，"
                    "且 `skeleton_plans` 中存在对应方案，"
                    "或回退到 Phase 2 重新写入骨架后再继续。"
                )
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content=message)
                yield LLMChunk(type=ChunkType.DONE)
                return
            tasks = self._compile_day_tasks(tasks)
            # P2-1：记录每天编排后的 locked POI，供全局校验核对必去项是否落位。
            self._locked_pois_by_day = {t.day: list(t.locked_pois) for t in tasks}
            # D3：骨架副本 + 黑板（run 级，不写权威 skeleton_plans）
            selected = self._find_selected_skeleton()
            self._skeleton_copy = copy.deepcopy(selected) if selected else {"days": []}
            self._skeleton_amendments = []
            self._renegotiate_count = {}
            self._init_blackboard(tasks)
            total_days = len(tasks)
            span.set_attribute("total_days", total_days)

            # 2. Build shared prefix
            shared_prefix = build_shared_prefix(self.plan)
            run_id = f"phase3_{uuid.uuid4().hex[:12]}"
            candidate_store = Phase3CandidateStore(
                self.config.artifact_root,
                source_registry=SourceRegistry(self.config.source_registry_root),
            )
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
                return next(i for i, w in enumerate(worker_statuses) if w["day"] == day)

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
                            worker_statuses[idx]["current_tool"] = payload.get(
                                "human_label"
                            ) or payload.get("tool")
                        progress_queue.put_nowait({"day": day, "kind": kind})
                    except Exception as exc:
                        logger.warning("orchestrator progress callback failed: %s", exc)

                return _on_progress

            async def _run_with_semaphore(task: DayTask) -> DayWorkerResult:
                idx = _find_worker_idx(task.day)
                async with semaphore:
                    active_worker_days.add(task.day)
                    try:
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
                    finally:
                        active_worker_days.discard(task.day)

            pending: dict[asyncio.Task, DayTask] = {}
            active_worker_days: set[int] = set()
            for task in tasks:
                worker_start_event_ids[
                    (task.day, 1)
                ] = await self._emit_worker_start_trace(
                    task=task,
                    run_id=run_id,
                    attempt=1,
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
                        result = self._accept_worker_dayplan(
                            completed.result(),
                            candidate_store=candidate_store,
                            run_id=run_id,
                            attempt=1,
                        )
                        if result.success:
                            successes.append(result)
                            worker_statuses[idx]["status"] = "done"
                            worker_statuses[idx]["current_tool"] = None
                            if result.dayplan:
                                worker_statuses[idx]["activity_count"] = len(
                                    result.dayplan.get("activities", [])
                                )
                        else:
                            failures.append((day_task, result.error or "Unknown error"))
                            worker_statuses[idx]["status"] = "failed"
                            worker_statuses[idx]["current_tool"] = None
                            worker_statuses[idx]["error"] = _format_error(result.error)
                            worker_statuses[idx]["error_code"] = result.error_code
                            if result.replan_request is not None:
                                worker_statuses[idx]["replan_request"] = (
                                    result.replan_request
                                )
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
                        worker_statuses[idx]["error"] = _format_error(f"Exception: {e}")
                        worker_statuses[idx]["error_code"] = "EXCEPTION"
                        logger.error("Day %d worker exception: %s", day_task.day, e)
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
                    1 for w in worker_statuses if w["status"] in ("done", "failed")
                )
                yield self._build_progress_chunk(
                    worker_statuses,
                    total_days,
                    f"已完成 {done_count}/{total_days} 天...",
                )
                # D4：每收完一个 worker 结果后 drain steering
                for ack in self._drain_steering(
                    tasks=tasks,
                    successes=successes,
                    total_days=total_days,
                    active_days=active_worker_days,
                ):
                    yield ack

            if getter_task and not getter_task.done():
                getter_task.cancel()
                try:
                    await getter_task
                except (asyncio.CancelledError, Exception):
                    pass

            # D4：用户引导要求重做已成功天 → 从 successes 摘下，进入 step 7 重派
            # C1：所有"乐观作废旧候选"的重派段共用回滚表，重派失败时恢复旧版本。
            redispatch_rollbacks: dict[int, _RedispatchRollback] = {}
            if self._steer_redispatch_days:
                redispatch = set(self._steer_redispatch_days)
                self._steer_redispatch_days.clear()
                kept: list[DayWorkerResult] = []
                for s in successes:
                    if s.day in redispatch:
                        redispatch_rollbacks[s.day] = (
                            self._invalidate_day_for_redispatch(
                                candidate_store=candidate_store,
                                run_id=run_id,
                                day=s.day,
                                reason="superseded by steering redispatch",
                                prev_result=s,
                            )
                        )
                        task = next((t for t in tasks if t.day == s.day), None)
                        if task is not None:
                            self._apply_steer_hints_to_task(task)
                            failures.append((task, "用户引导要求重排该天"))
                            idx = _find_worker_idx(s.day)
                            worker_statuses[idx]["status"] = "failed"
                            worker_statuses[idx]["error"] = "steering redispatch"
                            worker_statuses[idx]["error_code"] = "STEERING_REDISPATCH"
                        # 不保留旧成功结果
                    else:
                        kept.append(s)
                successes = kept
                yield LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content=(
                        f"\n\n↪️ 因用户引导重派第 "
                        f"{'、'.join(str(d) for d in sorted(redispatch))} 天…\n"
                    ),
                )

            span.set_attribute("successes", len(successes))
            span.set_attribute("failures", len(failures))

            # 6. High parallel failure rate → degrade to serial repair instead of
            # aborting. 之前此处 return 空结果，导致 step 7 逐天重试(实质的串行路径)
            # 完全不可达，下一回合进入条件不变而全量重跑并行——无限空转。
            # 现在改为继续走 step 7 的逐天串行重试；仍缺的天由 P0-2 部分交付兜底。
            if self.config.fallback_to_serial and len(failures) > len(tasks) / 2:
                logger.warning(
                    "Parallel mode failure rate %.0f%%, degrading to serial per-day retry",
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
                    "并行模式失败率过高，切换到逐天串行重排...",
                )
                # 不 return：继续走下面的逐天串行重试(step 7)。

            # 7. Retry failed days (one at a time)
            # D3：NEEDS_PHASE3_REPLAN 需改骨架后重派，同骨架串行重试无意义，留给 7b。
            for task, error_msg in failures:
                idx = _find_worker_idx(task.day)
                if worker_statuses[idx].get("error_code") == "NEEDS_PHASE3_REPLAN":
                    continue
                self._apply_steer_hints_to_task(task)
                if error_msg and error_msg not in (task.repair_hints or []):
                    task.repair_hints = list(task.repair_hints or []) + [error_msg]
                self._refresh_task_blackboard_snapshot(task)
                worker_statuses[idx].update(
                    {
                        "status": "retrying",
                        "iteration": None,
                        "current_tool": None,
                        "error": None,
                        "error_code": None,
                        "activity_count": None,
                    }
                )
                yield self._build_progress_chunk(
                    worker_statuses,
                    total_days,
                    f"重试第 {task.day} 天...",
                )
                logger.info("Retrying day %d (previous error: %s)", task.day, error_msg)
                worker_start_event_ids[
                    (task.day, 2)
                ] = await self._emit_worker_start_trace(
                    task=task,
                    run_id=run_id,
                    attempt=2,
                )
                retry_result = self._accept_worker_dayplan(
                    await run_day_worker(
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
                    ),
                    candidate_store=candidate_store,
                    run_id=run_id,
                    attempt=2,
                )
                for ack in self._drain_steering(
                    tasks=tasks,
                    successes=successes,
                    total_days=total_days,
                    active_days={task.day},
                ):
                    yield ack
                await self._emit_worker_result_trace(
                    task=task,
                    result=retry_result,
                    run_id=run_id,
                    attempt=2,
                    parent_event_id=worker_start_event_ids.get((task.day, 2)),
                )
                if retry_result.success:
                    successes.append(retry_result)
                    redispatch_rollbacks.pop(task.day, None)
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
                    worker_statuses[idx]["error"] = _format_error(retry_result.error)
                    worker_statuses[idx]["error_code"] = retry_result.error_code
                    if retry_result.replan_request is not None:
                        worker_statuses[idx]["replan_request"] = (
                            retry_result.replan_request
                        )
                    logger.error(
                        "Day %d retry also failed [%s]: %s",
                        task.day,
                        retry_result.error_code,
                        retry_result.error,
                    )
                    # C1：steering 重派（作废过旧候选）失败 → 回滚恢复旧版本
                    rollback = redispatch_rollbacks.pop(task.day, None)
                    if self._handle_redispatch_failure(
                        candidate_store=candidate_store,
                        run_id=run_id,
                        rollback=rollback,
                        successes=successes,
                    ):
                        worker_statuses[idx]["status"] = "done"
                        worker_statuses[idx]["error"] = None
                        worker_statuses[idx]["error_code"] = None
                        if rollback.result is not None and rollback.result.dayplan:
                            worker_statuses[idx]["activity_count"] = len(
                                rollback.result.dayplan.get("activities", [])
                            )
                        yield self._build_progress_chunk(
                            worker_statuses,
                            total_days,
                            f"第 {task.day} 天重派失败，已恢复重派前版本",
                        )
                    else:
                        yield self._build_progress_chunk(
                            worker_statuses,
                            total_days,
                            f"第 {task.day} 天重试失败",
                        )

            # Steering may arrive while the serial retry loop is awaiting an
            # LLM. Consume those messages before entering renegotiation instead
            # of dropping them with the run-scoped queue.
            for ack in self._drain_steering(
                tasks=tasks,
                successes=successes,
                total_days=total_days,
                active_days=set(),
            ):
                yield ack

            late_steer_days = set(self._steer_redispatch_days)
            if late_steer_days:
                self._steer_redispatch_days.clear()
                kept_successes: list[DayWorkerResult] = []
                for result in successes:
                    if result.day in late_steer_days:
                        redispatch_rollbacks[result.day] = (
                            self._invalidate_day_for_redispatch(
                                candidate_store=candidate_store,
                                run_id=run_id,
                                day=result.day,
                                reason="superseded by late steering redispatch",
                                prev_result=result,
                            )
                        )
                    else:
                        kept_successes.append(result)
                successes = kept_successes
                for steer_day in sorted(late_steer_days):
                    steer_task = next((t for t in tasks if t.day == steer_day), None)
                    if steer_task is None:
                        continue
                    self._apply_steer_hints_to_task(steer_task)
                    self._refresh_task_blackboard_snapshot(steer_task)
                    idx = _find_worker_idx(steer_day)
                    worker_statuses[idx]["status"] = "redispatch"
                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        f"用户引导触发重派第 {steer_day} 天...",
                    )
                    steer_result = self._accept_worker_dayplan(
                        await run_day_worker(
                            llm=self.llm,
                            tool_engine=self.tool_engine,
                            plan=self.plan,
                            task=steer_task,
                            shared_prefix=shared_prefix,
                            max_iterations=self.config.worker_max_iterations,
                            timeout_seconds=self.config.worker_timeout_seconds,
                            on_progress=_make_progress_cb(idx),
                            candidate_store=candidate_store,
                            run_id=run_id,
                            attempt=5,
                            stats=self.stats,
                            trace_recorder=self.trace_recorder,
                            trace_context=self.trace_context,
                            trace_parent_event_id=worker_start_event_ids.get(
                                (steer_day, 5)
                            ),
                        ),
                        candidate_store=candidate_store,
                        run_id=run_id,
                        attempt=5,
                    )
                    if steer_result.success:
                        successes.append(steer_result)
                        redispatch_rollbacks.pop(steer_day, None)
                        worker_statuses[idx]["status"] = "done"
                    else:
                        worker_statuses[idx]["status"] = "failed"
                        worker_statuses[idx]["error"] = _format_error(
                            steer_result.error
                        )
                        worker_statuses[idx]["error_code"] = steer_result.error_code
                    for ack in self._drain_steering(
                        tasks=tasks,
                        successes=successes,
                        total_days=total_days,
                        active_days={steer_day},
                        terminal_message=(
                            "本轮已进入最终收口，未能应用这条引导，请在下一轮重新发送"
                        ),
                    ):
                        yield ack
                # C1：重派失败（或任务缺失未重派）的天回滚，恢复旧版本
                for steer_day in sorted(late_steer_days):
                    rollback = redispatch_rollbacks.pop(steer_day, None)
                    if self._handle_redispatch_failure(
                        candidate_store=candidate_store,
                        run_id=run_id,
                        rollback=rollback,
                        successes=successes,
                    ):
                        idx = _find_worker_idx(steer_day)
                        worker_statuses[idx]["status"] = "done"
                        worker_statuses[idx]["error"] = None
                        worker_statuses[idx]["error_code"] = None
                        if rollback.result is not None and rollback.result.dayplan:
                            worker_statuses[idx]["activity_count"] = len(
                                rollback.result.dayplan.get("activities", [])
                            )
                        yield self._build_progress_chunk(
                            worker_statuses,
                            total_days,
                            f"第 {steer_day} 天重派失败，已恢复重派前版本",
                        )

            # 7b. D3：结构化再协商 — 改骨架副本 + 只重派受影响天
            replan_requests: list[ReplanRequest] = []
            for ws in worker_statuses:
                if ws.get("error_code") != "NEEDS_PHASE3_REPLAN":
                    continue
                req = ws.get("replan_request")
                if isinstance(req, ReplanRequest):
                    replan_requests.append(req)
                else:
                    replan_requests.append(
                        ReplanRequest(
                            day=int(ws["day"]),
                            kind="INFEASIBLE_DAY",
                            reason=str(ws.get("error") or "unknown"),
                        )
                    )

            if replan_requests:
                outcome = self._renegotiate_skeleton(replan_requests)
                await self._emit_trace_event(
                    event_type="phase3_orchestrator",
                    status="ok" if outcome.affected_days else "error",
                    correlation_id=f"phase3_orchestrator:{run_id}",
                    parent_event_id=self._trace_orchestrator_event_id,
                    root_event_id=self._trace_orchestrator_event_id,
                    payload={
                        "stage": "renegotiate",
                        "phase3_run_id": run_id,
                        "error_code": "NEEDS_PHASE3_REPLAN",
                        "requests": [
                            {
                                "day": r.day,
                                "kind": r.kind,
                                "reason": r.reason,
                                "move_poi": r.move_poi,
                                "to_day": r.to_day,
                            }
                            for r in replan_requests
                        ],
                        "affected_days": sorted(outcome.affected_days),
                        "amendments": [
                            {
                                "kind": a.kind,
                                "description": a.description,
                                "source_day": a.source_day,
                                "target_day": a.target_day,
                                "poi": a.poi,
                            }
                            for a in outcome.amendments
                        ],
                        "unresolved": [
                            {"day": r.day, "kind": r.kind, "reason": r.reason}
                            for r in outcome.unresolved
                        ],
                        "skeleton_amendments": True,
                    },
                )

                if outcome.affected_days:
                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        f"骨架再协商：重派第 "
                        f"{'、'.join(str(d) for d in sorted(outcome.affected_days))} 天...",
                    )
                    # 释放受影响天的黑板认领，并从 successes 移除（若目标天曾成功）
                    # C1：记录旧版本引用，重派失败时回滚
                    prev_success_by_day = {s.day: s for s in successes}
                    for d in outcome.affected_days:
                        redispatch_rollbacks[d] = self._invalidate_day_for_redispatch(
                            candidate_store=candidate_store,
                            run_id=run_id,
                            day=d,
                            reason="superseded by skeleton renegotiation",
                            prev_result=prev_success_by_day.get(d),
                        )
                    successes = [
                        s for s in successes if s.day not in outcome.affected_days
                    ]
                    redispatched = self._rebuild_tasks_for_days(
                        outcome.affected_days, tasks
                    )
                    # 同步 tasks 中受影响天的约束，供后续 repair 使用
                    task_by_day_map = {t.day: t for t in tasks}
                    for nt in redispatched:
                        task_by_day_map[nt.day] = nt
                    tasks = [task_by_day_map[t.day] for t in tasks]

                    for rd_task in redispatched:
                        self._apply_steer_hints_to_task(rd_task)
                        self._refresh_task_blackboard_snapshot(rd_task)
                        idx = _find_worker_idx(rd_task.day)
                        worker_statuses[idx].update(
                            {
                                "status": "renegotiate",
                                "iteration": None,
                                "current_tool": None,
                                "error": None,
                                "error_code": None,
                                "activity_count": None,
                                "replan_request": None,
                            }
                        )
                        yield self._build_progress_chunk(
                            worker_statuses,
                            total_days,
                            f"再协商后重派第 {rd_task.day} 天...",
                        )
                        worker_start_event_ids[
                            (rd_task.day, 4)
                        ] = await self._emit_worker_start_trace(
                            task=rd_task,
                            run_id=run_id,
                            attempt=4,
                        )
                        rd_result = self._accept_worker_dayplan(
                            await run_day_worker(
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
                                attempt=4,
                                stats=self.stats,
                                trace_recorder=self.trace_recorder,
                                trace_context=self.trace_context,
                                trace_parent_event_id=worker_start_event_ids.get(
                                    (rd_task.day, 4)
                                ),
                            ),
                            candidate_store=candidate_store,
                            run_id=run_id,
                            attempt=4,
                        )
                        await self._emit_worker_result_trace(
                            task=rd_task,
                            result=rd_result,
                            run_id=run_id,
                            attempt=4,
                            parent_event_id=worker_start_event_ids.get(
                                (rd_task.day, 4)
                            ),
                        )
                        if rd_result.success:
                            successes.append(rd_result)
                            redispatch_rollbacks.pop(rd_task.day, None)
                            worker_statuses[idx]["status"] = "done"
                            worker_statuses[idx]["current_tool"] = None
                            if rd_result.dayplan:
                                worker_statuses[idx]["activity_count"] = len(
                                    rd_result.dayplan.get("activities", [])
                                )
                            yield self._build_progress_chunk(
                                worker_statuses,
                                total_days,
                                f"第 {rd_task.day} 天再协商重派完成",
                            )
                        else:
                            worker_statuses[idx]["status"] = "failed"
                            worker_statuses[idx]["current_tool"] = None
                            worker_statuses[idx]["error"] = _format_error(
                                rd_result.error
                            )
                            worker_statuses[idx]["error_code"] = rd_result.error_code
                            yield self._build_progress_chunk(
                                worker_statuses,
                                total_days,
                                f"第 {rd_task.day} 天再协商重派失败",
                            )
                        for ack in self._drain_steering(
                            tasks=tasks,
                            successes=successes,
                            total_days=total_days,
                            active_days={rd_task.day},
                            terminal_message=(
                                "本轮已进入骨架再协商收口，未能应用这条引导，"
                                "请在下一轮重新发送"
                            ),
                        ):
                            yield ack

                    # C1：再协商重派失败（或重建任务缺失未重派）的天回滚
                    for d in sorted(outcome.affected_days):
                        rollback = redispatch_rollbacks.pop(d, None)
                        if self._handle_redispatch_failure(
                            candidate_store=candidate_store,
                            run_id=run_id,
                            rollback=rollback,
                            successes=successes,
                        ):
                            idx = _find_worker_idx(d)
                            worker_statuses[idx]["status"] = "done"
                            worker_statuses[idx]["error"] = None
                            worker_statuses[idx]["error_code"] = None
                            if rollback.result is not None and rollback.result.dayplan:
                                worker_statuses[idx]["activity_count"] = len(
                                    rollback.result.dayplan.get("activities", [])
                                )
                            yield self._build_progress_chunk(
                                worker_statuses,
                                total_days,
                                f"第 {d} 天再协商重派失败，已恢复重派前版本",
                            )

                # 用户可见摘要：自动改动 + 仍不可行的天
                summary_lines: list[str] = []
                if outcome.amendments:
                    summary_lines.append("已自动调整骨架并重派受影响天：")
                    for a in outcome.amendments:
                        summary_lines.append(f"- {a.description}")
                if outcome.unresolved:
                    summary_lines.append(
                        "以下天数无法自动改骨架，已保留其余天部分交付："
                    )
                    for r in outcome.unresolved:
                        summary_lines.append(f"- Day {r.day} [{r.kind}]: {r.reason}")
                    summary_lines.append(
                        "你可以让我针对这些天回退到 Phase 2 微调骨架。"
                    )
                if summary_lines:
                    # 兼容旧测试：INFEASIBLE 路径保留「骨架分配失败」关键字
                    if outcome.unresolved and not outcome.amendments:
                        head = "骨架分配失败，以下天数无法按当前骨架展开:\n"
                    else:
                        head = ""
                    yield LLMChunk(
                        type=ChunkType.TEXT_DELTA,
                        content=f"\n\n⚠️ {head}" + "\n".join(summary_lines) + "\n",
                    )
                # P1-6 + P0-2 + D3：不再整批丢弃；已成功天走部分交付。

            # 8. Sort and validate
            artifact_candidates = candidate_store.load_latest_candidates(
                self.plan.session_id,
                run_id,
                accepted_only=True,
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
                logger.warning(
                    "Global validation [%s]: %s", issue.severity, issue.description
                )
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
                        "fail" if any(i.severity == "error" for i in issues) else "pass"
                    ),
                    "issue_count": len(issues),
                    "issue_counts_by_type": {
                        issue_type: sum(
                            1 for issue in issues if issue.issue_type == issue_type
                        )
                        for issue_type in sorted({issue.issue_type for issue in issues})
                    },
                    "issues": [dict(issue.__dict__) for issue in issues],
                    "re_dispatch_hints": [
                        issue.description
                        for issue in issues
                        if issue.severity == "error"
                    ],
                },
            )

            # 8b. Re-dispatch for error-severity issues (max 1 round)
            error_issues = [i for i in issues if i.severity == "error"]
            if error_issues:
                task_by_day = {t.day: t for t in tasks}
                # C1：8b 修复重派前记录旧 accepted 候选，失败时恢复
                repair_prev_attempts: dict[int, int] = {}

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
                        repair_prev_attempts.pop(rd_day, None)
                        latest_by_day = {
                            c["day"]: c["dayplan"]
                            for c in candidate_store.load_latest_candidates(
                                self.plan.session_id,
                                run_id,
                                accepted_only=True,
                            )
                            if c.get("dayplan")
                        }
                        replacement_dayplan = latest_by_day.get(
                            rd_day, rd_result.dayplan
                        )
                        dayplans[:] = [dp for dp in dayplans if dp.get("day") != rd_day]
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
                        worker_statuses[idx]["error"] = _format_error(rd_result.error)
                        worker_statuses[idx]["error_code"] = rd_result.error_code
                        # C1：修复重派失败——交付仍保留旧 dayplan（in-memory），
                        # 但黑板认领已被 _run_repair_worker 释放，磁盘最新可能是
                        # 失败尝试写入的 rejected 候选。恢复两者，防止后续串行
                        # 修复抢占该天 POI 或磁盘口径缺天。
                        prev_attempt = repair_prev_attempts.pop(rd_day, None)
                        if prev_attempt is not None:
                            candidate_store.restore_candidate_as_latest(
                                self.plan.session_id,
                                run_id,
                                rd_day,
                                prev_attempt,
                                reason="restored after repair redispatch failure",
                            )
                        old_dp = next(
                            (dp for dp in dayplans if dp.get("day") == rd_day), None
                        )
                        if old_dp is not None:
                            ok, reason = self._blackboard.try_accept_dayplan(
                                rd_day, old_dp
                            )
                            if not ok:
                                logger.warning(
                                    "Day %d blackboard re-register after repair "
                                    "failure rejected: %s",
                                    rd_day,
                                    reason,
                                )

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
                    rd_task.repair_hints = _repair_hints_for_day(repair_issues, rd_day)
                    self._apply_steer_hints_to_task(rd_task)
                    self._refresh_task_blackboard_snapshot(rd_task)
                    idx = _find_worker_idx(rd_day)
                    worker_statuses[idx].update(
                        {
                            "status": "redispatch",
                            "iteration": None,
                            "current_tool": None,
                            "error": None,
                            "error_code": None,
                            "activity_count": None,
                        }
                    )
                    try:
                        worker_start_event_ids[
                            (rd_day, 3)
                        ] = await self._emit_worker_start_trace(
                            task=rd_task,
                            run_id=run_id,
                            attempt=3,
                        )
                        prev_payload = candidate_store.get_latest_candidate(
                            self.plan.session_id, run_id, rd_day
                        )
                        if (
                            prev_payload is not None
                            and prev_payload.get("status") == "accepted"
                        ):
                            try:
                                repair_prev_attempts[rd_day] = int(
                                    prev_payload.get("attempt", 0)
                                )
                            except (TypeError, ValueError):
                                pass
                        self._blackboard.release_day(rd_day)
                        rd_result = self._accept_worker_dayplan(
                            await run_day_worker(
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
                            ),
                            candidate_store=candidate_store,
                            run_id=run_id,
                            attempt=3,
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
                        worker_statuses[idx].update(
                            {
                                "status": "redispatch",
                                "iteration": None,
                                "current_tool": None,
                                "error": None,
                                "error_code": None,
                                "activity_count": None,
                            }
                        )
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
                    for ack in self._drain_steering(
                        tasks=tasks,
                        successes=successes,
                        total_days=total_days,
                        active_days=set(local_days),
                        terminal_message=(
                            "本轮已进入最终校验修复，未能应用这条引导，请在下一轮重新发送"
                        ),
                    ):
                        yield ack
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
                    worker_statuses[idx].update(
                        {
                            "status": "redispatch",
                            "iteration": None,
                            "current_tool": None,
                            "error": None,
                            "error_code": None,
                            "activity_count": None,
                        }
                    )
                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        f"校验发现跨天冲突，保守重排第 {rd_day} 天...",
                    )
                    _, rd_result = await _run_repair_worker(
                        rd_day=rd_day,
                        repair_issues=remaining_errors,
                    )
                    _apply_repair_result(rd_day=rd_day, rd_result=rd_result)
                    repaired_days.add(rd_day)
                    for ack in self._drain_steering(
                        tasks=tasks,
                        successes=successes,
                        total_days=total_days,
                        active_days={rd_day},
                        terminal_message=(
                            "本轮已进入最终校验修复，未能应用这条引导，请在下一轮重新发送"
                        ),
                    ):
                        yield ack

                    yield self._build_progress_chunk(
                        worker_statuses,
                        total_days,
                        f"第 {rd_day} 天重新规划{'完成' if rd_result.success else '失败'}",
                    )

                # Re-validate after re-dispatch
                issues = self._global_validate(dayplans)
                unresolved = [i for i in issues if i.severity == "error"]
                if unresolved:
                    for ui in unresolved:
                        logger.warning(
                            "Unresolved after re-dispatch: %s", ui.description
                        )
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
                        "fail" if any(i.severity == "error" for i in issues) else "pass"
                    ),
                    "issue_count": len(issues),
                    "issue_counts_by_type": {
                        issue_type: sum(
                            1 for issue in issues if issue.issue_type == issue_type
                        )
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
