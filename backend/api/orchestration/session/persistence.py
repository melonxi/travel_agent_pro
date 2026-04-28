from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agent.types import Message, Role, ToolCall, ToolResult
from api.orchestration.session.runtime_view import derive_runtime_view
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
        persisted_count: int,
    ) -> int:
        """增量追加未落盘的 tail messages，返回新的 persisted_count。

        - 不再 DELETE 旧记录；messages 表是会话完整历史的事实源。
        - persisted_count 表示已落盘条数（即下一条新消息在 messages 列表中的索引）。
        - 当 persisted_count >= len(messages) 时直接返回 persisted_count，不写盘
          （场景：phase rebuild 后 runtime view 比 history 短）。
        - phase / phase3_step 由调用方按当下语境决定（phase rebuild 之前用切换前的值；
          普通 finalize 用当下 plan 的值）。
        """
        if persisted_count < 0:
            raise ValueError(
                f"persisted_count must be >= 0, got {persisted_count}"
            )
        if persisted_count >= len(messages):
            return persisted_count
        await self.ensure_storage_ready()

        rows: list[dict[str, object]] = []
        for offset, message in enumerate(messages[persisted_count:]):
            rows.append(
                self._serialize_message(
                    message,
                    seq=persisted_count + offset,
                    phase=phase,
                    phase3_step=phase3_step,
                )
            )
        await self.message_store.append_batch(session_id, rows)
        return len(messages)

    def _serialize_message(
        self,
        message: Message,
        *,
        seq: int,
        phase: int,
        phase3_step: str | None,
    ) -> dict[str, object]:
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
            provider_state_json = json.dumps(message.provider_state, ensure_ascii=False)

        return {
            "role": message.role.value,
            "content": content,
            "tool_calls": tool_calls_json,
            "tool_call_id": tool_call_id,
            "provider_state": provider_state_json,
            "phase": phase,
            "phase3_step": phase3_step,
            "seq": seq,
        }

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
                )
            )
            # 把 phase 标签作为 sidecar 字段挂到对象上（不改 dataclass）
            restored_messages[-1].__dict__["_phase_tag"] = row.get("phase")
            restored_messages[-1].__dict__["_phase3_step_tag"] = row.get("phase3_step")

        self.phase_router.sync_phase_state(plan)
        compression_events: list[dict] = []
        history_view = restored_messages
        runtime_view = derive_runtime_view(history_view, plan)
        session: dict = {
            "plan": plan,
            "messages": runtime_view,
            "history_messages": history_view,
            "agent": None,
            "needs_rebuild": False,
            "user_id": meta["user_id"],
            "compression_events": compression_events,
            "stats": SessionStats(),
            "_pending_system_notes": [],
            "persisted_count": len(history_view),
        }
        session["agent"] = self.build_agent(
            plan,
            meta["user_id"],
            session=session,
            compression_events=compression_events,
        )
        return session
