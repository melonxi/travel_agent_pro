from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from agent.internal_tasks import InternalTask
from config import Phase5ParallelConfig
from llm.types import ChunkType, LLMChunk


@dataclass(frozen=True)
class Phase5ParallelHandoff:
    dayplans: list[dict[str, Any]]
    issues: list[Any]


def should_use_parallel_phase5(
    plan: Any | None,
    config: Phase5ParallelConfig | None,
) -> bool:
    if plan is None or config is None:
        return False
    if not config.enabled:
        return False
    if plan.phase != 5:
        return False
    if plan.daily_plans:
        return False
    if not plan.selected_skeleton_id:
        return False
    if not plan.skeleton_plans:
        return False
    return True


def should_enter_parallel_phase5_now(
    plan: Any | None,
    config: Phase5ParallelConfig | None,
) -> bool:
    """Loop-top Phase 5 guard for cold starts and normal phase entry.

    This shares today's eligibility rules with the boundary guard. It exists as
    a separate policy hook so startup routing can diverge later without changing
    the AgentLoop control flow.
    """
    return should_use_parallel_phase5(plan, config)


def should_enter_parallel_phase5_at_iteration_boundary(
    plan: Any | None,
    config: Phase5ParallelConfig | None,
) -> bool:
    """Final safety-boundary Phase 5 guard after the last loop iteration.

    This catches a write tool that promotes the plan to Phase 5 on the final
    allowed iteration. It may become more conservative than the loop-top guard
    if boundary-specific telemetry or fallback rules are added.
    """
    return should_use_parallel_phase5(plan, config)


async def run_parallel_phase5_orchestrator(
    *,
    plan: Any,
    llm: Any,
    tool_engine: Any,
    config: Phase5ParallelConfig | None,
    on_handoff: Callable[[Phase5ParallelHandoff], None] | None = None,
) -> AsyncIterator[LLMChunk]:
    from agent.phase5.orchestrator import Phase5Orchestrator

    task_id = f"phase5_orchestration:{plan.session_id if plan else 'unknown'}"
    started_at = time.time()
    yield LLMChunk(
        type=ChunkType.INTERNAL_TASK,
        internal_task=InternalTask(
            id=task_id,
            kind="phase5_orchestration",
            label="Phase 5 并行编排",
            status="pending",
            message="正在拆分每日任务并并行生成行程…",
            blocking=True,
            scope="turn",
            started_at=started_at,
        ),
    )

    orchestrator = Phase5Orchestrator(
        plan=plan,
        llm=llm,
        tool_engine=tool_engine,
        config=config,
    )
    try:
        async for chunk in orchestrator.run():
            yield chunk
    except Exception as exc:
        yield LLMChunk(
            type=ChunkType.INTERNAL_TASK,
            internal_task=InternalTask(
                id=task_id,
                kind="phase5_orchestration",
                label="Phase 5 并行编排",
                status="error",
                message="并行逐日行程生成失败。",
                blocking=True,
                scope="turn",
                error=str(exc),
                started_at=started_at,
                ended_at=time.time(),
            ),
        )
        raise

    final_dayplans = list(getattr(orchestrator, "final_dayplans", []) or [])
    final_issues = list(getattr(orchestrator, "final_issues", []) or [])
    if final_dayplans and on_handoff is not None:
        on_handoff(
            Phase5ParallelHandoff(
                dayplans=final_dayplans,
                issues=final_issues,
            )
        )

    completed = bool(final_dayplans)
    yield LLMChunk(
        type=ChunkType.INTERNAL_TASK,
        internal_task=InternalTask(
            id=task_id,
            kind="phase5_orchestration",
            label="Phase 5 并行编排",
            status="success" if completed else "warning",
            message=(
                "并行逐日行程生成完成"
                if completed
                else "并行生成未完全成功，已降级或等待后续串行处理。"
            ),
            blocking=True,
            scope="turn",
            result={"fallback": not completed},
            started_at=started_at,
            ended_at=time.time(),
        ),
    )
