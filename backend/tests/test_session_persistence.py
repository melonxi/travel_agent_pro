import json
from types import SimpleNamespace

import pytest

from agent.types import Message, Role, ToolCall, ToolResult
from api.orchestration.session.persistence import (
    SessionPersistence,
    deserialize_tool_result,
    serialize_tool_result,
)


def test_tool_error_result_serialization_roundtrips_repair_fields():
    result = ToolResult(
        tool_call_id="tc_1",
        status="error",
        error="POI '淄博市博物馆' 重复出现在 plans[0].days[1].candidate_pois[0]",
        error_code="INVALID_VALUE",
        suggestion="请把 '淄博市博物馆' 只保留在其中一天",
    )

    serialized = serialize_tool_result(result)
    restored = deserialize_tool_result("tc_1", serialized)

    assert json.loads(serialized) == {
        "status": "error",
        "data": None,
        "error": "POI '淄博市博物馆' 重复出现在 plans[0].days[1].candidate_pois[0]",
        "error_code": "INVALID_VALUE",
        "suggestion": "请把 '淄博市博物馆' 只保留在其中一天",
    }
    assert restored == result


def test_deserialize_tool_result_keeps_legacy_data_rows_as_success():
    restored = deserialize_tool_result("tc_1", '{"results": []}')

    assert restored == ToolResult(
        tool_call_id="tc_1",
        status="success",
        data={"results": []},
    )


@pytest.mark.asyncio
async def test_session_persistence_roundtrips_message_provider_state():
    rows: list[dict[str, object]] = []

    class _MessageStore:
        async def append_batch(self, session_id, payload):
            rows.extend(payload)

        async def load_all(self, session_id):
            return rows

    persistence = SessionPersistence(
        ensure_storage_ready=lambda: _noop(),
        db=SimpleNamespace(execute=_noop),
        session_store=None,
        message_store=_MessageStore(),
        archive_store=None,
        state_mgr=None,
        phase_router=None,
        build_agent=lambda *args, **kwargs: None,
    )
    messages = [
        Message(
            role=Role.ASSISTANT,
            content="先查",
            tool_calls=[ToolCall(id="tc_1", name="web_search", arguments={})],
            provider_state={"reasoning_content": "需要验证。"},
        )
    ]

    await persistence.persist_messages("sess_1", messages)

    assert json.loads(rows[0]["provider_state"]) == {"reasoning_content": "需要验证。"}


async def _noop(*args, **kwargs):
    return None
