from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Request

from agent.internal_tasks import InternalTask
from agent.types import Message, Role
from memory.async_jobs import (
    MemoryJobScheduler,
    MemoryJobSnapshot,
    build_extraction_user_window,
)
from state.models import TravelPlanState

from api.orchestration.memory.contracts import MemorySchedulerRuntime

logger = logging.getLogger(__name__)


@dataclass
class MemoryTaskRuntime:
    scheduler_runtimes: dict[str, MemorySchedulerRuntime]
    task_subscribers: dict[str, set[asyncio.Queue[str]]]
    active_tasks: dict[str, dict[str, InternalTask]]
    publish_memory_task: Callable[[str, InternalTask], None]
    get_publish_memory_task: Callable[[], Callable[[str, InternalTask], None]]
    build_memory_job_snapshot: Callable[..., MemoryJobSnapshot]
    submit_memory_snapshot: Callable[[MemoryJobSnapshot], None]
    run_memory_job: Callable[[MemoryJobSnapshot], Any]
    memory_task_stream: Callable[..., Any]


def create_memory_task_runtime(
    *,
    config: Any,
    keepalive_interval_seconds: Callable[[], float],
    decide_memory_extraction: Callable[..., Any],
    extract_memory_candidates: Callable[..., Any],
) -> MemoryTaskRuntime:
    scheduler_runtimes: dict[str, MemorySchedulerRuntime] = {}
    task_subscribers: dict[str, set[asyncio.Queue[str]]] = {}
    active_tasks: dict[str, dict[str, InternalTask]] = {}
    extraction_max_user_messages = 8
    extraction_max_chars = 3000

    def _publish_memory_task(session_id: str, task: InternalTask) -> None:
        active = active_tasks.setdefault(session_id, {})
        active[task.id] = task
        cutoff = time.time() - 300
        for task_id, existing_task in list(active.items()):
            ended_at = getattr(existing_task, "ended_at", None)
            if ended_at is not None and ended_at < cutoff:
                active.pop(task_id, None)
        if len(active) > 20:
            ordered_task_ids = sorted(
                active,
                key=lambda task_id: (
                    active[task_id].ended_at is None,
                    active[task_id].ended_at or active[task_id].started_at or 0,
                ),
            )
            for task_id in ordered_task_ids[: len(active) - 20]:
                active.pop(task_id, None)

        payload = json.dumps(
            {"type": "internal_task", "task": task.to_dict()},
            ensure_ascii=False,
        )
        subscribers = list(task_subscribers.get(session_id, set()))
        delivered_count = 0
        dropped_count = 0
        for queue in list(task_subscribers.get(session_id, set())):
            try:
                queue.put_nowait(payload)
                delivered_count += 1
            except asyncio.QueueFull:
                dropped_count += 1
                continue
        logger.warning(
            "后台记忆任务发布 session=%s task_id=%s kind=%s status=%s scope=%s subscribers=%s delivered=%s dropped=%s active_tasks=%s",
            session_id,
            task.id,
            task.kind,
            task.status,
            task.scope,
            len(subscribers),
            delivered_count,
            dropped_count,
            len(active),
        )

    def _get_publish_memory_task() -> Callable[[str, InternalTask], None]:
        return _publish_memory_task

    def _get_memory_scheduler_runtime(session_id: str) -> MemorySchedulerRuntime:
        runtime = scheduler_runtimes.get(session_id)
        if runtime is not None:
            return runtime

        async def _runner(snapshot: MemoryJobSnapshot) -> None:
            await _run_memory_job(snapshot)

        runtime = MemorySchedulerRuntime(scheduler=MemoryJobScheduler(runner=_runner))
        scheduler_runtimes[session_id] = runtime
        return runtime

    def _build_memory_job_snapshot(
        *,
        session_id: str,
        user_id: str,
        messages: list[Message],
        plan: TravelPlanState,
    ) -> MemoryJobSnapshot:
        user_messages = [
            message.content
            for message in messages
            if message.role == Role.USER and message.content
        ]
        return MemoryJobSnapshot(
            session_id=session_id,
            user_id=user_id,
            turn_id=str(uuid.uuid4()),
            user_messages=list(user_messages),
            submitted_user_count=len(user_messages),
            plan_snapshot=TravelPlanState.from_dict(plan.to_dict()),
        )

    def _submit_memory_snapshot(snapshot: MemoryJobSnapshot) -> None:
        if not config.memory.enabled or not config.memory.extraction.enabled:
            logger.warning(
                "记忆提取快照未提交 session=%s turn=%s reason=disabled memory_enabled=%s extraction_enabled=%s",
                snapshot.session_id,
                snapshot.turn_id,
                config.memory.enabled,
                config.memory.extraction.enabled,
            )
            return
        if config.memory.extraction.trigger != "each_turn":
            logger.warning(
                "记忆提取快照未提交 session=%s turn=%s reason=trigger_not_matched trigger=%s",
                snapshot.session_id,
                snapshot.turn_id,
                config.memory.extraction.trigger,
            )
            return
        runtime = _get_memory_scheduler_runtime(snapshot.session_id)
        logger.warning(
            "记忆提取快照提交 session=%s turn=%s user=%s user_messages=%s submitted_user_count=%s scheduler_running=%s has_pending=%s",
            snapshot.session_id,
            snapshot.turn_id,
            snapshot.user_id,
            len(snapshot.user_messages),
            snapshot.submitted_user_count,
            runtime.scheduler.running_task is not None
            and not runtime.scheduler.running_task.done(),
            runtime.scheduler.pending_snapshot is not None,
        )
        runtime.scheduler.submit(snapshot)

    async def _run_memory_job(snapshot: MemoryJobSnapshot) -> None:
        runtime = _get_memory_scheduler_runtime(snapshot.session_id)
        plan_snapshot = (
            snapshot.plan_snapshot
            if isinstance(snapshot.plan_snapshot, TravelPlanState)
            else TravelPlanState(session_id=snapshot.session_id)
        )
        logger.warning(
            "记忆提取后台任务开始 session=%s turn=%s user=%s user_messages=%s submitted_user_count=%s last_consumed_user_count=%s trip_id=%s",
            snapshot.session_id,
            snapshot.turn_id,
            snapshot.user_id,
            len(snapshot.user_messages),
            snapshot.submitted_user_count,
            runtime.last_consumed_user_count,
            getattr(plan_snapshot, "trip_id", None),
        )
        gate_task_id = f"memory_extraction_gate:{snapshot.session_id}:{snapshot.turn_id}"
        gate_started_at = time.time()
        _publish_memory_task(
            snapshot.session_id,
            InternalTask(
                id=gate_task_id,
                kind="memory_extraction_gate",
                label="记忆提取判定",
                status="pending",
                message="正在判断本轮是否值得提取记忆…",
                blocking=False,
                scope="background",
                started_at=gate_started_at,
            ),
        )
        gate_decision = await decide_memory_extraction(
            session_id=snapshot.session_id,
            user_id=snapshot.user_id,
            user_messages=snapshot.user_messages,
            plan_snapshot=plan_snapshot,
        )
        _publish_memory_task(
            snapshot.session_id,
            InternalTask(
                id=gate_task_id,
                kind="memory_extraction_gate",
                label="记忆提取判定",
                status=gate_decision.status,
                message=gate_decision.message,
                blocking=False,
                scope="background",
                result=gate_decision.to_result(),
                error=gate_decision.error,
                started_at=gate_started_at,
                ended_at=time.time(),
            ),
        )
        if not gate_decision.should_extract:
            if gate_decision.status == "skipped":
                runtime.last_consumed_user_count = max(
                    runtime.last_consumed_user_count,
                    snapshot.submitted_user_count,
                )
            logger.warning(
                "记忆提取后台任务结束 session=%s turn=%s gate_status=%s should_extract=%s reason=%s last_consumed_user_count=%s",
                snapshot.session_id,
                snapshot.turn_id,
                gate_decision.status,
                gate_decision.should_extract,
                gate_decision.reason,
                runtime.last_consumed_user_count,
            )
            return

        extraction_window = build_extraction_user_window(
            user_messages=snapshot.user_messages,
            last_consumed_user_count=runtime.last_consumed_user_count,
            submitted_user_count=snapshot.submitted_user_count,
            max_messages=extraction_max_user_messages,
            max_chars=extraction_max_chars,
        )
        logger.warning(
            "记忆提取窗口构建完成 session=%s turn=%s user_messages=%s extraction_window=%s last_consumed_user_count=%s submitted_user_count=%s",
            snapshot.session_id,
            snapshot.turn_id,
            len(snapshot.user_messages),
            len(extraction_window),
            runtime.last_consumed_user_count,
            snapshot.submitted_user_count,
        )
        task_id = f"memory_extraction:{snapshot.session_id}:{snapshot.turn_id}"
        started_at = time.time()
        _publish_memory_task(
            snapshot.session_id,
            InternalTask(
                id=task_id,
                kind="memory_extraction",
                label="记忆提取",
                status="pending",
                message="正在提取可复用的旅行偏好…",
                blocking=False,
                scope="background",
                started_at=started_at,
            ),
        )
        outcome = await extract_memory_candidates(
            session_id=snapshot.session_id,
            user_id=snapshot.user_id,
            user_messages=extraction_window,
            plan_snapshot=plan_snapshot,
            routes=gate_decision.routes,
            turn_id=snapshot.turn_id,
        )
        _publish_memory_task(
            snapshot.session_id,
            InternalTask(
                id=task_id,
                kind="memory_extraction",
                label="记忆提取",
                status=outcome.status,
                message=outcome.message,
                blocking=False,
                scope="background",
                result=outcome.to_result(),
                error=outcome.error,
                started_at=started_at,
                ended_at=time.time(),
            ),
        )
        if outcome.status in {"success", "skipped"}:
            runtime.last_consumed_user_count = max(
                runtime.last_consumed_user_count,
                snapshot.submitted_user_count,
            )
        logger.warning(
            "记忆提取后台任务结束 session=%s turn=%s extraction_status=%s reason=%s item_ids=%s saved_profile=%s saved_working=%s last_consumed_user_count=%s",
            snapshot.session_id,
            snapshot.turn_id,
            outcome.status,
            outcome.reason,
            len(outcome.item_ids),
            outcome.saved_profile_count,
            outcome.saved_working_count,
            runtime.last_consumed_user_count,
        )

    async def _memory_task_stream(session_id: str, request: Request):
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        subscribers = task_subscribers.setdefault(session_id, set())
        subscribers.add(queue)
        logger.warning(
            "后台记忆任务 SSE 订阅打开 session=%s subscribers=%s active_tasks=%s",
            session_id,
            len(subscribers),
            len(active_tasks.get(session_id, {})),
        )

        try:
            for task in active_tasks.get(session_id, {}).values():
                logger.warning(
                    "后台记忆任务 SSE 重放 session=%s task_id=%s kind=%s status=%s",
                    session_id,
                    task.id,
                    task.kind,
                    task.status,
                )
                yield json.dumps(
                    {"type": "internal_task", "task": task.to_dict()},
                    ensure_ascii=False,
                )

            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=keepalive_interval_seconds(),
                    )
                except asyncio.TimeoutError:
                    yield json.dumps({"type": "keepalive"}, ensure_ascii=False)
                    continue
                yield payload
        finally:
            subscribers.discard(queue)
            if not subscribers:
                task_subscribers.pop(session_id, None)
            logger.warning(
                "后台记忆任务 SSE 订阅关闭 session=%s subscribers=%s",
                session_id,
                len(task_subscribers.get(session_id, set())),
            )

    return MemoryTaskRuntime(
        scheduler_runtimes=scheduler_runtimes,
        task_subscribers=task_subscribers,
        active_tasks=active_tasks,
        publish_memory_task=_publish_memory_task,
        get_publish_memory_task=_get_publish_memory_task,
        build_memory_job_snapshot=_build_memory_job_snapshot,
        submit_memory_snapshot=_submit_memory_snapshot,
        run_memory_job=_run_memory_job,
        memory_task_stream=_memory_task_stream,
    )
