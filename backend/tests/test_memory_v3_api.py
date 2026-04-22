from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from main import create_app
from memory.v3_models import (
    ArchivedTripEpisode,
    EpisodeSlice,
    MemoryProfileItem,
    SessionWorkingMemory,
    UserMemoryProfile,
    WorkingMemoryItem,
)
from memory.v3_store import FileMemoryV3Store


def _write_config(tmp_path: Path, data_dir: Path) -> Path:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"""
llm:
  provider: openai
  model: gpt-4o
data_dir: "{data_dir}"
flyai:
  enabled: false
telemetry:
  enabled: false
""".strip(),
        encoding="utf-8",
    )
    return config_file


async def _seed_profile(data_dir: Path, user_id: str) -> UserMemoryProfile:
    store = FileMemoryV3Store(data_dir)
    profile = UserMemoryProfile.empty(user_id)
    profile.constraints.append(
        MemoryProfileItem(
            id="constraints:flight:avoid_red_eye",
            domain="flight",
            key="avoid_red_eye",
            value=True,
            polarity="positive",
            stability="hard",
            confidence=0.95,
            status="active",
        )
    )
    await store.save_profile(profile)
    return profile


async def _seed_working_memory(
    data_dir: Path, user_id: str, session_id: str, trip_id: str | None
) -> SessionWorkingMemory:
    store = FileMemoryV3Store(data_dir)
    memory = SessionWorkingMemory.empty(user_id, session_id, trip_id)
    item = WorkingMemoryItem(
        id="wm-1",
        phase=1,
        kind="note",
        domains=["flight"],
        content="保留周五下午返程航班",
        reason="用户本次需求",
        status="active",
        expires={"on_trip_end": True},
        created_at="2026-04-19T08:00:00",
    )
    await store.upsert_working_memory_item(user_id, session_id, trip_id, item)
    memory.items.append(item)
    return memory


async def _seed_episode_slice(data_dir: Path, user_id: str) -> EpisodeSlice:
    store = FileMemoryV3Store(data_dir)
    slice_ = EpisodeSlice(
        id="slice_1",
        user_id=user_id,
        source_episode_id="episode-1",
        source_trip_id="trip-1",
        slice_type="lodging",
        domains=["lodging"],
        entities={"destination": "京都"},
        keywords=["上次", "住宿"],
        content="上次京都住在民宿，用户满意度 5/5",
        applicability="京都相关规划",
        created_at="2026-03-01T00:00:00",
    )
    await store.append_episode_slice(slice_)
    return slice_


async def _seed_episode(data_dir: Path, user_id: str) -> ArchivedTripEpisode:
    store = FileMemoryV3Store(data_dir)
    episode = ArchivedTripEpisode(
        id="episode-v3-1",
        user_id=user_id,
        session_id="session-v3",
        trip_id="trip-v3",
        destination="京都",
        dates={"start": "2026-03-01", "end": "2026-03-05", "total_days": 5},
        travelers={"adults": 2},
        budget={"amount": 8000, "currency": "CNY"},
        selected_skeleton={"id": "sk-1", "title": "京都慢游"},
        selected_transport=None,
        accommodation=None,
        daily_plan_summary=[],
        final_plan_summary="一次轻松的京都旅行。",
        decision_log=[],
        lesson_log=[{"kind": "pitfall", "content": "町屋住宿体验很好"}],
        created_at="2026-03-06T00:00:00",
        completed_at="2026-03-06T00:00:00",
    )
    await store.append_episode(episode)
    return episode


