from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, is_dataclass

from fastapi import FastAPI, HTTPException
from sse_starlette.sse import EventSourceResponse

from agent.message_filters import clean_persisted_session_messages
from agent.types import Message, Role
from api.orchestration.chat.finalization import (
    make_context_rebuild_callback,
    persist_unflushed_messages,
)
from api.orchestration.chat.stream import ChatStreamDeps, run_agent_stream
from api.orchestration.chat.trace_persistence import ensure_trace_run_started
from api.orchestration.memory.turn import build_memory_context_for_turn
from api.schemas import BacktrackRequest, ChatRequest, SteerRequest
from agent.steering import make_steer_envelope
from storage.trace_redaction import stable_content_hash
from telemetry.trace_recorder import TraceContext, TraceRecorder


def _continuation_notice(context_type: str) -> str | None:
    if context_type == "partial_text":
        return "你的上一轮回复因网络中断未完成，请从断点继续，不要重复已说的内容。"
    if context_type == "tools_read_only":
        return "你已经调用了工具并获得结果，但总结被中断了。请根据已有的工具结果继续回复。"
    return None


def _llm_model_config(config) -> dict:
    llm_config = getattr(config, "llm", None)
    if llm_config is None:
        return {}
    if is_dataclass(llm_config):
        return asdict(llm_config)
    return {
        "provider": getattr(llm_config, "provider", None),
        "model": getattr(llm_config, "model", None),
        "temperature": getattr(llm_config, "temperature", None),
        "max_tokens": getattr(llm_config, "max_tokens", None),
    }


async def _install_trace_recorder(
    *,
    chat_stream_deps: ChatStreamDeps,
    session,
    plan,
    agent,
    run,
    phase_prompt: str | None = None,
) -> None:
    if chat_stream_deps.trace_store is None:
        return
    context = TraceContext(
        run_id=run.run_id,
        session_id=plan.session_id,
        trip_id=getattr(plan, "trip_id", None),
        context_epoch=session.get("current_context_epoch"),
        phase=plan.phase,
        phase2_step=getattr(plan, "phase2_step", None),
        metadata={
            "phase_prompt_id": (
                f"phase:{plan.phase}:step:{getattr(plan, 'phase2_step', None) or 'none'}"
            ),
            "phase_prompt_hash": (
                stable_content_hash(phase_prompt) if phase_prompt is not None else None
            ),
        },
    )
    recorder = TraceRecorder(
        trace_store=chat_stream_deps.trace_store,
        artifact_store=chat_stream_deps.trace_artifact_store,
    )
    tool_schemas = agent.tool_engine.get_tools_for_phase(plan.phase, plan)
    await recorder.start_run(
        context,
        payload={
            "phase": plan.phase,
            "phase2_step": getattr(plan, "phase2_step", None),
            "tool_count": len(tool_schemas),
        },
        model_config=_llm_model_config(chat_stream_deps.config),
        tool_schema_hash=stable_content_hash(tool_schemas),
    )
    initial_snapshot = plan.to_dict()
    snapshot_event = await recorder.emit_event(
        context,
        event_type="state_snapshot",
        status="success",
        actor="storage",
        payload={
            "snapshot_scope": "run_start",
            "state_hash": stable_content_hash(initial_snapshot),
            "phase": plan.phase,
            "phase2_step": getattr(plan, "phase2_step", None),
        },
    )
    if snapshot_event is not None:
        await recorder.attach_artifact(
            context,
            event_id=snapshot_event.event_id,
            kind="state_snapshot",
            content=initial_snapshot,
        )
    agent.trace_recorder = recorder
    agent.trace_context = context
    session["_trace_recorder"] = recorder
    session["_trace_context"] = context
    session["_trace_realtime_run_id"] = run.run_id


