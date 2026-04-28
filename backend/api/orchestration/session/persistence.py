from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from api.orchestration.session.context_segments import (
    ContextSegment,
    derive_context_segments,
)
from api.orchestration.session.runtime_view import (
    HistoryMessage,
    build_runtime_view_for_restore,
)
from agent.types import Message, Role, ToolCall, ToolResult
from state.models import TravelPlanState
from telemetry.stats import SessionStats


def generate_title(plan: TravelPlanState) -> str:
    destination = plan.destination or "未定"
    if plan.dates:
        days = plan.dates.total_days
        nights = max(days - 1, 0)
        return f"{destination} · {days}天{nights}晚"
    return f"{destination} · 新会话"


def serialize_tool_result(result: ToolResult) -> str:
    payload: dict[str, object] = {
        "status": result.status,
        "data": result.data,
    }
    if result.error is not None:
        payload["error"] = result.error
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    if result.suggestion is not None:
        payload["suggestion"] = result.suggestion
    return json.dumps(payload, ensure_ascii=False)


def deserialize_message_content(content: str | None) -> object:
    if content is None:
        return None
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content


def deserialize_tool_result(tool_call_id: str, content: str | None) -> ToolResult:
    payload = deserialize_message_content(content)
    if isinstance(payload, dict) and isinstance(payload.get("status"), str):
        return ToolResult(
            tool_call_id=tool_call_id,
            status=payload["status"],
            data=payload.get("data"),
            error=payload.get("error") if isinstance(payload.get("error"), str) else None,
            error_code=(
                payload.get("error_code")
                if isinstance(payload.get("error_code"), str)
                else None
            ),
            suggestion=(
                payload.get("suggestion")
                if isinstance(payload.get("suggestion"), str)
                else None
            ),
        )
    return ToolResult(
        tool_call_id=tool_call_id,
        status="success",
        data=payload,
    )


def deserialize_history_message(row: dict[str, object]) -> HistoryMessage:
    role = Role(row["role"])
    tool_calls = None
    if row.get("tool_calls"):
        tool_calls = [
            ToolCall(
                id=payload["id"],
                name=payload["name"],
                arguments=payload["arguments"],
                human_label=payload.get("human_label"),
            )
            for payload in json.loads(row["tool_calls"])
        ]

    tool_result = None
    if row.get("tool_call_id"):
        tool_result = deserialize_tool_result(
            str(row["tool_call_id"]),
            row.get("content"),
        )
    provider_state = None
    if row.get("provider_state"):
        provider_state = json.loads(row["provider_state"])

    raw_history_seq = row.get("history_seq")
    history_seq = int(raw_history_seq) if raw_history_seq is not None else None
    raw_phase = row.get("phase")
    phase = int(raw_phase) if raw_phase is not None else None

    return HistoryMessage(
        message=Message(
            role=role,
            content=row.get("content") if tool_result is None else None,
            tool_calls=tool_calls,
            tool_result=tool_result,
            provider_state=provider_state,
            history_persisted=True,
            history_seq=history_seq,
        ),
        phase=phase,
        phase3_step=(
            str(row["phase3_step"])
            if row.get("phase3_step") is not None
            else None
        ),
        history_seq=history_seq,
        run_id=str(row["run_id"]) if row.get("run_id") is not None else None,
        trip_id=str(row["trip_id"]) if row.get("trip_id") is not None else None,
    )


def next_history_seq_from_history(history_view: list[HistoryMessage]) -> int:
    seq_values = [
        item.history_seq
        for item in history_view
        if item.history_seq is not None
    ]
    if seq_values:
        return max(seq_values) + 1
    return len(history_view)


