from __future__ import annotations

from pathlib import Path
import json

import pytest

from memory.demo_seed import seed_demo_memory
from memory.v3_store import FileMemoryV3Store


@pytest.mark.asyncio
async def test_seed_demo_memory_creates_active_items_and_episode(tmp_path: Path):
    seed_file = (
        Path(__file__).resolve().parents[2] / "scripts" / "demo" / "seed-memory.json"
    )

    summary = await seed_demo_memory(seed_file=seed_file, data_dir=tmp_path)

    assert summary.user_id == "default_user"
    assert summary.items_seeded == 3
    assert summary.episodes_seeded == 1

    store = FileMemoryV3Store(tmp_path)
    profile = await store.load_profile("default_user")
    episodes = await store.list_episodes("default_user")
    slices = await store.list_episode_slices("default_user")

    assert {
        (item.domain, item.key, item.value)
        for item in profile.stable_preferences
    } == {
        ("travel", "travel_style", "文化体验为主，适度冒险"),
        ("accommodation", "accommodation_preference", "偏好精品民宿和设计酒店"),
        ("travel", "pace_preference", "不赶路，每天2-3个景点"),
    }
    assert all(item.status == "active" for item in profile.stable_preferences)
    assert len(episodes) == 1
    assert episodes[0].destination == "京都"
    assert episodes[0].dates == {
        "start": "2025-03-01",
        "end": "2025-03-31",
        "label": "2025-03",
    }
    assert {slice_.slice_type for slice_ in slices} == {
        "itinerary_pattern",
        "budget_signal",
        "pitfall",
    }


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

    store = FileMemoryV3Store(tmp_path)
    profile = await store.load_profile("default_user")
    assert len(profile.stable_preferences) == 3
    assert len(await store.list_episodes("default_user")) == 1


@pytest.mark.asyncio
async def test_seed_demo_memory_can_reset_existing_demo_user(tmp_path: Path):
    seed_file = (
        Path(__file__).resolve().parents[2] / "scripts" / "demo" / "seed-memory.json"
    )
    user_dir = tmp_path / "users" / "default_user"
    user_dir.mkdir(parents=True)
    (user_dir / "memory.json").write_text(json.dumps({"stale": True}), encoding="utf-8")

    summary = await seed_demo_memory(
        seed_file=seed_file,
        data_dir=tmp_path,
        reset_user=True,
    )

    assert summary.items_seeded == 3
    assert summary.episodes_seeded == 1

    store = FileMemoryV3Store(tmp_path)
    profile = await store.load_profile("default_user")
    episodes = await store.list_episodes("default_user")

    assert {item.key for item in profile.stable_preferences} == {
        "travel_style",
        "accommodation_preference",
        "pace_preference",
    }
    assert len(episodes) == 1
    assert episodes[0].destination == "京都"
