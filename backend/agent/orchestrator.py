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
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from opentelemetry import trace

from agent.day_worker import DayWorkerResult, run_day_worker
from agent.worker_prompt import (
    DayTask,
    build_shared_prefix,
    split_skeleton_to_day_tasks,
)
from config import Phase5ParallelConfig
from llm.base import LLMProvider
from llm.types import ChunkType, LLMChunk
from state.models import TravelPlanState
from state.plan_writers import replace_all_daily_plans
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
                    if prev_end + travel > curr_start:
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
            total_days = len(tasks)
            span.set_attribute("total_days", total_days)

            # 2. Build shared prefix
            shared_prefix = build_shared_prefix(self.plan)

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

            # 8. Sort and validate
            dayplans = sorted(
                [r.dayplan for r in successes if r.dayplan],
                key=lambda dp: dp.get("day", 0),
            )

            yield self._build_progress_chunk(
                worker_statuses, total_days, "正在做最终验证..."
            )
            issues = self._global_validate(dayplans)
            for issue in issues:
                logger.warning("Global validation: %s", issue.description)

            # 9. Write results
            if dayplans:
                replace_all_daily_plans(self.plan, dayplans)
                yield self._build_progress_chunk(
                    worker_statuses,
                    total_days,
                    f"已写入 {len(dayplans)} 天行程",
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
            yield LLMChunk(type=ChunkType.DONE)
