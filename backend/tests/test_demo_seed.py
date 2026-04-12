from __future__ import annotations

from pathlib import Path

import pytest

from memory.demo_seed import seed_demo_memory
from memory.models import MemoryItem, MemorySource, TripEpisode
from memory.store import FileMemoryStore


@pytest.mark.asyncio
async def test_seed_demo_memory_creates_active_items_and_episode(tmp_path: Path):
    seed_file = (
        Path(__file__).resolve().parents[2] / "scripts" / "demo" / "seed-memory.json"
    )

    summary = await seed_demo_memory(seed_file=seed_file, data_dir=tmp_path)

    assert summary.user_id == "default_user"
    assert summary.items_seeded == 3
    assert summary.episodes_seeded == 1

    store = FileMemoryStore(tmp_path)
    items = await store.list_items("default_user", status="active")
    episodes = await store.list_episodes("default_user")

    assert {(item.domain, item.key, item.value) for item in items} == {
        ("travel", "travel_style", "文化体验为主，适度冒险"),
        ("accommodation", "accommodation_preference", "偏好精品民宿和设计酒店"),
        ("travel", "pace_preference", "不赶路，每天2-3个景点"),
    }
    assert all(item.scope == "global" for item in items)
    assert len(episodes) == 1
    assert episodes[0].destination == "京都"
    assert episodes[0].dates == "2025-03"
    assert episodes[0].satisfaction == 5


@pytest.mark.asyncio
async def test_seed_demo_memory_is_idempotent(tmp_path: Path):
    seed_file = (
        Path(__file__).resolve().parents[2] / "scripts" / "demo" / "seed-memory.json"
    )

    first = await seed_demo_memory(seed_file=seed_file, data_dir=tmp_path)
    second = await seed_demo_memory(seed_file=seed_file, data_dir=tmp_path)

    assert first.items_seeded == 3
    assert first.episodes_seeded == 1
    assert second.items_seeded == 0
    assert second.episodes_seeded == 0

    store = FileMemoryStore(tmp_path)
    assert len(await store.list_items("default_user", status="active")) == 3
    assert len(await store.list_episodes("default_user")) == 1


@pytest.mark.asyncio
async def test_seed_demo_memory_can_reset_existing_demo_user(tmp_path: Path):
    seed_file = (
        Path(__file__).resolve().parents[2] / "scripts" / "demo" / "seed-memory.json"
    )
    store = FileMemoryStore(tmp_path)

    await store.upsert_item(
        MemoryItem(
            id="stale-item",
            user_id="default_user",
            type="preference",
            domain="travel",
            key="old_pref",
            value="旧数据",
            scope="global",
            polarity="neutral",
            confidence=0.3,
            status="active",
            source=MemorySource(kind="seed", session_id=""),
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )
    )
    await store.append_episode(
        TripEpisode(
            id="stale-episode",
            user_id="default_user",
            session_id="stale-session",
            trip_id=None,
            destination="旧行程",
            dates="2024-01",
            travelers=None,
            budget=None,
            selected_skeleton=None,
            final_plan_summary="旧摘要",
            accepted_items=[],
            rejected_items=[],
            lessons=["旧 lesson"],
            satisfaction=1,
            created_at="2026-01-01T00:00:00",
        )
    )

    summary = await seed_demo_memory(
        seed_file=seed_file,
        data_dir=tmp_path,
        reset_user=True,
    )

    assert summary.items_seeded == 3
    assert summary.episodes_seeded == 1

    items = await store.list_items("default_user", status="active")
    episodes = await store.list_episodes("default_user")

    assert {item.key for item in items} == {
        "travel_style",
        "accommodation_preference",
        "pace_preference",
    }
    assert len(episodes) == 1
    assert episodes[0].destination == "京都"
