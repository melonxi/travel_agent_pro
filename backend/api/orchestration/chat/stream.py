from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from agent.types import Message, Role
from llm.errors import LLMError
from llm.types import ChunkType
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES

from api.orchestration.chat.events import (
    apply_pending_tool_stats,
    chunk_event_data,
    done_event,
    event_json,
    passthrough_chunk_event,
)
from api.orchestration.chat.finalization import (
    finalize_agent_run,
    persist_run_safely,
    persist_unflushed_messages,
)
from api.orchestration.common.telemetry_helpers import (
    _plan_writer_updated_fields as plan_writer_updated_fields,
    _record_llm_usage_stats as record_llm_usage_stats,
    _record_tool_result_stats as record_tool_result_stats,
)

logger = logging.getLogger(__name__)


@dataclass
class ChatStreamDeps:
    config: object
    state_mgr: object
    session_store: object
    archive_store: object
    phase_router: object
    keepalive_interval_seconds: Callable[[], int]
    detect_backtrack: Callable[..., int | None]
    rotate_trip_on_reset_backtrack: Callable[..., object]
    apply_message_fallbacks: Callable[..., object]
    schedule_memory_event: Callable[..., object]
    persist_phase7_deliverables: Callable[..., object]
    persist_messages: Callable[..., object]
    generate_title: Callable[..., str]
    append_archived_trip_episode_once: Callable[..., object]
    user_friendly_message: Callable[[LLMError], str]


