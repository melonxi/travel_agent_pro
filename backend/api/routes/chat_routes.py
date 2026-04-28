from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import FastAPI, HTTPException
from sse_starlette.sse import EventSourceResponse

from agent.types import Message, Role
from api.orchestration.chat.stream import ChatStreamDeps, run_agent_stream
from api.orchestration.memory.turn import build_memory_context_for_turn
from api.schemas import BacktrackRequest, ChatRequest


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
        if req.to_phase == 2:
            req.to_phase = 1
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

            messages.append(Message(role=Role.USER, content=req.message))
            submit_memory_snapshot(
                build_memory_job_snapshot(
                    session_id=plan.session_id,
                    user_id=session["user_id"],
                    messages=messages,
                    plan=plan,
                )
            )

            memory_turn = await build_memory_context_for_turn(
                config=config,
                memory_mgr=memory_mgr,
                session=session,
                plan=plan,
                messages=messages,
                user_id=req.user_id,
                user_message=req.message,
                decide_memory_recall=decide_memory_recall,
                build_recall_retrieval_plan=_build_recall_retrieval_plan,
            )
            for event in memory_turn.events:
                yield event
            memory_context = memory_turn.memory_context

            sys_msg = context_mgr.build_system_message(
                plan,
                phase_prompt,
                memory_context,
                available_tools=available_tools,
            )

            if messages and messages[0].role == Role.SYSTEM:
                messages[0] = sys_msg
            else:
                messages.insert(0, sys_msg)

            from run import RunRecord

            run = RunRecord(
                run_id=str(uuid.uuid4()), session_id=plan.session_id, status="running"
            )
            session["_current_run"] = run
            cancel_event = asyncio.Event()
            session["_cancel_event"] = cancel_event
            agent.cancel_event = cancel_event

            async for event in run_agent_stream(
                chat_stream_deps,
                session,
                plan,
                messages,
                agent,
                run,
                cancel_event,
                phase_before_run,
                user_message=req.message,
            ):
                yield event

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

        if ctx_type == "partial_text":
            messages.append(
                Message(
                    role=Role.SYSTEM,
                    content="你的上一轮回复因网络中断未完成，请从断点继续，不要重复已说的内容。",
                )
            )
        elif ctx_type == "tools_read_only":
            messages.append(
                Message(
                    role=Role.SYSTEM,
                    content="你已经调用了工具并获得结果，但总结被中断了。请根据已有的工具结果继续回复。",
                )
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown continuation type: {ctx_type}",
            )

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

        phase_before_run = plan.phase

        async def event_stream():
            async for event in run_agent_stream(
                chat_stream_deps,
                session,
                plan,
                messages,
                agent,
                run,
                cancel_event,
                phase_before_run,
            ):
                yield event

        return EventSourceResponse(event_stream())
