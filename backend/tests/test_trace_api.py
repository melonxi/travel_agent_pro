import pytest
from httpx import AsyncClient, ASGITransport
from main import create_app
from telemetry.stats import SessionStats


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return create_app()


def _get_sessions(app) -> dict:
    """Extract sessions dict from app closure."""
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__closure__"):
            continue
        free_vars = getattr(endpoint.__code__, "co_freevars", ())
        for name, cell in zip(free_vars, endpoint.__closure__ or ()):
            if name == "sessions":
                return cell.cell_contents
    raise RuntimeError("Cannot locate sessions dict")


@pytest.mark.asyncio
async def test_trace_not_found(app):
    """GET /api/sessions/nonexistent/trace returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/nonexistent/trace")
    assert resp.status_code == 404
    assert "detail" in resp.json()


@pytest.mark.asyncio
async def test_trace_empty_session(app):
    """Create session, GET trace → 200, total_iterations=0, empty iterations."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        create_resp = await client.post("/api/sessions")
        assert create_resp.status_code == 200
        session_id = create_resp.json()["session_id"]

        resp = await client.get(f"/api/sessions/{session_id}/trace")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == session_id
    assert data["total_iterations"] == 0
    assert data["iterations"] == []
    summary = data["summary"]
    assert summary["llm_call_count"] == 0
    assert summary["tool_call_count"] == 0
    assert summary["total_input_tokens"] == 0
    assert summary["total_output_tokens"] == 0


@pytest.mark.asyncio
async def test_trace_with_stats(app):
    """Inject LLM + tool call records, verify summary counts and breakdowns."""
    sessions = _get_sessions(app)
    session_id = "test-stats-session"
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        duration_ms=200.0,
        phase=1,
        iteration=1,
    )
    stats.record_tool_call(
        tool_name="web_search",
        duration_ms=150.0,
        status="ok",
        error_code=None,
        phase=1,
    )
    sessions[session_id] = {"stats": stats, "messages": [], "plan": None}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    assert resp.status_code == 200
    data = resp.json()

    summary = data["summary"]
    assert summary["llm_call_count"] == 1
    assert summary["tool_call_count"] == 1
    assert summary["total_input_tokens"] == 100
    assert summary["total_output_tokens"] == 50

    # by_model breakdown
    assert "gpt-4o" in summary["by_model"]
    model_data = summary["by_model"]["gpt-4o"]
    assert model_data["calls"] == 1
    assert model_data["cost_usd"] > 0

    # by_tool breakdown
    assert "web_search" in summary["by_tool"]
    tool_data = summary["by_tool"]["web_search"]
    assert tool_data["calls"] == 1
    assert tool_data["total_duration_ms"] == 150.0
    assert tool_data["avg_duration_ms"] == 150.0
    for td in summary["by_tool"].values():
        assert "total_duration_ms" in td
        assert "avg_duration_ms" in td
        assert "duration_ms" not in td


@pytest.mark.asyncio
async def test_trace_iterations_ordered(app):
    """Two LLM calls → two iterations in correct order with correct models."""
    sessions = _get_sessions(app)
    session_id = "test-iter-order"
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        duration_ms=200.0,
        phase=1,
        iteration=1,
    )
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=80,
        output_tokens=40,
        duration_ms=100.0,
        phase=1,
        iteration=2,
    )
    sessions[session_id] = {"stats": stats, "messages": [], "plan": None}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_iterations"] == 2
    assert len(data["iterations"]) == 2
    assert data["iterations"][0]["index"] == 1
    assert data["iterations"][0]["llm_call"]["model"] == "gpt-4o"
    assert data["iterations"][1]["index"] == 2
    assert data["iterations"][1]["llm_call"]["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_trace_tool_side_effects(app):
    """Tool calls have correct side_effect: web_search=read, update_plan_state=write."""
    sessions = _get_sessions(app)
    session_id = "test-side-effects"
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        duration_ms=200.0,
        phase=1,
        iteration=1,
    )
    stats.record_tool_call(
        tool_name="web_search",
        duration_ms=100.0,
        status="ok",
        error_code=None,
        phase=1,
    )
    stats.record_tool_call(
        tool_name="update_plan_state",
        duration_ms=50.0,
        status="ok",
        error_code=None,
        phase=1,
    )
    sessions[session_id] = {"stats": stats, "messages": [], "plan": None}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    assert resp.status_code == 200
    data = resp.json()

    tools = data["iterations"][0]["tool_calls"]
    assert len(tools) == 2
    assert tools[0]["name"] == "web_search"
    assert tools[0]["side_effect"] == "read"
    assert tools[1]["name"] == "update_plan_state"
    assert tools[1]["side_effect"] == "write"


