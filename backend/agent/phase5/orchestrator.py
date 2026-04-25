# backend/agent/orchestrator.py
"""Phase 5 Orchestrator: parallel Day Worker dispatch and result collection.

The orchestrator is pure Python (not an LLM agent). It:
1. Splits the selected skeleton into per-day tasks
2. Builds a shared prompt prefix (maximizing KV-Cache hits)
3. Spawns N Day Workers in parallel via asyncio
4. Collects results and performs global validation
5. Writes validated DayPlans to state
6. Retries or falls back to serial on failures
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from math import radians, sin, cos, sqrt, atan2
from typing import Any, AsyncIterator

from opentelemetry import trace

from agent.phase5.candidate_store import Phase5CandidateStore
from agent.phase5.day_worker import DayWorkerResult, run_day_worker
from agent.phase5.worker_prompt import (
    DayTask,
    build_shared_prefix,
    split_skeleton_to_day_tasks,
)
from config import Phase5ParallelConfig
from llm.base import LLMProvider
from llm.types import ChunkType, LLMChunk
from state.models import TravelPlanState
from tools.engine import ToolEngine

logger = logging.getLogger(__name__)


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


def _time_to_minutes(t: str) -> int | None:
    try:
        h, m = map(int, t.split(":"))
        return h * 60 + m
    except (ValueError, AttributeError):
        return None


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
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


class Phase5Orchestrator:
    def __init__(
        self,
        *,
        plan: TravelPlanState,
        llm: LLMProvider | None,
        tool_engine: ToolEngine | None,
        config: Phase5ParallelConfig | None,
    ):
        self.plan = plan
        self.llm = llm
        self.tool_engine = tool_engine
        self.config = config or Phase5ParallelConfig()
        self.final_dayplans: list[dict[str, Any]] = []
        self.final_issues: list[GlobalValidationIssue] = []

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
            "relaxed":   {"max_cross_area_hops": 1, "max_transit_leg_min": 30},
            "balanced":  {"max_cross_area_hops": 2, "max_transit_leg_min": 40},
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
            sorted_tasks = sorted(tasks, key=lambda x: x.day)
            if arrival_min is not None and sorted_tasks[0].date_role == "arrival_day":
                hh, mm = divmod(arrival_min, 60)
                sorted_tasks[0].arrival_time = f"{hh:02d}:{mm:02d}"
            if departure_min is not None and sorted_tasks[-1].date_role == "departure_day":
                hh, mm = divmod(departure_min, 60)
                sorted_tasks[-1].departure_time = f"{hh:02d}:{mm:02d}"
            # Handle arrival_departure_day (single-day trips)
            if len(sorted_tasks) == 1 and sorted_tasks[0].date_role == "arrival_departure_day":
                if arrival_min is not None:
                    hh, mm = divmod(arrival_min, 60)
                    sorted_tasks[0].arrival_time = f"{hh:02d}:{mm:02d}"
                if departure_min is not None:
                    hh, mm = divmod(departure_min, 60)
                    sorted_tasks[0].departure_time = f"{hh:02d}:{mm:02d}"

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
        pace = (self.plan.trip_brief or {}).get("pace", "balanced")
        max_activities = {"relaxed": 3, "balanced": 4, "intensive": 5}.get(pace, 4)

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
                    severity="warning",
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
        """Execute parallel Phase 5 generation.

        Yields LLMChunk events for frontend progress display, including
        real-time per-worker status updates via ``parallel_progress`` events.
        """
        tracer = trace.get_tracer("phase5-orchestrator")

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
            run_id = f"phase5_{uuid.uuid4().hex[:12]}"
            candidate_store = Phase5CandidateStore(self.config.artifact_root)

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
                    )

            pending: dict[asyncio.Task, DayTask] = {}
            for task in tasks:
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
                yield LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content=f"\n\n⚠️ {reason}\n需要回退到 Phase 3 重新调整骨架方案。\n",
                )
                yield LLMChunk(type=ChunkType.DONE)
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

            # 8b. Re-dispatch for error-severity issues (max 1 round)
            error_issues = [i for i in issues if i.severity == "error"]
            if error_issues:
                redispatch_days = set()
                for ei in error_issues:
                    redispatch_days.update(ei.affected_days)

                task_by_day = {t.day: t for t in tasks}
                for rd_day in sorted(redispatch_days):
                    rd_task = task_by_day.get(rd_day)
                    if rd_task is None:
                        continue
                    # Inject repair hints
                    rd_task.repair_hints = [
                        ei.description for ei in error_issues if rd_day in ei.affected_days
                    ]
                    idx = _find_worker_idx(rd_day)
                    worker_statuses[idx].update({
                        "status": "redispatch",
                        "iteration": None,
                        "current_tool": None,
                        "error": None,
                        "error_code": None,
                    })
                    yield self._build_progress_chunk(
                        worker_statuses, total_days,
                        f"校验发现问题，重新规划第 {rd_day} 天...",
                    )
                    # Re-run with updated suffix (includes repair_hints)
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
                    )
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
                        # Replace in dayplans list
                        dayplans = [
                            dp for dp in dayplans if dp.get("day") != rd_day
                        ]
                        dayplans.append(replacement_dayplan)
                        dayplans.sort(key=lambda dp: dp.get("day", 0))
                        worker_statuses[idx]["status"] = "done"
                        worker_statuses[idx]["activity_count"] = len(
                            replacement_dayplan.get("activities", [])
                        )
                    else:
                        worker_statuses[idx]["status"] = "failed"
                        worker_statuses[idx]["error"] = _format_error(rd_result.error)

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

            # 9. Expose results for AgentLoop to commit via the standard write-tool path.
            self.final_dayplans = list(dayplans)
            self.final_issues = list(issues)
            if dayplans:
                yield self._build_progress_chunk(
                    worker_statuses,
                    total_days,
                    f"已生成 {len(dayplans)} 天行程，准备写入规划状态...",
                )

            # 10. Generate summary text
            summary_lines = [f"已完成 {len(dayplans)}/{len(tasks)} 天的行程规划。\n"]
            for dp in dayplans:
                day_num = dp.get("day", "?")
                notes = dp.get("notes", "")
                acts = dp.get("activities", [])
                act_names = [a.get("name", "") for a in acts[:5]]
                summary_lines.append(
                    f"**第 {day_num} 天**：{notes or ''}  \n{'→'.join(act_names)}\n"
                )
            if issues:
                summary_lines.append("\n⚠️ 发现以下问题需要关注：")
                for issue in issues:
                    summary_lines.append(f"- {issue.description}")

            summary_text = "\n".join(summary_lines)
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content=summary_text)
