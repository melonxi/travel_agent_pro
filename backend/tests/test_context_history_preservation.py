import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import patch

from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from main import create_app


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    return create_app()


def _get_route_closure(app, dependency_name: str):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__closure__"):
            continue
        free_vars = getattr(endpoint.__code__, "co_freevars", ())
        for name, cell in zip(free_vars, endpoint.__closure__ or ()):
            if name == dependency_name:
                return cell.cell_contents
    raise RuntimeError(f"Cannot locate {dependency_name}")


@pytest.mark.asyncio
async def test_phase1_to_phase3_to_phase5_preserves_phase1_tool_history_in_db(app):
    async def fake_run(self, messages, phase, tools_override=None):
        original_user = next(message for message in messages if message.role == Role.USER)

        phase1_call = ToolCall(
            id="tc_phase1_search",
            name="quick_travel_search",
            arguments={"query": "东京亲子游"},
        )
        messages.append(
            Message(
                role=Role.ASSISTANT,
                content=None,
                tool_calls=[phase1_call],
            )
        )
        messages.append(
            Message(
                role=Role.TOOL,
                tool_result=ToolResult(
                    tool_call_id="tc_phase1_search",
                    status="success",
                    data={"summary": "东京适合亲子游，有博物馆和公园"},
                ),
            )
        )
        messages.append(Message(role=Role.ASSISTANT, content="东京比较适合"))
        self.plan.phase = 3
        self.plan.phase3_step = "brief"
        await self.on_before_message_rebuild(
            messages=messages,
            from_phase=1,
            from_phase3_step=None,
        )

        messages[:] = [
            Message(role=Role.SYSTEM, content="phase 3 brief system"),
            original_user,
        ]
        messages.append(Message(role=Role.ASSISTANT, content="我来建立画像"))
        self.plan.phase3_step = "candidate"
        await self.on_before_message_rebuild(
            messages=messages,
            from_phase=3,
            from_phase3_step="brief",
        )

        messages[:] = [
            Message(role=Role.SYSTEM, content="phase 3 candidate system"),
            original_user,
        ]
        messages.append(Message(role=Role.ASSISTANT, content="候选池已收敛"))
        self.plan.phase = 5
        await self.on_before_message_rebuild(
            messages=messages,
            from_phase=3,
            from_phase3_step="candidate",
        )
        messages[:] = [
            Message(role=Role.SYSTEM, content="phase 5 system"),
            original_user,
        ]
        messages.append(Message(role=Role.ASSISTANT, content="进入日程组装"))
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="进入日程组装")
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            await client.post(f"/api/chat/{session_id}", json={"message": "亲子游去哪"})
            frontend_resp = await client.get(f"/api/messages/{session_id}")

            rows = await _get_route_closure(app, "message_store").load_all(session_id)

    phase1_rows = [row for row in rows if row["phase"] == 1]
    assert [row["history_seq"] for row in rows] == list(range(len(rows)))
    assert any(row["role"] == "assistant" and row["tool_calls"] for row in phase1_rows)
    assert any(
        row["role"] == "tool" and row["tool_call_id"] == "tc_phase1_search"
        for row in phase1_rows
    )
    assert any("东京比较适合" in (row["content"] or "") for row in phase1_rows)
    assert any(row["phase"] == 3 and row["phase3_step"] == "brief" for row in rows)
    assert any(row["phase"] == 3 and row["phase3_step"] == "candidate" for row in rows)
    assert rows[-1]["phase"] == 5

    frontend_payload = frontend_resp.json()
    assert frontend_resp.status_code == 200
    assert all(message["role"] != "system" for message in frontend_payload)
    assert [message["content"] for message in frontend_payload] == [
        "亲子游去哪",
        "进入日程组装",
    ]


@pytest.mark.asyncio
async def test_phase_history_rows_carry_run_id_and_trip_id(app):
    async def fake_run(self, messages, phase, tools_override=None):
        messages.append(Message(role=Role.ASSISTANT, content="第一轮回复"))
        await self.on_before_message_rebuild(
            messages=messages,
            from_phase=1,
            from_phase3_step=None,
        )
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="第一轮回复")
        yield LLMChunk(type=ChunkType.DONE)

    with patch("agent.loop.AgentLoop.run", fake_run):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            await client.post(f"/api/chat/{session_id}", json={"message": "去东京"})

            state_mgr = _get_route_closure(app, "state_mgr")
            message_store = _get_route_closure(app, "message_store")
            plan = await state_mgr.load(session_id)
            rows = await message_store.load_all(session_id)

    run_ids = {row["run_id"] for row in rows}
    trip_ids = {row["trip_id"] for row in rows}
    assert len(run_ids) == 1
    assert None not in run_ids
    assert trip_ids == {plan.trip_id}
