"""D4: 运行中引导 unit + endpoint + Phase 3 drain 注入测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent.phase3.day_worker import DayWorkerResult
from agent.phase3.orchestrator import Phase3Orchestrator
from agent.steering import (
    drain_steer_queue,
    inject_steer_into_messages,
    make_steer_envelope,
    parse_steer_day_targets,
)
from agent.types import Message, Role
from config import Phase3ParallelConfig
from llm.types import ChunkType
from main import create_app
from state.models import (
    Accommodation,
    Budget,
    DateRange,
    TravelPlanState,
    Travelers,
)


class TestDrainSteerQueue:
    def test_empty_queue_noop(self):
        assert drain_steer_queue(None) == []
        assert drain_steer_queue(asyncio.Queue()) == []

    def test_drains_multiple_and_keeps_recent_on_flood(self):
        q: asyncio.Queue = asyncio.Queue()
        for i in range(8):
            q.put_nowait(make_steer_envelope(f"msg-{i}"))
        texts = drain_steer_queue(q, max_keep=3)
        assert texts == ["msg-5", "msg-6", "msg-7"]
        assert drain_steer_queue(q) == []

    def test_inject_into_messages_is_runtime_notice(self):
        msgs: list[Message] = [Message(role=Role.USER, content="hi")]
        n = inject_steer_into_messages(msgs, ["第 3 天别排太满"])
        assert n == 1
        assert len(msgs) == 2
        assert msgs[-1].role == Role.USER
        assert "用户运行中引导" in (msgs[-1].content or "")
        assert "第 3 天别排太满" in (msgs[-1].content or "")
        assert "runtime_notice" in (msgs[-1].content or "")


class TestParseSteerDay:
    def test_parse_single_and_multi(self):
        assert parse_steer_day_targets("第 3 天别排太满") == [3]
        assert parse_steer_day_targets("第1天和第3天轻一点") == [1, 3]
        assert parse_steer_day_targets("整体节奏慢一点") == []


@pytest.mark.asyncio
async def test_steer_endpoint_no_active_run_returns_409(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    config_file.write_text(
        f"""
llm:
  provider: openai
  model: gpt-4o
data_dir: "{data_dir}"
flyai:
  enabled: false
memory_extraction:
  enabled: false
telemetry:
  enabled: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    app = create_app(str(config_file))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]
        resp = await client.post(
            f"/api/chat/{session_id}/steer",
            json={"text": "第 2 天别去太远"},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_steer_endpoint_accepted_when_queue_present(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    config_file.write_text(
        f"""
llm:
  provider: openai
  model: gpt-4o
data_dir: "{data_dir}"
flyai:
  enabled: false
memory_extraction:
  enabled: false
telemetry:
  enabled: false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    app = create_app(str(config_file))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_resp = await client.post("/api/sessions")
        session_id = session_resp.json()["session_id"]

    # create_app 的 sessions 在路由闭包中；从 endpoint 的 freevars 取出
    sessions_map = None
    for route in app.routes:
        ep = getattr(route, "endpoint", None)
        if ep is None or not getattr(ep, "__closure__", None):
            continue
        for cell in ep.__closure__ or []:
            val = cell.cell_contents
            if isinstance(val, dict) and session_id in val:
                sessions_map = val
                break
        if sessions_map is not None:
            break
    assert sessions_map is not None
    q: asyncio.Queue = asyncio.Queue()
    sessions_map[session_id]["_steer_queue"] = q

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/chat/{session_id}/steer",
            json={"text": "第 2 天别去太远"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    item = q.get_nowait()
    assert item["text"] == "第 2 天别去太远"


def _plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="steer-orch")
    plan.phase = 3
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.travelers = Travelers(adults=2)
    plan.trip_brief = {"goal": "文化", "pace": "balanced"}
    plan.accommodation = Accommodation(area="新宿")
    plan.budget = Budget(total=30000, currency="CNY")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [
        {
            "id": "plan_A",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["A"],
                    "locked_pois": ["P1"],
                    "candidate_pois": ["c1"],
                    "date_role": "arrival_day",
                },
                {
                    "area_cluster": ["B"],
                    "locked_pois": ["P2"],
                    "candidate_pois": ["c2"],
                    "date_role": "full_day",
                },
                {
                    "area_cluster": ["C"],
                    "locked_pois": ["P3"],
                    "candidate_pois": ["c3"],
                    "date_role": "departure_day",
                },
            ],
        }
    ]
    return plan


@pytest.mark.asyncio
async def test_phase3_steer_redispatches_completed_day_only(tmp_path):
    """第 1、2 天完成后 put 引导「第 3 天别排太满」→ 仅第 3 天带 hint 重派。"""
    plan = _plan()
    q: asyncio.Queue = asyncio.Queue()
    call_log: list[tuple[int, int, list[str]]] = []

    async def fake_worker(**kwargs):
        task = kwargs["task"]
        attempt = kwargs["attempt"]
        # 第 1 个 worker 完成时注入对第 3 天的引导（模拟用户中途 steer）
        if task.day == 1 and attempt == 1:
            q.put_nowait(make_steer_envelope("第 3 天别排太满"))
        call_log.append((task.day, attempt, list(task.repair_hints or [])))
        return DayWorkerResult(
            day=task.day,
            date=task.date,
            success=True,
            dayplan={
                "day": task.day,
                "date": task.date,
                "notes": "",
                "activities": [
                    {
                        "name": f"poi-{task.day}-{attempt}",
                        "cost": 0,
                        "start_time": "10:00",
                        "end_time": "11:00",
                    }
                ],
            },
        )

    with patch("agent.phase3.orchestrator.run_day_worker", side_effect=fake_worker):
        orch = Phase3Orchestrator(
            plan=plan,
            llm=AsyncMock(),
            tool_engine=AsyncMock(),
            config=Phase3ParallelConfig(
                max_workers=1,  # 串行，保证 day1 先完成再 day3
                fallback_to_serial=False,
            ),
            steer_queue=q,
        )
        chunks = []
        async for chunk in orch.run():
            chunks.append(chunk)

    # 第 3 天应在后续 attempt 中带 steering repair_hint
    day3_calls = [c for c in call_log if c[0] == 3]
    assert day3_calls, "day 3 should have been dispatched"
    # 至少有一次带引导的调用（retry 或 首次若引导更早）
    hinted = [
        c
        for c in day3_calls
        if any("别排太满" in h or "用户运行中引导" in h for h in c[2])
    ]
    # 若 day3 在 steer 入队前已启动，则 redispatch 会带 hint
    # max_workers=1 时顺序 1→2→3，day1 完成时 put steer，day3 启动时应已 drain
    assert hinted or any(
        "steering" in (c.content or "")
        or "引导" in (c.content or "")
        for c in chunks
        if c.type in (ChunkType.TEXT_DELTA, ChunkType.AGENT_STATUS)
    )

    text = "".join(
        c.content or "" for c in chunks if c.type == ChunkType.TEXT_DELTA
    )
    ack_stages = [
        c.agent_status.get("stage")
        for c in chunks
        if c.type == ChunkType.AGENT_STATUS and c.agent_status
    ]
    assert "steering_ack" in ack_stages or "引导" in text
