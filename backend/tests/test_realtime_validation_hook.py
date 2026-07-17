from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from state.models import Accommodation, Budget, DateRange, DayPlan, TravelPlanState


def _future_date(days_ahead: int = 30) -> str:
    """行程日期必须在未来，否则会被 past_date guardrail 拦截；禁止写死日期。"""
    return (date.today() + timedelta(days=days_ahead)).isoformat()


def test_build_deliverable_consistency_errors_detects_plan_fact_conflicts():
    from api.orchestration.agent.hooks import build_deliverable_consistency_errors

    plan = TravelPlanState(session_id="s-consistency", phase=4)
    plan.dates = DateRange(start="2026-07-10", end="2026-07-12")
    plan.budget = Budget(total=10_000)
    plan.selected_transport = {
        "segments": [
            {"flight_no": "MU523"},
            {"flight_no": "IJ005"},
        ]
    }
    plan.accommodation = Accommodation(area="新宿", hotel="Hotel Locked")
    plan.accommodation_options = [
        {"name": "Hotel Locked"},
        {"name": "Hotel Other"},
    ]
    plan.daily_plans = [
        DayPlan.from_dict(
            {
                "day": 1,
                "date": "2026-07-10",
                "notes": "天气参考：小雨 / 18°C，建议带伞",
                "activities": [],
            }
        )
    ]

    errors = build_deliverable_consistency_errors(
        plan,
        {
            "travel_plan_markdown": (
                "# 东京旅行计划\n\n"
                "总预算：12000 元\n\n"
                "## 第 1 天 2026-07-15\n"
                "- 搭乘 NH972 抵达东京，入住 Hotel Other。\n"
                "- 7月东京平均 28.7°C，体感湿热。\n"
            ),
            "checklist_markdown": "# 清单\n\n- [ ] 护照\n",
        },
        {},
    )

    joined = "\n".join(errors)
    assert "日期与状态不一致" in joined
    assert "锁定交通不一致" in joined
    assert "锁定住宿不一致" in joined
    assert "总预算与状态不一致" in joined
    assert "天气温度与逐日行程不一致" in joined


def test_build_deliverable_consistency_errors_allows_matching_plan_facts():
    from api.orchestration.agent.hooks import build_deliverable_consistency_errors

    plan = TravelPlanState(session_id="s-consistency-ok", phase=4)
    plan.dates = DateRange(start="2026-07-10", end="2026-07-12")
    plan.budget = Budget(total=10_000)
    plan.selected_transport = {"segments": [{"flight_no": "MU523"}]}
    plan.accommodation = Accommodation(area="新宿", hotel="Hotel Locked")
    plan.accommodation_options = [{"name": "Hotel Locked"}, {"name": "Hotel Other"}]
    plan.daily_plans = [
        DayPlan.from_dict(
            {
                "day": 1,
                "date": "2026-07-10",
                "notes": "天气参考：小雨 / 18°C，建议带伞",
                "activities": [],
            }
        )
    ]

    errors = build_deliverable_consistency_errors(
        plan,
        {
            "travel_plan_markdown": (
                "# 东京旅行计划\n\n"
                "总预算：10000 元\n\n"
                "## 第 1 天 2026-07-10\n"
                "- 搭乘 MU523 抵达东京，入住 Hotel Locked。\n"
                "- 天气参考：18°C，小雨，建议带伞。\n"
            ),
            "checklist_markdown": "# 清单\n\n- [ ] 护照\n",
        },
        {},
    )

    assert errors == []


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    # Mock create_llm_provider used by on_soft_judge so it doesn't call real LLM
    async def _fake_judge_chat(messages, tools=None, stream=True, **kw):
        yield LLMChunk(
            type=ChunkType.TEXT_DELTA,
            content='{"overall":4.0,"pace":4,"geography":4,"coherence":4,"personalization":4,"suggestions":[]}',
        )
        yield LLMChunk(type=ChunkType.DONE)

    class _FakeJudgeLLM:
        async def chat(self, messages, **kw):
            async for chunk in _fake_judge_chat(messages, **kw):
                yield chunk

    import main as main_mod

    def _patched_create(config):
        return _FakeJudgeLLM()

    monkeypatch.setattr(main_mod, "create_llm_provider", _patched_create)


@pytest.fixture
def app():
    from main import create_app

    return create_app(config_path="__nonexistent__.yaml")