@pytest.fixture
def v3_app(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = _write_config(tmp_path, data_dir)
    app = create_app(str(config_path))
    return app, data_dir


def _get_closure(app, name: str):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__closure__"):
            continue
        free_vars = getattr(endpoint.__code__, "co_freevars", ())
        for var_name, cell in zip(free_vars, endpoint.__closure__ or ()):
            if var_name == name:
                return cell.cell_contents
    raise RuntimeError(f"Cannot locate {name}")


@pytest.mark.asyncio
async def test_get_memory_profile_returns_v3_payload(v3_app):
    app, data_dir = v3_app
    user_id = "default_user"
    await _seed_profile(data_dir, user_id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/memory/{user_id}/profile")

    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == user_id
    assert data["schema_version"] == 3
    assert len(data["constraints"]) == 1
    entry = data["constraints"][0]
    assert entry["id"] == "constraints:flight:avoid_red_eye"
    assert entry["status"] == "active"


@pytest.mark.asyncio
async def test_list_memory_episode_slices(v3_app):
    app, data_dir = v3_app
    user_id = "default_user"
    await _seed_episode_slice(data_dir, user_id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/memory/{user_id}/episode-slices")

    assert resp.status_code == 200
    payload = resp.json()
    assert "slices" in payload
    assert len(payload["slices"]) == 1
    slice_payload = payload["slices"][0]
    assert slice_payload["id"] == "slice_1"
    assert slice_payload["entities"]["destination"] == "京都"


@pytest.mark.asyncio
async def test_get_session_working_memory(v3_app):
    app, data_dir = v3_app
    user_id = "default_user"
    session_id = "session-xyz"
    trip_id = "trip-xyz"

    sessions = _get_closure(app, "sessions")

    class _StubPlan:
        def __init__(self, trip_id: str | None):
            self.trip_id = trip_id

    sessions[session_id] = {"plan": _StubPlan(trip_id)}

    await _seed_working_memory(data_dir, user_id, session_id, trip_id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            f"/api/memory/{user_id}/sessions/{session_id}/working-memory"
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["user_id"] == user_id
    assert payload["session_id"] == session_id
    assert payload["trip_id"] == trip_id
    assert len(payload["items"]) == 1
    assert payload["items"][0]["id"] == "wm-1"


@pytest.mark.asyncio
async def test_profile_item_actions_update_v3_profile(v3_app):
    app, data_dir = v3_app
    user_id = "default_user"
    await _seed_profile(data_dir, user_id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        confirm_resp = await client.post(
            f"/api/memory/{user_id}/profile/constraints:flight:avoid_red_eye/confirm",
        )
        delete_resp = await client.delete(
            f"/api/memory/{user_id}/profile/constraints:flight:avoid_red_eye"
        )
        profile_resp = await client.get(f"/api/memory/{user_id}/profile")

    assert confirm_resp.status_code == 200
    assert delete_resp.status_code == 200
    assert profile_resp.status_code == 200
    assert profile_resp.json()["constraints"] == []


@pytest.mark.asyncio
async def test_v3_episodes_route_returns_v3_payload_without_deprecated(v3_app):
    app, data_dir = v3_app
    user_id = "default_user"
    await _seed_episode(data_dir, user_id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        episodes_resp = await client.get(f"/api/memory/{user_id}/episodes")

    assert episodes_resp.status_code == 200
    episodes_payload = episodes_resp.json()
    assert "deprecated" not in episodes_payload
    assert episodes_payload["episodes"][0]["id"] == "episode-v3-1"


@pytest.mark.asyncio
async def test_legacy_memory_routes_are_removed(v3_app):
    app, _ = v3_app
    user_id = "default_user"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        items_resp = await client.get(f"/api/memory/{user_id}")
        confirm_resp = await client.post(
            f"/api/memory/{user_id}/confirm",
            json={"item_id": "constraints:flight:avoid_red_eye"},
        )
        reject_resp = await client.post(
            f"/api/memory/{user_id}/reject",
            json={"item_id": "constraints:flight:avoid_red_eye"},
        )
        events_resp = await client.post(
            f"/api/memory/{user_id}/events",
            json={
                "event_type": "reject",
                "object_type": "phase_output",
                "object_payload": {"to_phase": 3},
                "reason_text": "用户要求回退",
            },
        )
        delete_resp = await client.delete(
            f"/api/memory/{user_id}/constraints:flight:avoid_red_eye"
        )

    assert items_resp.status_code == 404
    assert confirm_resp.status_code == 404
    assert reject_resp.status_code == 404
    assert events_resp.status_code == 404
    assert delete_resp.status_code == 404


def test_memory_hit_record_includes_v3_sources():
    from telemetry.stats import MemoryHitRecord

    record = MemoryHitRecord(
        sources={
            "working_memory": 0,
            "query_profile": 1,
            "episode_slice": 1,
        },
        profile_ids=["constraints:flight:avoid_red_eye"],
        working_memory_ids=[],
        slice_ids=["slice_1"],
        matched_reasons=["用户询问上次京都住宿"],
    )

    payload = record.to_dict()
    assert payload["sources"]["episode_slice"] == 1
    assert payload["sources"]["query_profile"] == 1
    assert len(payload["sources"]) == 3
    assert payload["profile_ids"] == ["constraints:flight:avoid_red_eye"]
    assert payload["slice_ids"] == ["slice_1"]
    assert payload["matched_reasons"] == ["用户询问上次京都住宿"]
    assert payload["working_memory_ids"] == []


def test_memory_hit_record_defaults_are_empty():
    from telemetry.stats import MemoryHitRecord

    record = MemoryHitRecord(sources={"query_profile": 1})
    payload = record.to_dict()
    assert payload["profile_ids"] == []
    assert payload["working_memory_ids"] == []
    assert payload["slice_ids"] == []
    assert payload["matched_reasons"] == []
    assert record.timestamp >= 0.0