@pytest.mark.asyncio
async def test_trace_orphan_tool_calls(app):
    """Tool calls without a parent LLM call should still appear in iterations."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        create_resp = await client.post("/api/sessions")
        session_id = create_resp.json()["session_id"]

    sessions = _get_sessions(app)
    stats: SessionStats = sessions[session_id]["stats"]
    stats.record_tool_call(
        tool_name="web_search",
        duration_ms=500.0,
        status="success",
        error_code=None,
        phase=1,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    data = resp.json()
    assert data["total_iterations"] == 1
    assert data["iterations"][0]["llm_call"] is None
    assert len(data["iterations"][0]["tool_calls"]) == 1
    assert data["iterations"][0]["tool_calls"][0]["name"] == "web_search"


def test_tool_call_record_new_fields():
    """ToolCallRecord accepts state_changes, parallel_group, validation_errors, judge_scores."""
    from telemetry.stats import ToolCallRecord

    rec = ToolCallRecord(
        tool_name="update_plan_state",
        duration_ms=50.0,
        status="ok",
        error_code=None,
        phase=1,
        state_changes=[{"field": "destination", "before": None, "after": "东京"}],
        parallel_group=1,
        validation_errors=["时间冲突"],
        judge_scores={"pace": 4, "geography": 5},
    )
    assert rec.state_changes == [
        {"field": "destination", "before": None, "after": "东京"}
    ]
    assert rec.parallel_group == 1
    assert rec.validation_errors == ["时间冲突"]
    assert rec.judge_scores == {"pace": 4, "geography": 5}


def test_tool_call_record_defaults_none():
    """New fields default to None for backward compatibility."""
    from telemetry.stats import ToolCallRecord

    rec = ToolCallRecord(
        tool_name="web_search",
        duration_ms=100.0,
        status="ok",
        error_code=None,
        phase=1,
    )
    assert rec.state_changes is None
    assert rec.parallel_group is None
    assert rec.validation_errors is None
    assert rec.judge_scores is None


def test_memory_hit_record():
    """MemoryHitRecord stores recall metadata."""
    from telemetry.stats import MemoryHitRecord

    rec = MemoryHitRecord(
        item_ids=["mem-1", "mem-2"],
        core_count=1,
        trip_count=0,
        phase_count=1,
    )
    assert rec.item_ids == ["mem-1", "mem-2"]
    assert rec.core_count == 1
    assert rec.timestamp > 0


def test_session_stats_memory_hits():
    """SessionStats has memory_hits list, defaults empty."""
    stats = SessionStats()
    assert stats.memory_hits == []


def test_session_stats_to_dict_includes_memory_hits():
    """to_dict includes memory_hits count."""
    from telemetry.stats import MemoryHitRecord

    stats = SessionStats()
    stats.memory_hits.append(
        MemoryHitRecord(
            item_ids=["m1"],
            core_count=1,
            trip_count=0,
            phase_count=0,
        )
    )
    d = stats.to_dict()
    assert d["memory_hit_count"] == 1


@pytest.mark.asyncio
async def test_update_plan_state_returns_previous_value():
    """update_plan_state should include previous_value in result."""
    from state.models import TravelPlanState
    from tools.update_plan_state import make_update_plan_state_tool

    plan = TravelPlanState(session_id="s1", phase=1, destination="北京")
    tool_fn = make_update_plan_state_tool(plan)
    result = await tool_fn(field="destination", value="东京")
    assert result["previous_value"] == "北京"
    assert result["updated_field"] == "destination"
    assert plan.destination == "东京"


@pytest.mark.asyncio
async def test_update_plan_state_previous_value_none_for_new_field():
    """previous_value is None when field was not previously set."""
    from state.models import TravelPlanState
    from tools.update_plan_state import make_update_plan_state_tool

    plan = TravelPlanState(session_id="s1", phase=1)
    tool_fn = make_update_plan_state_tool(plan)
    result = await tool_fn(field="destination", value="东京")
    assert result["previous_value"] is None
    assert result["updated_field"] == "destination"


@pytest.mark.asyncio
async def test_trace_state_changes_from_stats(app):
    """state_changes populated from ToolCallRecord.state_changes."""
    sessions = _get_sessions(app)
    session_id = "test-state-changes"
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        duration_ms=200.0,
        phase=1,
        iteration=1,
    )
    stats.record_tool_call(
        tool_name="update_plan_state",
        duration_ms=50.0,
        status="ok",
        error_code=None,
        phase=1,
    )
    stats.tool_calls[-1].state_changes = [
        {"field": "destination", "before": None, "after": "东京"}
    ]
    sessions[session_id] = {
        "stats": stats,
        "messages": [],
        "plan": None,
        "compression_events": [],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    data = resp.json()

    assert data["iterations"][0]["state_changes"] == [
        {"field": "destination", "before": None, "after": "东京"}
    ]


@pytest.mark.asyncio
async def test_trace_compression_event(app):
    """compression_event populated from session compression_events."""
    sessions = _get_sessions(app)
    session_id = "test-compression"
    stats = SessionStats()
    ts = 1000.0
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        duration_ms=200.0,
        phase=1,
        iteration=1,
    )
    stats.llm_calls[-1].timestamp = ts
    sessions[session_id] = {
        "stats": stats,
        "messages": [],
        "plan": None,
        "compression_events": [
            {
                "timestamp": ts - 1,
                "mode": "tool_compaction",
                "reason": "test compression",
                "message_count_before": 20,
                "message_count_after": 10,
            }
        ],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    data = resp.json()

    assert data["iterations"][0]["compression_event"] is not None
    assert "tool_compaction" in data["iterations"][0]["compression_event"]


@pytest.mark.asyncio
async def test_trace_parallel_group(app):
    """parallel_group populated from ToolCallRecord."""
    sessions = _get_sessions(app)
    session_id = "test-parallel"
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        duration_ms=200.0,
        phase=1,
        iteration=1,
    )
    stats.record_tool_call(
        tool_name="web_search",
        duration_ms=100.0,
        status="ok",
        error_code=None,
        phase=1,
        parallel_group=1,
    )
    stats.record_tool_call(
        tool_name="search_flights",
        duration_ms=150.0,
        status="ok",
        error_code=None,
        phase=1,
        parallel_group=1,
    )
    sessions[session_id] = {
        "stats": stats,
        "messages": [],
        "plan": None,
        "compression_events": [],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    data = resp.json()

    tools = data["iterations"][0]["tool_calls"]
    assert tools[0]["parallel_group"] == 1
    assert tools[1]["parallel_group"] == 1


@pytest.mark.asyncio
async def test_trace_memory_hits(app):
    """memory_hits populated from SessionStats.memory_hits."""
    from telemetry.stats import MemoryHitRecord

    sessions = _get_sessions(app)
    session_id = "test-memory-hits"
    stats = SessionStats()
    stats.record_llm_call(
        provider="openai",
        model="gpt-4o",
        input_tokens=100,
        output_tokens=50,
        duration_ms=200.0,
        phase=1,
        iteration=1,
    )
    stats.memory_hits.append(
        MemoryHitRecord(
            item_ids=["m1", "m2"],
            core_count=1,
            trip_count=1,
            phase_count=0,
            timestamp=stats.llm_calls[-1].timestamp,
        )
    )
    sessions[session_id] = {
        "stats": stats,
        "messages": [],
        "plan": None,
        "compression_events": [],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/trace")
    data = resp.json()

    hits = data["iterations"][0]["memory_hits"]
    assert hits is not None
    assert hits["item_ids"] == ["m1", "m2"]
    assert hits["core"] == 1