@pytest.fixture
def sessions(app):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        closure = getattr(endpoint, "__closure__", None)
        if endpoint is None or closure is None:
            continue
        for cell in closure:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    pytest.fail("Could not locate sessions dict from app closure")


@pytest.mark.asyncio
async def test_plan_tool_injects_realtime_incremental_feedback(app, sessions):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        trip_start = _future_date()
        plan.phase = 3
        plan.destination = "东京"
        plan.dates = DateRange(start=trip_start, end=_future_date(32))
        plan.budget = Budget(total=10_000)
        plan.accommodation = Accommodation(area="新宿", hotel="A")

        agent = session["agent"]

        captured_messages: list[list[Message]] = []

        async def fake_chat(messages, tools=None, stream=True):
            captured_messages.append([m for m in messages])
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_conflict",
                    name="save_day_plan",
                    arguments={
                        "mode": "create",
                        "day": 1,
                        "date": trip_start,
                        "activities": [
                            {
                                "name": "浅草寺",
                                "location": {
                                    "name": "浅草寺",
                                    "lat": 35.7148,
                                    "lng": 139.7967,
                                },
                                "start_time": "09:00",
                                "end_time": "10:00",
                                "category": "景点",
                                "cost": 0,
                            },
                            {
                                "name": "上野公园",
                                "location": {
                                    "name": "上野公园",
                                    "lat": 35.7156,
                                    "lng": 139.7745,
                                },
                                "start_time": "10:05",
                                "end_time": "11:00",
                                "category": "景点",
                                "cost": 0,
                                "transport_duration_min": 20,
                            },
                        ],
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "安排第一天"},
        )

    assert resp.status_code == 200
    realtime_messages = [
        message.content
        for call_messages in captured_messages[1:]
        for message in call_messages
        if message.role == Role.USER
        and message.transient
        and message.content
        and '<runtime_notice kind="validation">' in message.content
    ]
    assert any("[实时约束检查]" in content for content in realtime_messages)
    assert any("时间冲突" in content for content in realtime_messages)


