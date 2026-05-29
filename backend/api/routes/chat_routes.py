from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import FastAPI, HTTPException
from sse_starlette.sse import EventSourceResponse

from agent.message_filters import clean_persisted_session_messages
from agent.types import Message, Role
from api.orchestration.chat.finalization import (
    make_context_rebuild_callback,
    persist_unflushed_messages,
)
from api.orchestration.chat.stream import ChatStreamDeps, run_agent_stream
from api.orchestration.memory.turn import build_memory_context_for_turn
from api.schemas import BacktrackRequest, ChatRequest


def _continuation_notice(context_type: str) -> str | None:
    if context_type == "partial_text":
        return "你的上一轮回复因网络中断未完成，请从断点继续，不要重复已说的内容。"
    if context_type == "tools_read_only":
        return "你已经调用了工具并获得结果，但总结被中断了。请根据已有的工具结果继续回复。"
    return None


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
            )
            for event in memory_turn.events:
                yield event
            memory_context = memory_turn.memory_context

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

            from run import RunRecord

            run = RunRecord(
                run_id=str(uuid.uuid4()), session_id=plan.session_id, status="running"
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
