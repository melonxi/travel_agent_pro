# backend/tests/test_e2e_golden_path.py
"""
Golden-path end-to-end test: simulates a complete conversation
from "五一去东京" through all phases to summary generation.
"""
from __future__ import annotations

import json

import httpx
import pytest

from agent.types import ToolCall
from llm.types import ChunkType, LLMChunk
from state.models import TravelPlanState


def _text_chunks(*texts: str) -> list[LLMChunk]:
    chunks = [LLMChunk(type=ChunkType.TEXT_DELTA, content=text) for text in texts]
    chunks.append(LLMChunk(type=ChunkType.DONE))
    return chunks


async def _collect_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload:
            events.append(json.loads(payload))
    return events


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def app():
    from main import create_app

    return create_app(config_path="__nonexistent__.yaml")


@pytest.fixture
def sessions(app):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        closure = getattr(endpoint, "__closure__", None)
        if closure is None:
            continue
        for cell in closure:
            try:
                val = cell.cell_contents
            except ValueError:
                continue
            if isinstance(val, dict):
                return val
    pytest.fail("Could not locate 'sessions' dict from app closure")


@pytest.mark.asyncio
async def test_golden_path_tokyo_trip(app, sessions):
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/sessions")
        assert resp.status_code == 200
        session_id = resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        agent = session["agent"]
        assert plan.phase == 1

        class SummaryLLM:
            async def chat(self, messages, tools=None, stream=True):
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="阶段摘要")
                yield LLMChunk(type=ChunkType.DONE)

        agent.llm_factory = lambda: SummaryLLM()

        phase1_call_count = 0

        async def phase1_chat(messages, tools=None, stream=True):
            nonlocal phase1_call_count
            phase1_call_count += 1

            if phase1_call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_destination",
                        name="update_plan_state",
                        arguments={"field": "destination", "value": "东京"},
                    ),
                )
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_budget_skipped",
                        name="update_plan_state",
                        arguments={"field": "budget", "value": "2万元"},
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return

            if phase1_call_count == 2:
                assert plan.phase == 3
                assert plan.dates is None
                assert "- 阶段：3" in messages[0].content
                assert messages[1].content == "[前序阶段摘要]\n阶段摘要"
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_dates",
                        name="update_plan_state",
                        arguments={
                            "field": "dates",
                            "value": {"start": "2026-05-01", "end": "2026-05-06"},
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return

            if phase1_call_count == 3:
                assert plan.phase == 3
                assert plan.budget is not None
                assert plan.budget.total == 20000.0
                assert "- 阶段：3" in messages[0].content
                for chunk in _text_chunks("好的，已记录东京和日期。", "接下来确认住宿偏好。"):
                    yield chunk
                return

            for chunk in _text_chunks("额外兜底文本"):
                yield chunk

        agent.llm.chat = phase1_chat
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "我想五一去东京玩5天，预算2万元，2个大人"},
        )
        assert resp.status_code == 200
        events = await _collect_sse(resp)

        assert phase1_call_count == 3
        assert any(event["type"] == "tool_call" for event in events)
        assert plan.destination == "东京"
        assert plan.dates is not None
        assert plan.dates.start == "2026-05-01"
        assert plan.dates.end == "2026-05-06"
        assert plan.dates.total_days == 5
        assert plan.budget is not None
        assert plan.budget.total == 20000.0
        assert plan.travelers is not None
        assert plan.travelers.adults == 2
        assert plan.phase == 3

        phase3_accom_call_count = 0

        async def phase3_accom_chat(messages, tools=None, stream=True):
            nonlocal phase3_accom_call_count
            phase3_accom_call_count += 1
            if phase3_accom_call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_budget",
                        name="update_plan_state",
                        arguments={"field": "budget", "value": "2万元"},
                    ),
                )
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_travelers",
                        name="update_plan_state",
                        arguments={
                            "field": "travelers",
                            "value": {"adults": 2, "children": 0},
                        },
                    ),
                )
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_accommodation",
                        name="update_plan_state",
                        arguments={
                            "field": "accommodation",
                            "value": {"area": "新宿", "hotel": "新宿华盛顿酒店"},
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return

            assert plan.phase == 5
            assert "- 阶段：5" in messages[0].content
            for chunk in _text_chunks("已锁定新宿住宿。", "接下来开始逐天安排行程。"):
                yield chunk

        agent.llm.chat = phase3_accom_chat
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "住新宿"},
        )
        assert resp.status_code == 200

        assert phase3_accom_call_count == 2
        assert plan.budget is not None
        assert plan.budget.total == 20000.0
        assert plan.travelers is not None
        assert plan.travelers.adults == 2
        assert plan.accommodation is not None
        assert plan.accommodation.area == "新宿"
        assert plan.phase == 5

        sample_activity = {
            "name": "浅草寺",
            "location": {"lat": 35.7148, "lng": 139.7967, "name": "浅草寺"},
            "start_time": "09:00",
            "end_time": "11:00",
            "category": "景点",
            "cost": 0,
        }
        daily_plans_payload = [
            {
                "day": day_num,
                "date": f"2026-05-{day_num:02d}",
                "activities": [sample_activity],
                "notes": f"第{day_num}天行程",
            }
            for day_num in range(1, 6)
        ]

        phase5_call_count = 0

        async def phase5_chat(messages, tools=None, stream=True):
            nonlocal phase5_call_count
            phase5_call_count += 1
            if phase5_call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_daily_plans",
                        name="update_plan_state",
                        arguments={"field": "daily_plans", "value": daily_plans_payload},
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return

            assert plan.phase == 7
            assert "- 阶段：7" in messages[0].content
            for chunk in _text_chunks("5天行程已生成。"):
                yield chunk

        agent.llm.chat = phase5_chat
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "开始安排每天行程"},
        )
        assert resp.status_code == 200

        assert phase5_call_count == 2
        assert plan.phase == 7
        assert len(plan.daily_plans) == 5

        async def phase7_chat(messages, tools=None, stream=True):
            for chunk in _text_chunks(
                "东京五一期间天气温暖，建议带轻便衣物。",
                "\n\n您的5天东京之旅已全部规划完成！祝旅途愉快！",
            ):
                yield chunk

        agent.llm.chat = phase7_chat
        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "帮我生成最终的出行摘要"},
        )
        assert resp.status_code == 200

        assert plan.phase == 7
        assert plan.destination == "东京"
        assert plan.dates is not None
        assert plan.dates.start == "2026-05-01"
        assert plan.dates.end == "2026-05-06"
        assert plan.budget is not None
        assert plan.budget.total == 20000.0
        assert plan.travelers is not None
        assert plan.travelers.adults == 2
        assert plan.accommodation is not None
        assert plan.accommodation.area == "新宿"
        assert len(plan.daily_plans) == 5
        assert plan.daily_plans[0].activities[0].name == "浅草寺"

        resp = await client.get(f"/api/plan/{session_id}")
        assert resp.status_code == 200
        plan_dict = resp.json()
        assert plan_dict["phase"] == 7
        assert plan_dict["destination"] == "东京"
        assert plan_dict["dates"]["start"] == "2026-05-01"
        assert plan_dict["dates"]["end"] == "2026-05-06"
        assert plan_dict["budget"]["total"] == 20000.0
        assert plan_dict["accommodation"]["area"] == "新宿"
        assert len(plan_dict["daily_plans"]) == 5