@pytest.mark.asyncio
async def test_on_validate_records_state_changes_to_stats(app, sessions):
    """update_trip_basics should record normalized state_changes on the latest ToolCallRecord."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        plan.phase = 1
        plan.destination = "北京"

        agent = session["agent"]

        call_count = 0

        async def fake_chat(messages, tools=None, stream=True, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_dest",
                        name="update_trip_basics",
                        arguments={"destination": "东京"},
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
            else:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="好的")
                yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "去东京"},
        )

    assert resp.status_code == 200
    stats = session["stats"]
    utb_records = [
        r
        for r in stats.tool_calls
        if r.tool_name == "update_trip_basics" and r.status == "success"
    ]
    assert len(utb_records) >= 1
    rec = utb_records[-1]
    assert rec.state_changes is not None
    assert len(rec.state_changes) == 1
    assert rec.state_changes[0]["field"] == "destination"
    assert rec.state_changes[0]["before"] is None
    assert rec.state_changes[0]["after"] == "东京"


@pytest.mark.asyncio
async def test_save_day_plan_replace_existing_shows_soft_judge_after_tool_result(
    app,
    sessions,
):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

        session = sessions[session_id]
        plan: TravelPlanState = session["plan"]
        trip_date = _future_date()
        plan.phase = 3
        plan.destination = "惠州"
        plan.dates = DateRange(start=trip_date, end=trip_date)
        plan.budget = Budget(total=3_000)
        plan.accommodation = Accommodation(area="双月湾", hotel="A")
        plan.daily_plans = [
            DayPlan.from_dict(
                {
                    "day": 1,
                    "date": trip_date,
                    "activities": [
                        {
                            "name": "旧行程",
                            "location": {
                                "name": "双月湾",
                                "lat": 22.57,
                                "lng": 114.90,
                            },
                            "start_time": "09:00",
                            "end_time": "10:00",
                            "category": "景点",
                            "cost": 0,
                        }
                    ],
                }
            )
        ]

        agent = session["agent"]
        call_count = 0

        async def fake_chat(messages, tools=None, stream=True, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_save_day",
                        name="save_day_plan",
                        arguments={
                            "mode": "replace_existing",
                            "day": 1,
                            "date": trip_date,
                            "activities": [
                                {
                                    "name": "巽寮湾散步",
                                    "location": {
                                        "name": "巽寮湾",
                                        "lat": 22.68,
                                        "lng": 114.74,
                                    },
                                    "start_time": "09:00",
                                    "end_time": "11:00",
                                    "category": "景点",
                                    "cost": 0,
                                }
                            ],
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
            else:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已更新")
                yield LLMChunk(type=ChunkType.DONE)

        agent.llm.chat = fake_chat

        resp = await client.post(
            f"/api/chat/{session_id}",
            json={"message": "把惠州第一天换成海边慢走"},
        )

    assert resp.status_code == 200
    tool_result_pos = resp.text.index('"type": "tool_result"')
    soft_judge_pos = resp.text.index('"kind": "soft_judge"')
    assert tool_result_pos < soft_judge_pos
    assert '"label": "行程质量评审"' in resp.text
    assert '"status": "pending"' in resp.text
    assert '"status": "success"' in resp.text


def _plan_with_estimated_transport() -> TravelPlanState:
    plan = TravelPlanState(session_id="s-est", phase=4)
    plan.daily_plans = [
        DayPlan.from_dict(
            {
                "day": 1,
                "date": "2026-07-10",
                "activities": [
                    {
                        "name": "浅草寺",
                        "location": {"name": "浅草寺", "lat": 35.7148, "lng": 139.7967},
                        "start_time": "09:00",
                        "end_time": "10:00",
                        "category": "shrine",
                        "cost": 0,
                        "transport_duration_min": 25,
                        "transport_estimated": True,
                    }
                ],
            }
        )
    ]
    return plan


def test_estimation_visibility_flags_unmarked_estimate():
    from api.orchestration.agent.hooks import build_estimation_visibility_errors

    errors = build_estimation_visibility_errors(
        _plan_with_estimated_transport(),
        {
            "travel_plan_markdown": "# 计划\n\n## 第 1 天 2026-07-10\n- 浅草寺，交通约 25 分钟。\n",
            "checklist_markdown": "# 清单\n- [ ] 护照\n",
        },
        {},
    )
    assert errors


def test_estimation_visibility_allows_marked_estimate():
    from api.orchestration.agent.hooks import build_estimation_visibility_errors

    errors = build_estimation_visibility_errors(
        _plan_with_estimated_transport(),
        {
            "travel_plan_markdown": (
                "# 计划\n\n## 第 1 天 2026-07-10\n"
                "- 浅草寺，交通约 25 分钟（估算，未经路线工具验证）。\n"
            ),
            "checklist_markdown": "# 清单\n- [ ] 护照\n",
        },
        {},
    )
    assert errors == []


def test_estimation_visibility_ignores_when_no_estimate():
    from api.orchestration.agent.hooks import build_estimation_visibility_errors

    plan = TravelPlanState(session_id="s-noest", phase=4)
    plan.daily_plans = [
        DayPlan.from_dict(
            {
                "day": 1,
                "date": "2026-07-10",
                "activities": [
                    {
                        "name": "浅草寺",
                        "location": {"name": "浅草寺", "lat": 35.7148, "lng": 139.7967},
                        "start_time": "09:00",
                        "end_time": "10:00",
                        "category": "shrine",
                        "cost": 0,
                    }
                ],
            }
        )
    ]
    errors = build_estimation_visibility_errors(
        plan,
        {"travel_plan_markdown": "# 计划\n\n## 第 1 天\n- 浅草寺。\n", "checklist_markdown": "# 清单\n- [ ] 护照\n"},
        {},
    )
    assert errors == []


def test_deliverable_gate_prioritizes_consistency_over_estimation():
    from api.orchestration.agent.hooks import evaluate_deliverable_gate

    plan = _plan_with_estimated_transport()
    plan.dates = DateRange(start="2026-07-10", end="2026-07-10")
    plan.budget = Budget(total=10_000)

    code, errors = evaluate_deliverable_gate(
        plan,
        {
            "travel_plan_markdown": "# 计划\n\n总预算：99999 元\n\n## 第 1 天 2099-01-01\n- 浅草寺，交通约 25 分钟。\n",
            "checklist_markdown": "# 清单\n- [ ] 护照\n",
        },
        {},
    )
    assert code == "DELIVERABLE_FACTS_CONFLICT_WITH_PLAN"
    assert errors
