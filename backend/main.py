from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.orchestration.session.message_fallbacks import _apply_message_fallbacks
from api.orchestration.session.pending_notes import flush_pending_system_notes, push_pending_system_note
from api.schemas import BacktrackRequest, ChatRequest
from api.orchestration.common.telemetry_helpers import (
    _days_count_from_dates,
    _record_llm_usage_stats,
    _record_tool_result_stats,
)
from api.orchestration.memory.contracts import (
    MemoryExtractionGateDecision,
    MemoryExtractionOutcome,
    MemoryExtractionProgress,
    MemoryRecallDecision,
    MemoryRouteSaveProgress,
    RecallQueryPlanResult,
)
from api.orchestration.memory.recall_planning import (
    _build_recall_query_tool,
    _collect_forced_tool_call_arguments,
)
from api.orchestration.memory.orchestration import create_memory_orchestration
from api.orchestration.agent.builder import build_agent
from api.orchestration.session.backtrack import detect_backtrack, rotate_trip_on_reset_backtrack
from api.orchestration.chat.stream import ChatStreamDeps
from api.orchestration.session.deliverables import persist_phase7_deliverables
from api.orchestration.common.llm_errors import user_friendly_message
from api.orchestration.session.persistence import SessionPersistence, generate_title
from api.routes.session_routes import register_session_routes
from api.routes.memory_routes import register_memory_routes
from api.routes.internal_task_routes import register_internal_task_routes
from api.routes.chat_routes import register_chat_routes
from api.routes.artifact_routes import register_artifact_routes

from agent.reflection import ReflectionInjector
from config import load_config
from telemetry import setup_telemetry
from context.manager import ContextManager
from llm.factory import create_llm_provider
from memory.manager import MemoryManager
from phase.router import PhaseRouter
from storage.archive_store import ArchiveStore
from storage.database import Database
from storage.message_store import MessageStore
from storage.session_store import SessionStore
from state.manager import StateManager

KEEPALIVE_INTERVAL_S = 8


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_on_phase_rebuild(session_persistence, session, session_id):
    """Build an on_phase_rebuild callback bound to the given persistence/session.

    Extracted as a module-level pure function so it can be unit-tested
    without instantiating the FastAPI app, and to make session_persistence
    an explicit parameter (no closure-over-late-binding).
    """

    async def on_phase_rebuild(*, messages, from_phase, from_step):
        new_count = await session_persistence.persist_messages(
            session_id,
            messages,
            phase=from_phase,
            phase3_step=from_step,
            persisted_count=session.get("persisted_count", 0),
        )
        session["persisted_count"] = new_count

    return on_phase_rebuild


