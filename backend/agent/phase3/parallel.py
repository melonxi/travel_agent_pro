from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from agent.internal_tasks import InternalTask
from config import Phase3ParallelConfig
from llm.types import ChunkType, LLMChunk


@dataclass(frozen=True)
class Phase3ParallelHandoff:
    dayplans: list[dict[str, Any]]
    issues: list[Any]


def build_phase3_commit_calls(
    *,
    dayplans: list[dict[str, Any]],
    plan: Any | None,
) -> tuple[list[Any], list[int], list[int]]:
    """构造并行 Phase 3 成果的提交工具调用。

    全天覆盖时走 replace_all_day_plans 整体替换；部分覆盖时逐天
    save_day_plan 落盘（避免完整覆盖校验丢弃已成功的天），并返回
    covered_days/missing_days 供调用方向用户明示缺口。
    """
    from agent.types import ToolCall

    handoff_dayplans = list(dayplans)
    dates = getattr(plan, "dates", None) if plan is not None else None
    total_days = (
        dates.total_days if dates is not None else len(handoff_dayplans)
    )
    covered_days = sorted(
        {dp.get("day") for dp in handoff_dayplans if dp.get("day") is not None}
    )
    missing_days = [d for d in range(1, total_days + 1) if d not in covered_days]

    if missing_days:
        commit_calls = [
            ToolCall(
                id=f"internal_phase3_parallel_commit_day_{dp.get('day')}",
                name="save_day_plan",
                arguments={
                    "mode": "create",
                    "day": dp.get("day"),
                    "date": dp.get("date", ""),
                    "activities": dp.get("activities", []),
                    "tips": dp.get("tips", "") or dp.get("notes", ""),
                },
                human_label=f"写入并行第 {dp.get('day')} 天行程",
            )
            for dp in sorted(handoff_dayplans, key=lambda d: d.get("day", 0))
        ]
    else:
        commit_calls = [
            ToolCall(
                id="internal_phase3_parallel_commit",
                name="replace_all_day_plans",
                arguments={"days": handoff_dayplans},
                human_label="写入并行逐日行程",
            )
        ]
    return commit_calls, covered_days, missing_days


def phase3_commit_failure_notice(
    commit_calls: list[Any], messages: list[Any]
) -> str:
    """提交失败时向用户输出的可行动提示。"""
    commit_ids = {call.id for call in commit_calls}
    commit_result = None
    for message in reversed(messages):
        result = getattr(message, "tool_result", None)
        if result and result.tool_call_id in commit_ids:
            commit_result = result
            break
    detail = (
        commit_result.error
        if commit_result is not None and commit_result.error
        else "逐日行程未成功写入状态"
    )
    suggestion = (
        f" {commit_result.suggestion}"
        if commit_result is not None and commit_result.suggestion
        else ""
    )
    return (
        "\n\n⚠️ 并行行程写入失败，当前行程尚未保存到规划状态。"
        f"原因：{detail}{suggestion}"
    )


def phase3_partial_delivery_notice(
    covered_days: list[int], missing_days: list[int]
) -> str:
    return (
        f"\n\n⚠️ 本轮为部分交付：已保存第 "
        f"{'、'.join(str(d) for d in covered_days)} 天，"
        f"第 {'、'.join(str(d) for d in missing_days)} 天规划失败暂缺。"
        "你可以让我单独重排缺失的那几天。"
    )


def should_use_parallel_phase3(
    plan: Any | None,
    config: Phase3ParallelConfig | None,
) -> bool:
    if plan is None or config is None:
        return False
    if not config.enabled:
        return False
    if plan.phase != 3:
        return False
    if plan.daily_plans:
        return False
    if not plan.selected_skeleton_id:
        return False
    if not plan.skeleton_plans:
        return False
    return True


def should_enter_parallel_phase3_now(
    plan: Any | None,
    config: Phase3ParallelConfig | None,
) -> bool:
    """Loop-top Phase 3 daily-planning guard for cold starts and normal phase entry.

    This shares today's eligibility rules with the boundary guard. It exists as
    a separate policy hook so startup routing can diverge later without changing
    the AgentLoop control flow.
    """
    return should_use_parallel_phase3(plan, config)


def should_enter_parallel_phase3_at_iteration_boundary(
    plan: Any | None,
    config: Phase3ParallelConfig | None,
) -> bool:
    """Final safety-boundary Phase 3 daily-planning guard after the last loop iteration.

    This catches a write tool that promotes the plan to Phase 3 on the final
    allowed iteration. It may become more conservative than the loop-top guard
    if boundary-specific telemetry or fallback rules are added.
    """
    return should_use_parallel_phase3(plan, config)


async def run_parallel_phase3_orchestrator(
    *,
    plan: Any,
    llm: Any,
    tool_engine: Any,
    config: Phase3ParallelConfig | None,
    on_handoff: Callable[[Phase3ParallelHandoff], None] | None = None,
    stats: Any | None = None,
    trace_recorder: Any | None = None,
    trace_context: Any | None = None,
) -> AsyncIterator[LLMChunk]:
    from agent.phase3.orchestrator import Phase3Orchestrator

    task_id = f"phase3_orchestration:{plan.session_id if plan else 'unknown'}"
    started_at = time.time()
    yield LLMChunk(
        type=ChunkType.INTERNAL_TASK,
        internal_task=InternalTask(
            id=task_id,
            kind="phase3_orchestration",
            label="Phase 3 并行编排",
            status="pending",
            message="正在拆分每日任务并并行生成行程…",
            blocking=True,
            scope="turn",
            started_at=started_at,
        ),
    )

    orchestrator = Phase3Orchestrator(
        plan=plan,
        llm=llm,
        tool_engine=tool_engine,
        config=config,
        stats=stats,
        trace_recorder=trace_recorder,
        trace_context=trace_context,
    )
    try:
        async for chunk in orchestrator.run():
            yield chunk
    except Exception as exc:
        yield LLMChunk(
            type=ChunkType.INTERNAL_TASK,
            internal_task=InternalTask(
                id=task_id,
                kind="phase3_orchestration",
                label="Phase 3 并行编排",
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
            Phase3ParallelHandoff(
                dayplans=final_dayplans,
                issues=final_issues,
            )
        )

    completed = bool(final_dayplans)
    yield LLMChunk(
        type=ChunkType.INTERNAL_TASK,
        internal_task=InternalTask(
            id=task_id,
            kind="phase3_orchestration",
            label="Phase 3 并行编排",
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