async def run_agent_stream(
    deps: ChatStreamDeps,
    session,
    plan,
    messages,
    agent,
    run,
    cancel_event,
    phase_before_run,
    *,
    user_message: str | None = None,
):
    """Shared agent streaming logic for chat and continue endpoints.

    Parameters
    ----------
    user_message : str | None
        The original user message text. Used for fallback backtrack
        detection and ``apply_message_fallbacks``. Pass ``None`` in the
        continue endpoint where no new user message exists — backtrack
        detection will be skipped.
    """
    from run import IterationProgress

    keepalive_queue: asyncio.Queue[str] = asyncio.Queue()

    async def _keepalive_loop():
        try:
            while True:
                await asyncio.sleep(deps.keepalive_interval_seconds())
                await keepalive_queue.put(event_json({"type": "keepalive"}))
        except asyncio.CancelledError:
            pass

    tool_call_names: dict[str, str] = {}
    tool_call_args: dict[str, dict] = {}

    keepalive_task = asyncio.create_task(_keepalive_loop())
    try:
        accum_text = ""  # 追踪本轮 LLM 输出的文本，供中断恢复使用
        llm_started_at = time.monotonic()
        usage_iteration = 0
        try:
            async for chunk in agent.run(messages, phase=plan.phase):
                if chunk.type.value == "keepalive":
                    passthrough_event = passthrough_chunk_event(chunk)
                    if passthrough_event is not None:
                        yield passthrough_event
                        continue
                if chunk.type == ChunkType.DONE:
                    continue
                if chunk.type == ChunkType.USAGE and chunk.usage_info:
                    record_llm_usage_stats(
                        stats=session.get("stats"),
                        provider=deps.config.llm.provider,
                        model=deps.config.llm.model,
                        usage_info=chunk.usage_info,
                        started_at=llm_started_at,
                        phase=plan.phase,
                        iteration=usage_iteration,
                    )
                    usage_iteration += 1
                    llm_started_at = time.monotonic()
                    continue
                passthrough_event = passthrough_chunk_event(chunk)
                if passthrough_event is not None:
                    yield passthrough_event
                    continue
                event_data, content_delta = chunk_event_data(
                    chunk,
                    tool_call_names,
                    tool_call_args,
                )
                accum_text += content_delta
                if chunk.tool_result:
                    record_tool_result_stats(
                        stats=session.get("stats"),
                        tool_call_names=tool_call_names,
                        tool_call_args=tool_call_args,
                        result=chunk.tool_result,
                        phase=plan.phase,
                    )
                    apply_pending_tool_stats(session)
                while not keepalive_queue.empty():
                    yield keepalive_queue.get_nowait()
                yield event_json(event_data)
                tool_name = (
                    tool_call_names.get(chunk.tool_result.tool_call_id)
                    if chunk.tool_result
                    else None
                )
                if (
                    chunk.tool_result
                    and chunk.tool_result.status == "success"
                    and (
                        tool_name in PLAN_WRITER_TOOL_NAMES
                        or tool_name == "generate_summary"
                    )
                ):
                    result_data = (
                        chunk.tool_result.data
                        if isinstance(chunk.tool_result.data, dict)
                        else {}
                    )
                    updated_fields = plan_writer_updated_fields(result_data)
                    if tool_name == "generate_summary":
                        await deps.persist_phase7_deliverables(plan, result_data)
                    elif result_data.get("backtracked"):
                        await deps.state_mgr.clear_deliverables(plan.session_id)
                        await deps.rotate_trip_on_reset_backtrack(
                            user_id=session["user_id"],
                            plan=plan,
                            to_phase=int(result_data.get("to_phase", plan.phase)),
                            reason_text=str(result_data.get("reason", "")),
                        )
                    elif "selected_skeleton_id" in updated_fields:
                        deps.schedule_memory_event(
                            user_id=session["user_id"],
                            session_id=plan.session_id,
                            event_type="accept",
                            object_type="skeleton",
                            object_payload=chunk.tool_result.data or {},
                        )
                    elif "selected_transport" in updated_fields:
                        deps.schedule_memory_event(
                            user_id=session["user_id"],
                            session_id=plan.session_id,
                            event_type="accept",
                            object_type="transport",
                            object_payload=chunk.tool_result.data or {},
                        )
                    elif "accommodation" in updated_fields:
                        deps.schedule_memory_event(
                            user_id=session["user_id"],
                            session_id=plan.session_id,
                            event_type="accept",
                            object_type="hotel",
                            object_payload=chunk.tool_result.data or {},
                        )
                    # 增量持久化：工具写入成功后立即保存，防止 SSE 中断丢失状态
                    await deps.state_mgr.save(plan)
                    # 同步更新 session meta，确保 plan 文件与数据库一致
                    try:
                        await deps.session_store.update(
                            plan.session_id,
                            phase=plan.phase,
                            title=deps.generate_title(plan),
                        )
                    except Exception:
                        logger.warning(
                            "增量 session meta 更新失败 session=%s",
                            plan.session_id,
                            exc_info=True,
                        )
                    yield event_json({"type": "state_update", "plan": plan.to_dict()})
                    _pending_step = session.pop(
                        "_pending_phase_step_transition", None
                    )
                    if _pending_step is not None:
                        yield event_json({"type": "phase_transition", **_pending_step})
        except LLMError as exc:
            if exc.failure_phase == "cancelled":
                run.status = "cancelled"
                run.finished_at = time.time()
                yield done_event(run)
            else:
                run.status = "failed"
                run.error_code = exc.code.value
                run.finished_at = time.time()
                logger.exception(
                    "LLM error for session %s: %s",
                    plan.session_id,
                    exc.code.value,
                )

                progress = agent.progress
                can_continue = progress in (
                    IterationProgress.PARTIAL_TEXT,
                    IterationProgress.TOOLS_READ_ONLY,
                )

                if can_continue and accum_text.strip():
                    # 把不完整的 assistant 消息追加到历史
                    messages.append(
                        Message(
                            role=Role.ASSISTANT,
                            content=accum_text,
                            incomplete=True,
                        )
                    )
                    run.continuation_context = {
                        "type": progress.value,
                        "partial_assistant_text": accum_text,
                    }
                    if progress == IterationProgress.TOOLS_READ_ONLY:
                        run.continuation_context["completed_tool_count"] = sum(
                            1 for m in messages if m.role == Role.TOOL
                        )

                run.can_continue = can_continue

                yield event_json(
                    {
                        "type": "error",
                        "error_code": exc.code.value,
                        "retryable": exc.retryable,
                        "can_continue": can_continue,
                        "provider": exc.provider,
                        "model": exc.model,
                        "failure_phase": exc.failure_phase,
                        "message": deps.user_friendly_message(exc),
                        "error": exc.raw_error,
                    }
                )
        except Exception as exc:
            run.status = "failed"
            run.error_code = "AGENT_STREAM_ERROR"
            run.finished_at = time.time()
            logger.exception("Agent stream failed for session %s", plan.session_id)
            yield event_json(
                {
                    "type": "error",
                    "error_code": "AGENT_STREAM_ERROR",
                    "retryable": False,
                    "can_continue": False,
                    "message": "系统内部错误，请稍后重试。",
                    "error": str(exc),
                }
            )

        # Fallback：如果本轮 agent 没触发 backtrack，检查关键词 fallback
        # 仅在有用户消息时进行（continue 场景无新用户消息，跳过）
        if user_message is not None and plan.phase == phase_before_run:
            backtrack_target = deps.detect_backtrack(user_message, plan)
            if backtrack_target is not None:
                reason = f"fallback回退：{user_message[:50]}"
                tool_call_id = f"fallback.request_backtrack:{plan.version}"
                yield event_json(
                    {
                        "type": "tool_call",
                        "tool_call": {
                            "id": tool_call_id,
                            "name": "request_backtrack",
                            "arguments": {
                                "to_phase": backtrack_target,
                                "reason": reason,
                            },
                            "human_label": "回退到之前阶段",
                        },
                    }
                )
                snapshot_path = await deps.state_mgr.save_snapshot(plan)
                from_phase = plan.phase
                await persist_unflushed_messages(
                    deps=deps,
                    session=session,
                    plan=plan,
                    messages=messages,
                    phase=from_phase,
                    phase3_step=getattr(plan, "phase3_step", None),
                    run_id=run.run_id,
                    trip_id=getattr(plan, "trip_id", None),
                )
                deps.phase_router.prepare_backtrack(
                    plan,
                    backtrack_target,
                    reason,
                    snapshot_path,
                )
                await deps.state_mgr.clear_deliverables(plan.session_id)
                await deps.rotate_trip_on_reset_backtrack(
                    user_id=session["user_id"],
                    plan=plan,
                    to_phase=backtrack_target,
                    reason_text=user_message,
                )
                session["needs_rebuild"] = True
                yield event_json(
                    {
                        "type": "tool_result",
                        "tool_result": {
                            "tool_call_id": tool_call_id,
                            "status": "success",
                            "data": {
                                "backtracked": True,
                                "from_phase": from_phase,
                                "to_phase": backtrack_target,
                                "reason": reason,
                                "next_action": "请向用户确认回退结果，不要继续调用其他工具",
                            },
                            "error": None,
                            "error_code": None,
                            "suggestion": None,
                        },
                    }
                )
                deps.schedule_memory_event(
                    user_id=session["user_id"],
                    session_id=plan.session_id,
                    event_type="reject",
                    object_type="phase_output",
                    object_payload={
                        "from_phase": from_phase,
                        "to_phase": backtrack_target,
                        "reason": reason,
                    },
                    reason_text=reason,
                )

        if user_message is not None and plan.phase < phase_before_run:
            await deps.apply_message_fallbacks(plan, user_message, deps.phase_router)

        async for event in finalize_agent_run(
            deps=deps,
            session=session,
            plan=plan,
            messages=messages,
            run=run,
            phase_before_run=phase_before_run,
        ):
            yield event

    finally:
        await persist_run_safely(
            deps=deps,
            session=session,
            plan=plan,
            messages=messages,
            run=run,
        )
        keepalive_task.cancel()
        session.pop("_cancel_event", None)
        # 当 run 可以继续时，保留 _current_run 以供 continue endpoint 使用
        if not run.can_continue:
            session.pop("_current_run", None)