@dataclass
class SessionPersistence:
    ensure_storage_ready: Callable[[], Awaitable[None]]
    db: object
    session_store: object
    message_store: object
    archive_store: object
    state_mgr: object
    phase_router: object
    build_agent: Callable[..., object]
    context_manager: object | None = None
    memory_mgr: object | None = None
    memory_enabled: bool = False

    async def list_context_segments(self, session_id: str) -> list[ContextSegment]:
        await self.ensure_storage_ready()
        rows = await self.message_store.load_all(session_id)
        return derive_context_segments(rows)

    async def load_context_segment_messages(
        self,
        session_id: str,
        context_epoch: int,
    ) -> list[dict]:
        await self.ensure_storage_ready()
        return await self.message_store.load_by_context_epoch(session_id, context_epoch)

    async def persist_messages(
        self,
        session_id: str,
        messages: list[Message],
        *,
        phase: int,
        phase3_step: str | None,
        run_id: str | None,
        trip_id: str | None,
        next_history_seq: int,
    ) -> int:
        await self.ensure_storage_ready()
        rows: list[dict[str, object]] = []
        messages_to_mark: list[tuple[Message, int]] = []
        cursor = next_history_seq
        for message in messages:
            if message.history_persisted:
                continue

            tool_calls_json = None
            if message.tool_calls:
                tool_calls_json = json.dumps(
                    [
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": tool_call.arguments,
                            "human_label": tool_call.human_label,
                        }
                        for tool_call in message.tool_calls
                    ],
                    ensure_ascii=False,
                )

            content = message.content
            tool_call_id = None
            if message.tool_result is not None:
                content = serialize_tool_result(message.tool_result)
                tool_call_id = message.tool_result.tool_call_id
            provider_state_json = None
            if message.provider_state:
                provider_state_json = json.dumps(
                    message.provider_state,
                    ensure_ascii=False,
                )

            assigned_history_seq = cursor
            cursor += 1
            rows.append(
                {
                    "role": message.role.value,
                    "content": content,
                    "tool_calls": tool_calls_json,
                    "tool_call_id": tool_call_id,
                    "provider_state": provider_state_json,
                    "seq": assigned_history_seq,
                    "phase": phase,
                    "phase3_step": phase3_step,
                    "history_seq": assigned_history_seq,
                    "run_id": run_id,
                    "trip_id": trip_id,
                }
            )
            messages_to_mark.append((message, assigned_history_seq))

        await self.message_store.append_batch(session_id, rows)
        for message, assigned_history_seq in messages_to_mark:
            message.history_persisted = True
            message.history_seq = assigned_history_seq
        return cursor

    async def restore_session(self, session_id: str) -> dict | None:
        await self.ensure_storage_ready()
        meta = await self.session_store.load(session_id)
        if meta is None or meta["status"] == "deleted":
            return None

        try:
            plan = await self.state_mgr.load(session_id)
        except FileNotFoundError:
            snapshot = await self.archive_store.load_latest_snapshot(session_id)
            if snapshot is None:
                return None
            plan = TravelPlanState.from_dict(json.loads(snapshot["plan_json"]))

        history_view = [
            deserialize_history_message(row)
            for row in await self.message_store.load_all(session_id)
        ]
        next_history_seq = next_history_seq_from_history(history_view)
        self.phase_router.sync_phase_state(plan)
        compression_events: list[dict] = []
        agent = self.build_agent(
            plan,
            meta["user_id"],
            compression_events=compression_events,
        )
        runtime_view = await build_runtime_view_for_restore(
            history_view=history_view,
            plan=plan,
            user_id=meta["user_id"],
            phase_router=self.phase_router,
            context_manager=self.context_manager,
            memory_mgr=self.memory_mgr,
            memory_enabled=self.memory_enabled,
            tool_engine=agent.tool_engine,
        )
        return {
            "plan": plan,
            "messages": runtime_view,
            "history_messages": history_view,
            "next_history_seq": next_history_seq,
            "agent": agent,
            "needs_rebuild": False,
            "user_id": meta["user_id"],
            "compression_events": compression_events,
            "stats": SessionStats(),
            "_pending_system_notes": [],
        }
