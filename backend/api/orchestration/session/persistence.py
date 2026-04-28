from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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

        restored_messages: list[Message] = []
        for row in await self.message_store.load_all(session_id):
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
                    row["tool_call_id"],
                    row.get("content"),
                )
            provider_state = None
            if row.get("provider_state"):
                provider_state = json.loads(row["provider_state"])

            restored_messages.append(
                Message(
                    role=role,
                    content=row.get("content") if tool_result is None else None,
                    tool_calls=tool_calls,
                    tool_result=tool_result,
                    provider_state=provider_state,
                    history_persisted=True,
                    history_seq=(
                        int(row["history_seq"])
                        if row.get("history_seq") is not None
                        else None
                    ),
                )
            )

        max_history_seq = await self.message_store.max_history_seq(session_id)
        next_history_seq = 0 if max_history_seq is None else max_history_seq + 1
        self.phase_router.sync_phase_state(plan)
        compression_events: list[dict] = []
        session: dict = {
            "plan": plan,
            "messages": restored_messages,
            "agent": None,
            "needs_rebuild": False,
            "user_id": meta["user_id"],
            "compression_events": compression_events,
            "stats": SessionStats(),
            "_pending_system_notes": [],
            "next_history_seq": next_history_seq,
        }
        session["agent"] = self.build_agent(
            plan,
            meta["user_id"],
            session=session,
            compression_events=compression_events,
        )
        return session