def register_chat_routes(
    app: FastAPI,
    *,
    sessions: dict[str, dict],
    config,
    memory_mgr,
    context_mgr,
    phase_router,
    ensure_storage_ready,
    restore_session,
    build_agent,
    chat_stream_deps: ChatStreamDeps,
    submit_memory_snapshot,
    build_memory_job_snapshot,
    decide_memory_recall,
    build_recall_retrieval_plan,
    rotate_trip_on_reset_backtrack,
    generate_title,
    state_mgr,
    session_store,
    archive_store,
) -> None:
    _build_recall_retrieval_plan = build_recall_retrieval_plan
    _rotate_trip_on_reset_backtrack = rotate_trip_on_reset_backtrack

    @app.post("/api/backtrack/{session_id}")
    async def backtrack(session_id: str, req: BacktrackRequest):
        await ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await restore_session(session_id)
            if restored is None:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions[session_id] = restored
            session = restored
        plan = session["plan"]
        if req.to_phase >= plan.phase:
            raise HTTPException(status_code=400, detail="只能回退到更早的阶段")
        snapshot_path = await state_mgr.save_snapshot(plan)
        phase_router.prepare_backtrack(
            plan, req.to_phase, req.reason or "用户主动回退", snapshot_path
        )
        await state_mgr.clear_deliverables(session_id)
        await _rotate_trip_on_reset_backtrack(
            user_id=session.get("user_id", "default_user"),
            plan=plan,
            to_phase=req.to_phase,
            reason_text=req.reason,
        )
        await state_mgr.save(plan)
        session["agent"] = build_agent(
            plan,
            session.get("user_id", "default_user"),
            session=session,
            compression_events=session.get("compression_events"),
        )
        session["needs_rebuild"] = False
        await session_store.update(
            session_id,
            phase=plan.phase,
            title=generate_title(plan),
        )
        await archive_store.save_snapshot(
            session_id,
            plan.phase,
            json.dumps(plan.to_dict(), ensure_ascii=False),
        )
        return {"phase": plan.phase, "plan": plan.to_dict()}

    @app.post("/api/chat/{session_id}")
    async def chat(session_id: str, req: ChatRequest):
        await ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await restore_session(session_id)
            if restored is None:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions[session_id] = restored
            session = restored

        plan = session["plan"]
        messages = session["messages"]
        session["user_id"] = req.user_id

        if session.get("needs_rebuild"):
            session["agent"] = build_agent(
                plan,
                session["user_id"],
                session=session,
                compression_events=session.get("compression_events"),
            )
            session["needs_rebuild"] = False

        agent = session["agent"]
        agent.user_id = session["user_id"]

        phase_router.sync_phase_state(plan)
        phase_prompt = phase_router.get_prompt_for_plan(plan)
        available_tools = [
            tool["name"]
            for tool in agent.tool_engine.get_tools_for_phase(plan.phase, plan)
        ]
        phase_before_run = plan.phase

        async def event_stream():
            for task in session.pop("_background_internal_tasks", []):
                if getattr(task, "kind", None) == "memory_extraction":
                    continue
                yield json.dumps(
                    {"type": "internal_task", "task": task.to_dict()},
                    ensure_ascii=False,
                )

            persisted_history = clean_persisted_session_messages(messages)
            current_user = Message(role=Role.USER, content=req.message)
            recall_messages = [*persisted_history, current_user]

            from run import RunRecord

            run = RunRecord(
                run_id=str(uuid.uuid4()), session_id=plan.session_id, status="running"
            )
            await ensure_trace_run_started(
                trace_store=chat_stream_deps.trace_store,
                session=session,
                plan=plan,
                run=run,
            )
            await _install_trace_recorder(
                chat_stream_deps=chat_stream_deps,
                session=session,
                plan=plan,
                agent=agent,
                run=run,
                phase_prompt=phase_prompt,
            )
            session["_current_run"] = run
            submit_memory_snapshot(
                build_memory_job_snapshot(
                    session_id=plan.session_id,
                    user_id=session["user_id"],
                    messages=recall_messages,
                    plan=plan,
                )
            )

            memory_turn = await build_memory_context_for_turn(
                config=config,
                memory_mgr=memory_mgr,
                session=session,
                plan=plan,
                messages=recall_messages,
                user_id=req.user_id,
                user_message=req.message,
                decide_memory_recall=decide_memory_recall,
                build_recall_retrieval_plan=_build_recall_retrieval_plan,
                trace_recorder=getattr(agent, "trace_recorder", None),
                trace_context=getattr(agent, "trace_context", None),
            )
            for event in memory_turn.events:
                yield event
            memory_context = memory_turn.memory_context
            if memory_turn.memory_hit_event_id and getattr(agent, "trace_context", None):
                current_trace_context = agent.trace_context
                agent.trace_context = TraceContext(
                    run_id=current_trace_context.run_id,
                    session_id=current_trace_context.session_id,
                    trip_id=current_trace_context.trip_id,
                    context_epoch=current_trace_context.context_epoch,
                    phase=current_trace_context.phase,
                    phase2_step=current_trace_context.phase2_step,
                    parent_event_id=memory_turn.memory_hit_event_id,
                    root_event_id=memory_turn.memory_hit_event_id,
                    correlation_id=current_trace_context.correlation_id,
                    actor=current_trace_context.actor,
                    metadata={
                        **dict(current_trace_context.metadata),
                        "memory_candidate_ids": list(
                            memory_turn.injected_context_ids or []
                        ),
                        "memory_hit_event_id": memory_turn.memory_hit_event_id,
                        "memory_recall_event_id": memory_turn.memory_recall_event_id,
                        "memory_context_hash": memory_turn.memory_context_hash,
                    },
                )
                session["_trace_context"] = agent.trace_context

            llm_messages = [
                context_mgr.build_static_system_message(plan, phase_prompt),
                *persisted_history,
                current_user,
                context_mgr.build_turn_context_message(
                    plan=plan,
                    available_tools=available_tools,
                    memory_context=memory_context,
                ),
            ]
            session["_active_runtime_messages"] = llm_messages
            cancel_event = asyncio.Event()
            session["_cancel_event"] = cancel_event
            agent.cancel_event = cancel_event

            agent.on_context_rebuild = make_context_rebuild_callback(
                deps=chat_stream_deps,
                session=session,
                plan=plan,
                run=run,
            )

            try:
                async for event in run_agent_stream(
                    chat_stream_deps,
                    session,
                    plan,
                    llm_messages,
                    agent,
                    run,
                    cancel_event,
                    phase_before_run,
                    user_message=req.message,
                ):
                    yield event
            finally:
                session.pop("_active_runtime_messages", None)
                session["messages"] = clean_persisted_session_messages(llm_messages)

        return EventSourceResponse(event_stream())

    @app.post("/api/chat/{session_id}/cancel")
    async def cancel_chat(session_id: str):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        cancel_event = session.get("_cancel_event")
        if cancel_event:
            cancel_event.set()
        return {"status": "cancelled"}

    @app.post("/api/chat/{session_id}/steer")
    async def steer_chat(session_id: str, req: SteerRequest):
        """D4：向进行中的 run 注入运行中引导，不中断主 run。"""
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        text = (req.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        queue = session.get("_steer_queue")
        if queue is None:
            raise HTTPException(status_code=409, detail="No active run to steer")
        try:
            queue.put_nowait(make_steer_envelope(text))
        except asyncio.QueueFull as exc:
            raise HTTPException(
                status_code=429,
                detail="Steering queue is full; retry after the current safe point",
            ) from exc
        return {"status": "queued"}

    @app.post("/api/chat/{session_id}/continue")
    async def continue_chat(session_id: str):
        await ensure_storage_ready()
        session = sessions.get(session_id)
        if not session:
            restored = await restore_session(session_id)
            if restored is None:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions[session_id] = restored
            session = restored

        last_run = session.get("_current_run")
        if not last_run or not last_run.can_continue:
            raise HTTPException(status_code=400, detail="Cannot continue this run")

        plan = session["plan"]
        messages = session["messages"]
        agent = session["agent"]
        ctx = last_run.continuation_context or {}
        ctx_type = ctx.get("type", "")
        notice_text = _continuation_notice(ctx_type)
        if notice_text is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown continuation type: {ctx_type}",
            )

        phase_prompt = phase_router.get_prompt_for_plan(plan)
        available_tools = [
            tool["name"]
            for tool in agent.tool_engine.get_tools_for_phase(plan.phase, plan)
        ]
        persisted_history = clean_persisted_session_messages(messages)
        continuation_messages = [
            context_mgr.build_static_system_message(plan, phase_prompt),
            *persisted_history,
            context_mgr.build_runtime_notice_message(
                kind="continue",
                content=notice_text,
            ),
            context_mgr.build_turn_context_message(
                plan=plan,
                available_tools=available_tools,
                memory_context="暂无相关用户记忆",
            ),
        ]
        session["_active_runtime_messages"] = continuation_messages

        from run import RunRecord

        run = RunRecord(
            run_id=str(uuid.uuid4()),
            session_id=plan.session_id,
            status="running",
        )
        await ensure_trace_run_started(
            trace_store=chat_stream_deps.trace_store,
            session=session,
            plan=plan,
            run=run,
        )
        await _install_trace_recorder(
            chat_stream_deps=chat_stream_deps,
            session=session,
            plan=plan,
            agent=agent,
            run=run,
            phase_prompt=phase_prompt,
        )
        session["_current_run"] = run
        cancel_event = asyncio.Event()
        session["_cancel_event"] = cancel_event
        agent.cancel_event = cancel_event

        agent.on_context_rebuild = make_context_rebuild_callback(
            deps=chat_stream_deps,
            session=session,
            plan=plan,
            run=run,
        )

        phase_before_run = plan.phase

        async def event_stream():
            try:
                async for event in run_agent_stream(
                    chat_stream_deps,
                    session,
                    plan,
                    continuation_messages,
                    agent,
                    run,
                    cancel_event,
                    phase_before_run,
                ):
                    yield event
            finally:
                session.pop("_active_runtime_messages", None)
                session["messages"] = clean_persisted_session_messages(
                    continuation_messages
                )

        return EventSourceResponse(event_stream())