def create_app(config_path: str = "config.yaml") -> FastAPI:
    config = load_config(config_path)
    state_mgr = StateManager(data_dir=config.data_dir)
    memory_mgr = MemoryManager(data_dir=config.data_dir)
    phase_router = PhaseRouter()
    context_mgr = ContextManager()

    resolved_context_window: dict[str, int] = {"value": config.llm.context_window}

    sessions: dict[str, dict] = {}  # session_id → {plan, messages, agent}
    reflection_cache: dict[str, ReflectionInjector] = {}
    quality_gate_retries: dict[tuple[str, int, int], int] = {}
    db = Database(db_path=str(Path(config.data_dir) / "sessions.db"))
    session_store = SessionStore(db)
    message_store = MessageStore(db)
    archive_store = ArchiveStore(db)

    memory_orchestration = create_memory_orchestration(
        config=config,
        memory_mgr=memory_mgr,
        create_llm_provider_func=lambda llm_config: create_llm_provider(llm_config),
        # Late-bind through main so existing tests and compatibility patches that
        # monkeypatch main._collect_forced_tool_call_arguments still affect this flow.
        collect_forced_tool_call_arguments=(
            lambda *args, **kwargs: _collect_forced_tool_call_arguments(*args, **kwargs)
        ),
        keepalive_interval_seconds=lambda: KEEPALIVE_INTERVAL_S,
    )
    memory_scheduler_runtimes = memory_orchestration.scheduler_runtimes
    memory_task_subscribers = memory_orchestration.task_subscribers
    memory_active_tasks = memory_orchestration.active_tasks
    _schedule_memory_event = memory_orchestration.schedule_memory_event
    _build_memory_job_snapshot = memory_orchestration.build_memory_job_snapshot
    _submit_memory_snapshot = memory_orchestration.submit_memory_snapshot
    _decide_memory_recall = memory_orchestration.decide_memory_recall
    _build_recall_retrieval_plan = memory_orchestration.build_recall_retrieval_plan
    _extract_memory_candidates = memory_orchestration.extract_memory_candidates
    _run_memory_job = memory_orchestration.run_memory_job
    _memory_task_stream = memory_orchestration.memory_task_stream
    _append_archived_trip_episode_once = (
        memory_orchestration.append_archived_trip_episode_once
    )

    async def _probe_context_window() -> None:
        """Query model API for actual context window, fallback to config default."""
        llm = create_llm_provider(config.llm)
        try:
            queried = await llm.get_context_window()
            if queried and queried > 0:
                resolved_context_window["value"] = queried
                logging.getLogger("travel-agent-pro").info(
                    f"Context window from model API: {queried}"
                )
        except Exception:
            pass  # keep config default

    async def _ensure_storage_ready() -> None:
        await db.initialize()

    async def _run_v3_memory_cutover_cleanup_once() -> None:
        if getattr(app.state, "_v3_memory_cutover_cleanup_done", False):
            return
        await memory_mgr.v3_store.delete_all_legacy_memory_files()
        app.state._v3_memory_cutover_cleanup_done = True

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.initialize()
        await _probe_context_window()
        await _run_v3_memory_cutover_cleanup_once()
        yield
        for runtime in memory_scheduler_runtimes.values():
            task = runtime.scheduler.running_task
            if task is not None and not task.done():
                task.cancel()
        await db.close()

    app = FastAPI(title="Travel Agent Pro", lifespan=lifespan)
    app.state._run_v3_memory_cutover_cleanup_once = _run_v3_memory_cutover_cleanup_once
    setup_telemetry(app, config.telemetry)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _build_agent(
        plan,
        user_id: str,
        *,
        session: dict | None = None,
        compression_events: list[dict] | None = None,
    ):
        on_phase_rebuild = None
        if session is not None:
            on_phase_rebuild = _make_on_phase_rebuild(
                session_persistence, session, plan.session_id
            )

        return build_agent(
            plan=plan,
            user_id=user_id,
            config=config,
            sessions=sessions,
            resolved_context_window=resolved_context_window,
            context_mgr=context_mgr,
            phase_router=phase_router,
            memory_mgr=memory_mgr,
            reflection_cache=reflection_cache,
            quality_gate_retries=quality_gate_retries,
            create_llm_provider_func=lambda llm_config: create_llm_provider(llm_config),
            collect_forced_tool_call_arguments=(
                lambda *args, **kwargs: _collect_forced_tool_call_arguments(
                    *args, **kwargs
                )
            ),
            compression_events=compression_events,
            on_phase_rebuild=on_phase_rebuild,
        )

    session_persistence = SessionPersistence(
        ensure_storage_ready=_ensure_storage_ready,
        db=db,
        session_store=session_store,
        message_store=message_store,
        archive_store=archive_store,
        state_mgr=state_mgr,
        phase_router=phase_router,
        build_agent=_build_agent,
    )

    register_session_routes(
        app,
        sessions=sessions,
        ensure_storage_ready=_ensure_storage_ready,
        restore_session=session_persistence.restore_session,
        build_agent=_build_agent,
        generate_title=generate_title,
        state_mgr=state_mgr,
        phase_router=phase_router,
        session_store=session_store,
        message_store=message_store,
        archive_store=archive_store,
        reflection_cache=reflection_cache,
        quality_gate_retries=quality_gate_retries,
    )
    register_memory_routes(
        app,
        sessions=sessions,
        ensure_storage_ready=_ensure_storage_ready,
        restore_session=session_persistence.restore_session,
        memory_mgr=memory_mgr,
        now_iso=_now_iso,
    )
    register_internal_task_routes(
        app,
        sessions=sessions,
        ensure_storage_ready=_ensure_storage_ready,
        restore_session=session_persistence.restore_session,
        memory_active_tasks=memory_active_tasks,
        memory_task_subscribers=memory_task_subscribers,
        memory_task_stream=_memory_task_stream,
    )

    chat_stream_deps = ChatStreamDeps(
        config=config,
        state_mgr=state_mgr,
        session_store=session_store,
        archive_store=archive_store,
        phase_router=phase_router,
        keepalive_interval_seconds=lambda: KEEPALIVE_INTERVAL_S,
        detect_backtrack=detect_backtrack,
        rotate_trip_on_reset_backtrack=rotate_trip_on_reset_backtrack,
        apply_message_fallbacks=_apply_message_fallbacks,
        schedule_memory_event=_schedule_memory_event,
        persist_phase7_deliverables=partial(
            persist_phase7_deliverables,
            state_mgr=state_mgr,
            now_iso=_now_iso,
        ),
        persist_messages=session_persistence.persist_messages,
        generate_title=generate_title,
        append_archived_trip_episode_once=_append_archived_trip_episode_once,
        user_friendly_message=user_friendly_message,
    )

    register_chat_routes(
        app,
        sessions=sessions,
        config=config,
        memory_mgr=memory_mgr,
        context_mgr=context_mgr,
        phase_router=phase_router,
        ensure_storage_ready=_ensure_storage_ready,
        restore_session=session_persistence.restore_session,
        build_agent=_build_agent,
        chat_stream_deps=chat_stream_deps,
        submit_memory_snapshot=_submit_memory_snapshot,
        build_memory_job_snapshot=_build_memory_job_snapshot,
        decide_memory_recall=_decide_memory_recall,
        build_recall_retrieval_plan=_build_recall_retrieval_plan,
        rotate_trip_on_reset_backtrack=rotate_trip_on_reset_backtrack,
        generate_title=generate_title,
        state_mgr=state_mgr,
        session_store=session_store,
        archive_store=archive_store,
    )
    register_artifact_routes(
        app,
        sessions=sessions,
        ensure_storage_ready=_ensure_storage_ready,
        session_store=session_store,
        state_mgr=state_mgr,
    )

    app.state.memory_scheduler_runtimes = memory_scheduler_runtimes
    app.state.memory_active_tasks = memory_active_tasks
    app.state.run_memory_job = _run_memory_job
    app.state.extract_memory_candidates = _extract_memory_candidates

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
