from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memory.models import MemoryEvent, MemoryItem, MemorySource, TripEpisode
from memory.v3_models import UserMemoryProfile
from scripts.migrate_memory_v2_to_v3 import migrate_user


def _write_v2_user_data(
    data_dir: Path,
    user_id: str,
    *,
    memory_items: list[MemoryItem],
    events: list[MemoryEvent],
    episodes: list[TripEpisode],
) -> None:
    user_dir = data_dir / "users" / user_id
    user_dir.mkdir(parents=True, exist_ok=True)

    (user_dir / "memory.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "user_id": user_id,
                "items": [item.to_dict() for item in memory_items],
                "legacy": {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (user_dir / "memory_events.jsonl").write_text(
        "\n".join(json.dumps(event.to_dict(), ensure_ascii=False) for event in events)
        + "\n",
        encoding="utf-8",
    )
    (user_dir / "trip_episodes.jsonl").write_text(
        "\n".join(json.dumps(episode.to_dict(), ensure_ascii=False) for episode in episodes)
        + "\n",
        encoding="utf-8",
    )


def _sample_episode(user_id: str) -> TripEpisode:
    return TripEpisode(
        id="ep_kyoto_2026",
        user_id=user_id,
        session_id="s1",
        trip_id="trip_123",
        destination="京都",
        dates="2026-05-01 to 2026-05-05",
        travelers={"adults": 2},
        budget={"amount": 20000, "currency": "CNY"},
        selected_skeleton={
            "id": "balanced",
            "name": "轻松版",
            "summary": "节奏舒适，保留自由活动时间。",
        },
        final_plan_summary="这次京都之行选择了轻松节奏和町屋住宿。",
        accepted_items=[{"type": "skeleton", "id": "balanced", "name": "轻松版"}],
        rejected_items=[{"type": "hotel", "name": "商务连锁酒店"}],
        lessons=["上午安排太满会让后半天疲劳。"],
        satisfaction=5,
        created_at="2026-04-19T00:00:00",
    )


def _sample_memory_items(user_id: str) -> list[MemoryItem]:
    return [
        MemoryItem(
            id="mem_preferred_pace",
            user_id=user_id,
            type="preference",
            domain="pace",
            key="preferred_pace",
            value="轻松",
            scope="global",
            polarity="like",
            confidence=0.9,
            status="active",
            source=MemorySource(kind="message", session_id="s1", quote="我喜欢轻松一点"),
            created_at="2026-04-11T00:00:00",
            updated_at="2026-04-11T00:00:00",
        ),
        MemoryItem(
            id="mem_avoid_hostel",
            user_id=user_id,
            type="rejection",
            domain="hotel",
            key="avoid",
            value="青旅",
            scope="global",
            polarity="avoid",
            confidence=0.95,
            status="active",
            source=MemorySource(kind="message", session_id="s1", quote="不住青旅"),
            created_at="2026-04-11T00:00:00",
            updated_at="2026-04-11T00:00:00",
        ),
        MemoryItem(
            id="mem_trip_item",
            user_id=user_id,
            type="preference",
            domain="attraction",
            key="trip_only",
            value="只适用于本次行程",
            scope="trip",
            polarity="like",
            confidence=0.5,
            status="active",
            source=MemorySource(kind="message", session_id="s1", quote="临时偏好"),
            trip_id="trip_123",
            destination="京都",
            created_at="2026-04-11T00:00:00",
            updated_at="2026-04-11T00:00:00",
        ),
    ]


def _sample_event(user_id: str) -> MemoryEvent:
    return MemoryEvent(
        id="evt1",
        user_id=user_id,
        session_id="s1",
        event_type="accept",
        object_type="preference",
        object_payload={"id": "mem_preferred_pace"},
        reason_text=None,
        created_at="2026-04-11T00:00:00",
    )


def test_migrate_user_maps_global_items_and_generates_episodes_and_slices(tmp_path: Path):
    _write_v2_user_data(
        tmp_path,
        "u1",
        memory_items=_sample_memory_items("u1"),
        events=[_sample_event("u1")],
        episodes=[_sample_episode("u1")],
    )

    result = migrate_user(tmp_path, "u1")

    memory_dir = tmp_path / "users" / "u1" / "memory"
    profile = UserMemoryProfile.from_dict(
        json.loads((memory_dir / "profile.json").read_text(encoding="utf-8")),
        user_id="u1",
    )

    assert profile.stable_preferences[0].key == "preferred_pace"
    assert profile.rejections[0].value == "青旅"
    assert (memory_dir / "legacy_ignored.jsonl").exists()
    assert result["ignored_trip_items"] == 1
    assert (memory_dir / "events.jsonl").exists()
    assert (memory_dir / "episodes.jsonl").exists()
    assert (memory_dir / "episode_slices.jsonl").exists()

    episodes = [
        json.loads(line)
        for line in (memory_dir / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    slices = [
        json.loads(line)
        for line in (memory_dir / "episode_slices.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert episodes[0]["id"] == "ep_kyoto_2026"
    assert any(item["source_episode_id"] == "ep_kyoto_2026" for item in slices)
    assert result["events"] == 1
    assert result["episodes"] == 1
    assert result["slices"] == len(slices)
    assert result["would_write"] is True


def test_migrate_user_dry_run_does_not_write_files(tmp_path: Path):
    _write_v2_user_data(
        tmp_path,
        "u1",
        memory_items=_sample_memory_items("u1"),
        events=[_sample_event("u1")],
        episodes=[_sample_episode("u1")],
    )

    result = migrate_user(tmp_path, "u1", dry_run=True)

    assert result["would_write"] is True
    assert not (tmp_path / "users" / "u1" / "memory" / "profile.json").exists()
    assert not (tmp_path / "users" / "u1" / "memory").exists()
    assert (tmp_path / "users" / "u1" / "memory.json").exists()


def test_migrate_user_is_idempotent(tmp_path: Path):
    _write_v2_user_data(
        tmp_path,
        "u1",
        memory_items=_sample_memory_items("u1"),
        events=[_sample_event("u1")],
        episodes=[_sample_episode("u1")],
    )

    first = migrate_user(tmp_path, "u1")
    second = migrate_user(tmp_path, "u1")

    memory_dir = tmp_path / "users" / "u1" / "memory"
    profile = UserMemoryProfile.from_dict(
        json.loads((memory_dir / "profile.json").read_text(encoding="utf-8")),
        user_id="u1",
    )
    episodes = [
        json.loads(line)
        for line in (memory_dir / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    slices = [
        json.loads(line)
        for line in (memory_dir / "episode_slices.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert first["would_write"] is True
    assert second["would_write"] is False
    assert len(profile.stable_preferences) == 1
    assert len(profile.rejections) == 1
    assert len(episodes) == 1
    assert len(slices) == len({item["id"] for item in slices})
    assert (tmp_path / "users" / "u1" / "legacy_memory_v2").exists()
