from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from memory.models import (
    MemoryEvent,
    MemoryItem,
    MemorySource,
    Rejection,
    TripEpisode,
    TripSummary,
    UserMemory,
)
from memory.store import FileMemoryStore


@pytest.mark.asyncio
async def test_load_empty_returns_schema_v2(tmp_path: Path):
    store = FileMemoryStore(tmp_path)

    envelope = await store.load_envelope("u1")

    assert envelope["schema_version"] == 2
    assert envelope["user_id"] == "u1"
    assert envelope["items"] == []
    assert envelope["legacy"] == {}


@pytest.mark.asyncio
async def test_migrates_legacy_user_memory(tmp_path: Path):
    user_dir = tmp_path / "users" / "u1"
    user_dir.mkdir(parents=True)
    legacy = UserMemory(
        user_id="u1",
        explicit_preferences={"节奏": "轻松"},
        implicit_preferences={"住宿": "民宿"},
        rejections=[
            Rejection(item="红眼航班", reason="休息不好", permanent=True),
            Rejection(item="长途大巴", reason="太累", permanent=False),
        ],
    )
    (user_dir / "memory.json").write_text(
        json.dumps(legacy.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )
    store = FileMemoryStore(tmp_path)

    items = await store.list_items("u1")

    assert {item.key for item in items} == {"节奏", "住宿", "avoid"}
    assert [item for item in items if item.key == "avoid"] and len(
        [item for item in items if item.key == "avoid"]
    ) == 2
    migrated = {
        item.key: [candidate for candidate in items if candidate.key == item.key]
        for item in items
        if item.key != "avoid"
    }
    assert migrated["节奏"][0].type == "preference"
    assert migrated["节奏"][0].scope == "global"
    assert migrated["节奏"][0].status == "active"
    assert migrated["节奏"][0].confidence == 0.8
    assert migrated["住宿"][0].confidence == 0.6
    avoid_items = [item for item in items if item.key == "avoid"]
    assert {item.polarity for item in avoid_items} == {"avoid"}
    assert {item.status for item in avoid_items} == {"active", "pending"}
    assert {item.attributes["reason"] for item in avoid_items} == {"休息不好", "太累"}
    assert {item.source.kind for item in avoid_items} == {"migration"}


@pytest.mark.asyncio
async def test_migration_merges_duplicate_preference_keys(tmp_path: Path):
    user_dir = tmp_path / "users" / "u1"
    user_dir.mkdir(parents=True)
    legacy = UserMemory(
        user_id="u1",
        explicit_preferences={"节奏": "轻松"},
        implicit_preferences={"节奏": "慢速", "住宿": "酒店"},
    )
    (user_dir / "memory.json").write_text(
        json.dumps(legacy.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )

    store = FileMemoryStore(tmp_path)
    items = await store.list_items("u1")
    pace_items = [item for item in items if item.key == "节奏"]

    assert len(pace_items) == 1
    assert pace_items[0].value == "轻松"
    assert pace_items[0].confidence == 0.8
    assert {item.key for item in items} == {"节奏", "住宿"}


@pytest.mark.asyncio
async def test_legacy_trip_history_is_visible_through_list_episodes(tmp_path: Path):
    user_dir = tmp_path / "users" / "u1"
    user_dir.mkdir(parents=True)
    legacy = UserMemory(
        user_id="u1",
        trip_history=[
            TripSummary(
                destination="Osaka",
                dates="2025-10",
                satisfaction=4,
                notes="节奏舒适",
            )
        ],
    )
    (user_dir / "memory.json").write_text(
        json.dumps(legacy.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )

    store = FileMemoryStore(tmp_path)
    episodes = await store.list_episodes("u1")

    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.destination == "Osaka"
    assert episode.dates == "2025-10"
    assert episode.satisfaction == 4
    assert "节奏舒适" in episode.final_plan_summary or "节奏舒适" in episode.lessons


@pytest.mark.asyncio
async def test_upsert_item_writes_schema_v2(tmp_path: Path):
    store = FileMemoryStore(tmp_path)
    item = MemoryItem(
        id="mem1",
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        value="轻松",
        scope="global",
        polarity="like",
        confidence=0.9,
        status="active",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )

    await store.upsert_item(item)

    loaded = await store.list_items("u1")
    assert loaded == [item]
    raw = json.loads((tmp_path / "users" / "u1" / "memory.json").read_text())
    assert raw["schema_version"] == 2
    assert raw["user_id"] == "u1"
    assert raw["items"][0]["id"] == "mem1"


@pytest.mark.asyncio
async def test_update_status_marks_item(tmp_path: Path):
    store = FileMemoryStore(tmp_path)
    item = MemoryItem(
        id="mem1",
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        value="轻松",
        scope="global",
        polarity="like",
        confidence=0.9,
        status="pending",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )

    await store.upsert_item(item)
    changed = await store.update_status("u1", "mem1", "active")

    assert changed is True
    assert (await store.list_items("u1"))[0].status == "active"


@pytest.mark.asyncio
async def test_update_status_returns_false_for_missing_user_without_creating_file(
    tmp_path: Path,
):
    store = FileMemoryStore(tmp_path)

    changed = await store.update_status("u-missing", "mem1", "active")

    assert changed is False
    assert not (tmp_path / "users" / "u-missing" / "memory.json").exists()


@pytest.mark.asyncio
async def test_update_status_returns_false_for_missing_item_without_writing_file(
    tmp_path: Path,
):
    store = FileMemoryStore(tmp_path)
    item = MemoryItem(
        id="mem1",
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        value="轻松",
        scope="global",
        polarity="like",
        confidence=0.9,
        status="pending",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )

    await store.upsert_item(item)
    raw_before = json.loads((tmp_path / "users" / "u1" / "memory.json").read_text())
    changed = await store.update_status("u1", "missing", "active")
    raw_after = json.loads((tmp_path / "users" / "u1" / "memory.json").read_text())

    assert changed is False
    assert raw_after == raw_before


@pytest.mark.asyncio
async def test_append_event_writes_jsonl(tmp_path: Path):
    store = FileMemoryStore(tmp_path)
    event = MemoryEvent(
        id="evt1",
        user_id="u1",
        session_id="s1",
        event_type="accept",
        object_type="skeleton",
        object_payload={"id": "sk1"},
        reason_text=None,
        created_at="2026-04-11T00:00:00",
    )

    await store.append_event(event)

    path = tmp_path / "users" / "u1" / "memory_events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [event.to_dict()]


@pytest.mark.asyncio
async def test_append_episode_and_list_episodes(tmp_path: Path):
    store = FileMemoryStore(tmp_path)
    episode = TripEpisode(
        id="ep1",
        user_id="u1",
        session_id="s1",
        trip_id="trip1",
        destination="Tokyo",
        dates="2026-05",
        travelers={"adults": 2},
        budget={"total": 30000, "currency": "CNY"},
        selected_skeleton={"id": "sk1"},
        final_plan_summary="Tokyo trip",
        accepted_items=[{"type": "skeleton", "id": "sk1"}],
        rejected_items=[],
        lessons=["likes easy pace"],
        satisfaction=5,
        created_at="2026-04-11T00:00:00",
    )

    await store.append_episode(episode)
    await store.append_episode(episode)

    all_episodes = await store.list_episodes("u1")
    tokyo_episodes = await store.list_episodes("u1", destination="Tokyo")
    assert all_episodes == [episode]
    assert tokyo_episodes == [episode]


@pytest.mark.asyncio
async def test_concurrent_upserts_do_not_drop_items(tmp_path: Path):
    store = FileMemoryStore(tmp_path)

    async def write_item(index: int):
        await store.upsert_item(
            MemoryItem(
                id=f"mem{index}",
                user_id="u1",
                type="preference",
                domain="general",
                key=f"k{index}",
                value=f"v{index}",
                scope="global",
                polarity="neutral",
                confidence=0.8,
                status="active",
                source=MemorySource(kind="message", session_id="s1"),
                created_at="2026-04-11T00:00:00",
                updated_at="2026-04-11T00:00:00",
            )
        )

    await asyncio.gather(*(write_item(i) for i in range(10)))

    loaded = await store.list_items("u1")
    assert len(loaded) == 10
    assert {item.id for item in loaded} == {f"mem{i}" for i in range(10)}

