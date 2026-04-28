from __future__ import annotations

import logging
import time

from agent.compaction import (
    compact_messages_for_prompt,
    compute_prompt_budget,
    estimate_messages_tokens,
)
from agent.hooks import GateResult, HookManager
from agent.internal_tasks import InternalTask
from agent.types import Message, Role
from harness.judge import (
    build_judge_prompt,
    build_judge_tool,
    parse_judge_tool_arguments,
)
from harness.validator import (
    validate_hard_constraints,
    validate_incremental,
    validate_lock_budget,
)
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES

from api.orchestration.session.pending_notes import flush_pending_system_notes, push_pending_system_note
from api.orchestration.session.runtime_view import append_dual_track
from api.orchestration.common.telemetry_helpers import (
    _days_count_from_dates,
    _plan_writer_state_changes,
    _plan_writer_updates,
)

logger = logging.getLogger(__name__)


def build_agent_hooks(
    *,
    plan,
    sessions: dict[str, dict],
    resolved_context_window: dict[str, int],
    config,
    context_mgr,
    compression_events: list[dict] | None,
    create_llm_provider_func,
    collect_forced_tool_call_arguments,
    quality_gate_retries: dict,
):
    hooks = HookManager()
    internal_task_events: list[InternalTask] = []

    async def on_tool_call(**kwargs):
        tool_name = kwargs.get("tool_name")
        if tool_name in PLAN_WRITER_TOOL_NAMES:
            result = kwargs.get("result")
            if (
                result
                and isinstance(result.data, dict)
                and result.data.get("backtracked")
            ):
                session = sessions.get(plan.session_id)
                if session:
                    session["needs_rebuild"] = True
            return

    async def on_validate(**kwargs):
        tool_name = kwargs.get("tool_name")
        if tool_name in PLAN_WRITER_TOOL_NAMES:
            tc = kwargs.get("tool_call")
            result = kwargs.get("result")
            arguments = tc.arguments if tc and tc.arguments else {}
            session = sessions.get(plan.session_id)
            if not (
                result
                and result.status == "success"
                and isinstance(result.data, dict)
                and session
            ):
                return

            updates = _plan_writer_updates(tool_name, arguments, result.data)
            if not updates:
                return

            session["_pending_state_changes"] = _plan_writer_state_changes(
                tool_name,
                arguments,
                result.data,
            )
            errors: list[str] = []
            for update in updates:
                field = update["field"]
                value = update["value"]
                errors.extend(validate_incremental(plan, field, value))
                if field in ("selected_transport", "accommodation"):
                    errors.extend(validate_lock_budget(plan))

            if errors:
                session["_pending_validation_errors"] = errors
                push_pending_system_note(
                    session,
                    "[实时约束检查]\n"
                    + "\n".join(f"- {error}" for error in errors),
                )

    async def on_before_llm(**kwargs):
        msgs = kwargs.get("messages")
        tools = kwargs.get("tools") or []
        phase = kwargs.get("phase", plan.phase)
        if not msgs:
            return
        session = sessions.get(plan.session_id)
        if session:
            flush_pending_system_notes(session, msgs)
        prompt_budget = compute_prompt_budget(
            resolved_context_window["value"],
            config.llm.max_tokens,
        )
        estimated_tokens_before = estimate_messages_tokens(msgs, tools=tools)
        message_count_before = len(msgs)

        tool_compaction = compact_messages_for_prompt(
            msgs,
            prompt_budget=prompt_budget,
            tools=tools,
        )
        if tool_compaction.changed:
            msgs[:] = tool_compaction.messages

        estimated_after_tool_compaction = estimate_messages_tokens(
            msgs, tools=tools
        )
        if (
            tool_compaction.changed
            and estimated_after_tool_compaction <= prompt_budget
        ):
            if compression_events is not None:
                compression_events.append(
                    {
                        "timestamp": time.time(),
                        "message_count_before": message_count_before,
                        "message_count_after": len(msgs),
                        "must_keep_count": 0,
                        "compressed_count": tool_compaction.compacted_tool_messages,
                        "estimated_tokens_before": estimated_tokens_before,
                        "estimated_tokens_after": estimated_after_tool_compaction,
                        "mode": "tool_compaction",
                        "reason": (
                            f"prompt 预算 {prompt_budget} 内进行 {tool_compaction.mode or 'moderate'}"
                            f" TOOL 压缩，usage_ratio={tool_compaction.usage_ratio_before:.2f}"
                        ),
                    }
                )
            return

        if not context_mgr.should_compress(msgs, prompt_budget, tools=tools):
            return

        must_keep, compressible = context_mgr.classify_messages(msgs)
        recent = msgs[-4:]
        recent_ids = {id(m) for m in recent}
        older_compressible = [m for m in compressible if id(m) not in recent_ids]
        summary_source = (
            older_compressible if len(older_compressible) > 2 else compressible
        )
        if len(summary_source) <= 2:
            return

        summary_text = await context_mgr.compress_for_transition(
            messages=summary_source,
            from_phase=phase,
            to_phase=phase,
            llm_factory=None,
        )
        if not summary_text:
            return

        summary_lines = summary_text.splitlines()
        summary = Message(
            role=Role.SYSTEM,
            content="[对话摘要]\n" + "\n".join(summary_lines[-12:]),
        )

        rebuilt: list[Message] = []
        seen_ids: set[int] = set()

        def append_unique(message: Message) -> None:
            ident = id(message)
            if ident in seen_ids:
                return
            rebuilt.append(message)
            seen_ids.add(ident)

        sys_msg = msgs[0] if msgs and msgs[0].role == Role.SYSTEM else None
        if sys_msg:
            append_unique(sys_msg)
        for message in must_keep:
            append_unique(message)
        append_unique(summary)
        for message in recent:
            append_unique(message)

        msgs[:] = rebuilt

        estimated_after_summary = estimate_messages_tokens(msgs, tools=tools)
        if compression_events is not None:
            compression_events.append(
                {
                    "timestamp": time.time(),
                    "message_count_before": message_count_before,
                    "message_count_after": len(msgs),
                    "must_keep_count": len(must_keep),
                    "compressed_count": len(summary_source),
                    "estimated_tokens_before": estimated_tokens_before,
                    "estimated_tokens_after": estimated_after_summary,
                    "mode": "history_summary",
                    "reason": (
                        f"prompt 预算 {prompt_budget} 仍不足，"
                        f"压缩旧消息并保留最近 {len(recent)} 条"
                    ),
                }
            )

    hooks.register("before_llm_call", on_before_llm)

    async def on_soft_judge(**kwargs):
        tool_name = kwargs.get("tool_name")
        if tool_name not in (
            "save_day_plan",
            "replace_all_day_plans",
            "generate_summary",
        ):
            return
        tool_call = kwargs.get("tool_call")
        task_id = f"soft_judge:{getattr(tool_call, 'id', tool_name)}"
        started_at = time.time()
        internal_task_events.append(
            InternalTask(
                id=task_id,
                kind="soft_judge",
                label="行程质量评审",
                status="pending",
                message="正在检查行程节奏、地理顺路性和个性化匹配…",
                related_tool_call_id=getattr(tool_call, "id", None),
                started_at=started_at,
            )
        )
        if not plan.daily_plans:
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="soft_judge",
                    label="行程质量评审",
                    status="skipped",
                    message="暂无每日行程，跳过质量评审。",
                    related_tool_call_id=getattr(tool_call, "id", None),
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return
        session = sessions.get(plan.session_id)
        if not session:
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="soft_judge",
                    label="行程质量评审",
                    status="skipped",
                    message="会话已不可用，跳过质量评审。",
                    related_tool_call_id=getattr(tool_call, "id", None),
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return
        try:
            prefs = {p.key: p.value for p in plan.preferences}
            prompt_text = build_judge_prompt(plan.to_dict(), prefs)
            judge_llm = create_llm_provider_func(config.llm)
            judge_msgs = [
                Message(role=Role.SYSTEM, content="你是旅行行程质量评估专家。"),
                Message(role=Role.USER, content=prompt_text),
            ]
            score_args = await collect_forced_tool_call_arguments(
                judge_llm,
                messages=judge_msgs,
                tool_def=build_judge_tool(),
            )
            score = parse_judge_tool_arguments(score_args)
        except Exception as exc:
            logger.warning("soft judge failed", exc_info=True)
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="soft_judge",
                    label="行程质量评审",
                    status="error",
                    message="质量评审未完成，不影响已保存的行程。",
                    error=str(exc),
                    related_tool_call_id=getattr(tool_call, "id", None),
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return
        # Stage judge scores for the TOOL_RESULT handler to attach to ToolCallRecord
        judge_scores = {
            "overall": score.overall,
            "pace": score.pace,
            "geography": score.geography,
            "coherence": score.coherence,
            "personalization": score.personalization,
            "suggestions_count": len(score.suggestions),
        }
        session["_pending_judge_scores"] = judge_scores
        stats = session.get("stats")
        if stats and stats.tool_calls:
            latest = stats.tool_calls[-1]
            if latest.tool_name == tool_name and latest.judge_scores is None:
                latest.judge_scores = judge_scores
        if score.suggestions:
            suggestion_text = "\n".join(f"- {s}" for s in score.suggestions)
            append_dual_track(
                session,
                plan,
                Message(
                    role=Role.SYSTEM,
                    content=f"💡 行程质量评估（{score.overall:.1f}/5）：\n{suggestion_text}",
                ),
            )
        final_status = "warning" if score.suggestions else "success"
        final_message = (
            f"评分 {score.overall:.1f}/5，发现 {len(score.suggestions)} 条改进建议。"
            if score.suggestions
            else f"评分 {score.overall:.1f}/5，未发现需要立即处理的问题。"
        )
        internal_task_events.append(
            InternalTask(
                id=task_id,
                kind="soft_judge",
                label="行程质量评审",
                status=final_status,
                message=final_message,
                related_tool_call_id=getattr(tool_call, "id", None),
                result=judge_scores,
                started_at=started_at,
                ended_at=time.time(),
            )
        )

    hooks.register("after_tool_call", on_tool_call)
    hooks.register("after_tool_call", on_validate)
    hooks.register("after_tool_result", on_soft_judge)

    async def on_before_phase_transition(**kwargs):
        target_plan = kwargs.get("plan", plan)
        from_phase = int(kwargs.get("from_phase", target_plan.phase))
        to_phase = int(kwargs.get("to_phase", from_phase))
        session = sessions.get(target_plan.session_id)
        task_id = f"quality_gate:{target_plan.session_id}:{from_phase}:{to_phase}"
        started_at = time.time()
        internal_task_events.append(
            InternalTask(
                id=task_id,
                kind="quality_gate",
                label="阶段推进检查",
                status="pending",
                message=f"正在判断 Phase {from_phase} 是否可以进入 Phase {to_phase}…",
                blocking=True,
                scope="turn",
                result={"from_phase": from_phase, "to_phase": to_phase},
                started_at=started_at,
            )
        )

        # Feasibility gate: catch impossible plans early (Phase 1→3)
        if from_phase == 1 and to_phase == 3:
            from harness.feasibility import check_feasibility

            days_count = _days_count_from_dates(target_plan.dates)
            budget_total = None
            if target_plan.budget and target_plan.budget.total:
                budget_total = target_plan.budget.total
            feas = check_feasibility(
                target_plan.destination, budget_total, days_count
            )
            if not feas.feasible:
                feedback = (
                    "[可行性检查]\n当前旅行计划存在以下问题：\n"
                    + "\n".join(f"- {r}" for r in feas.reasons)
                    + "\n请调整后再继续。"
                )
                if session:
                    append_dual_track(
                        session,
                        target_plan,
                        Message(role=Role.SYSTEM, content=feedback),
                    )
                internal_task_events.append(
                    InternalTask(
                        id=task_id,
                        kind="quality_gate",
                        label="阶段推进检查",
                        status="warning",
                        message="可行性检查未通过，暂不推进阶段。",
                        blocking=True,
                        scope="turn",
                        result={"reasons": feas.reasons},
                        started_at=started_at,
                        ended_at=time.time(),
                    )
                )
                return GateResult(allowed=False, feedback=feedback)

        errors = validate_hard_constraints(target_plan)
        if errors:
            feedback = "[质量门控]\n硬约束冲突，必须修正：\n" + "\n".join(
                f"- {error}" for error in errors
            )
            if session:
                append_dual_track(
                    session,
                    target_plan,
                    Message(role=Role.SYSTEM, content=feedback),
                )
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="warning",
                    message="发现硬约束冲突，暂不推进阶段。",
                    blocking=True,
                    scope="turn",
                    result={"errors": errors},
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return GateResult(allowed=False, feedback=feedback)

        if (from_phase, to_phase) not in {(3, 5), (5, 7)}:
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="success",
                    message=f"允许进入 Phase {to_phase}。",
                    blocking=True,
                    scope="turn",
                    result={"from_phase": from_phase, "to_phase": to_phase},
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return GateResult(allowed=True)

        try:
            prefs = {p.key: p.value for p in target_plan.preferences}
            prompt_text = build_judge_prompt(target_plan.to_dict(), prefs)
            judge_llm = create_llm_provider_func(config.llm)
            judge_msgs = [
                Message(role=Role.SYSTEM, content="你是旅行行程质量评估专家。"),
                Message(role=Role.USER, content=prompt_text),
            ]
            score_args = await collect_forced_tool_call_arguments(
                judge_llm,
                messages=judge_msgs,
                tool_def=build_judge_tool(),
            )
            score = parse_judge_tool_arguments(score_args)
        except Exception as exc:
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="skipped",
                    message="阶段推进检查不可用，已跳过并允许主流程继续。",
                    blocking=True,
                    scope="turn",
                    error=str(exc),
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return GateResult(allowed=True)
        if score.overall >= config.quality_gate.threshold:
            quality_gate_retries.pop(
                (target_plan.session_id, from_phase, to_phase),
                None,
            )
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="success",
                    message=f"评分 {score.overall:.1f}/5，可以进入 Phase {to_phase}。",
                    blocking=True,
                    scope="turn",
                    result={"overall": score.overall},
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return GateResult(allowed=True)

        retry_key = (target_plan.session_id, from_phase, to_phase)
        retry_count = quality_gate_retries.get(retry_key, 0)
        if retry_count >= config.quality_gate.max_retries:
            quality_gate_retries.pop(retry_key, None)
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="warning",
                    message="质量门控已达到重试上限，本次允许继续。",
                    blocking=True,
                    scope="turn",
                    result={"overall": score.overall},
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return GateResult(allowed=True)

        quality_gate_retries[retry_key] = retry_count + 1
        suggestions = score.suggestions or [
            "请根据当前旅行画像补强方案质量后再推进阶段。"
        ]
        suggestion_text = "\n".join(f"- {suggestion}" for suggestion in suggestions)
        feedback = (
            f"[质量门控]\n当前方案评分 {score.overall:.1f}/5，"
            f"低于阈值 {config.quality_gate.threshold:.1f}。"
            f"请修正后再进入 Phase {to_phase}：\n{suggestion_text}"
        )
        if session:
            append_dual_track(
                session,
                target_plan,
                Message(role=Role.SYSTEM, content=feedback),
            )
        internal_task_events.append(
            InternalTask(
                id=task_id,
                kind="quality_gate",
                label="阶段推进检查",
                status="warning",
                message=f"评分 {score.overall:.1f}/5，低于阈值 {config.quality_gate.threshold:.1f}。",
                blocking=True,
                scope="turn",
                result={"overall": score.overall, "suggestions": suggestions},
                started_at=started_at,
                ended_at=time.time(),
            )
        )
        return GateResult(allowed=False, feedback=feedback)

    hooks.register_gate("before_phase_transition", on_before_phase_transition)

    return hooks, internal_task_events
