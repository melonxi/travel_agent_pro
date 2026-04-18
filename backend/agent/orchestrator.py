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


@dataclass
class GlobalValidationIssue:
    issue_type: str  # "poi_duplicate" | "budget_overrun" | "coverage_gap"
    description: str
    affected_days: list[int] = field(default_factory=list)


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
                    )
                )

        return issues

    async def run(self) -> AsyncIterator[LLMChunk]:
        """Execute parallel Phase 5 generation.

        Yields LLMChunk events for frontend progress display.
        """
        tracer = trace.get_tracer("phase5-orchestrator")

        with tracer.start_as_current_span("orchestrator.run") as span:
            # 1. Split tasks
            yield LLMChunk(
                type=ChunkType.AGENT_STATUS,
                agent_status={"stage": "planning", "hint": "正在分解行程任务..."},
            )
            tasks = self._split_tasks()
            span.set_attribute("total_days", len(tasks))

            # 2. Build shared prefix
            shared_prefix = build_shared_prefix(self.plan)

            # 3. Spawn workers with concurrency control
            yield LLMChunk(
                type=ChunkType.AGENT_STATUS,
                agent_status={
                    "stage": "thinking",
                    "hint": f"正在并行规划第 1-{len(tasks)} 天的详细行程...",
                },
            )

            semaphore = asyncio.Semaphore(self.config.max_workers)

            async def _run_with_semaphore(task: DayTask) -> DayWorkerResult:
                async with semaphore:
                    return await run_day_worker(
                        llm=self.llm,
                        tool_engine=self.tool_engine,
                        plan=self.plan,
                        task=task,
                        shared_prefix=shared_prefix,
                        max_iterations=self.config.worker_max_iterations,
                        timeout_seconds=self.config.worker_timeout_seconds,
                    )

            raw_results = await asyncio.gather(
                *[_run_with_semaphore(t) for t in tasks],
                return_exceptions=True,
            )

            # 4. Collect results
            successes: list[DayWorkerResult] = []
            failures: list[tuple[DayTask, str]] = []

            for task, result in zip(tasks, raw_results):
                if isinstance(result, Exception):
                    failures.append((task, f"Exception: {result}"))
                    logger.error("Day %d worker exception: %s", task.day, result)
                elif result.success:
                    successes.append(result)
                    yield LLMChunk(
                        type=ChunkType.AGENT_STATUS,
                        agent_status={
                            "stage": "summarizing",
                            "hint": f"第 {result.day} 天规划完成",
                        },
                    )
                else:
                    failures.append((task, result.error or "Unknown error"))
                    logger.warning("Day %d worker failed: %s", task.day, result.error)

            span.set_attribute("successes", len(successes))
            span.set_attribute("failures", len(failures))

            # 5. Check if we should fall back to serial
            if self.config.fallback_to_serial and len(failures) > len(tasks) / 2:
                logger.warning(
                    "Parallel mode failure rate %.0f%%, falling back to serial",
                    len(failures) / len(tasks) * 100,
                )
                yield LLMChunk(
                    type=ChunkType.AGENT_STATUS,
                    agent_status={
                        "stage": "thinking",
                        "hint": "并行模式失败率过高，切换到串行模式...",
                    },
                )
                return

            # 6. Retry failed days (one at a time)
            for task, error_msg in failures:
                logger.info("Retrying day %d (previous error: %s)", task.day, error_msg)
                retry_result = await run_day_worker(
                    llm=self.llm,
                    tool_engine=self.tool_engine,
                    plan=self.plan,
                    task=task,
                    shared_prefix=shared_prefix,
                    max_iterations=self.config.worker_max_iterations,
                    timeout_seconds=self.config.worker_timeout_seconds,
                )
                if retry_result.success:
                    successes.append(retry_result)
                    yield LLMChunk(
                        type=ChunkType.AGENT_STATUS,
                        agent_status={
                            "stage": "summarizing",
                            "hint": f"第 {retry_result.day} 天（重试）规划完成",
                        },
                    )
                else:
                    logger.error(
                        "Day %d retry also failed: %s",
                        task.day,
                        retry_result.error,
                    )

            # 7. Sort and validate
            dayplans = sorted(
                [r.dayplan for r in successes if r.dayplan],
                key=lambda dp: dp.get("day", 0),
            )

            yield LLMChunk(
                type=ChunkType.AGENT_STATUS,
                agent_status={"stage": "summarizing", "hint": "正在做最终验证..."},
            )
            issues = self._global_validate(dayplans)
            for issue in issues:
                logger.warning("Global validation: %s", issue.description)

            # 8. Write results
            if dayplans:
                replace_all_daily_plans(self.plan, dayplans)
                yield LLMChunk(
                    type=ChunkType.AGENT_STATUS,
                    agent_status={
                        "stage": "summarizing",
                        "hint": f"已写入 {len(dayplans)} 天行程",
                    },
                )

            # 9. Generate summary text
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
