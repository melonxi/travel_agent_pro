import json

import pytest

from memory.v3_models import (
    EpisodeSlice,
    MemoryProfileItem,
    WorkingMemoryItem,
)
from memory.v3_store import FileMemoryV3Store


@pytest.mark.asyncio
async def test_profile_defaults_to_empty(tmp_path):
    store = FileMemoryV3Store(tmp_path)

    profile = await store.load_profile("u1")

    assert profile.schema_version == 3
    assert profile.user_id == "u1"
    assert profile.constraints == []


@pytest.mark.asyncio
async def test_upsert_profile_item_by_bucket(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    item = MemoryProfileItem(
        id="constraints:flight:avoid_red_eye",
        domain="flight",
        key="avoid_red_eye",
        value=True,
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.9,
        status="active",
        context={},
        applicability="适用于所有旅行。",
        recall_hints={"keywords": ["红眼航班"]},
        source_refs=[],
        created_at="2026-04-19T00:00:00",
        updated_at="2026-04-19T00:00:00",
    )

    await store.upsert_profile_item("u1", "constraints", item)
    item.confidence = 0.95
    await store.upsert_profile_item("u1", "constraints", item)

    profile = await store.load_profile("u1")
    assert len(profile.constraints) == 1
    assert profile.constraints[0].confidence == 0.95


@pytest.mark.asyncio
async def test_working_memory_is_session_scoped(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    item = WorkingMemoryItem(
        id="wm_1",
        phase=3,
        kind="temporary_rejection",
        domains=["attraction"],
        content="先别考虑迪士尼。",
        reason="当前候选筛选需要避让。",
        status="active",
        expires={"on_trip_change": True},
        created_at="2026-04-19T00:00:00",
    )

    await store.upsert_working_memory_item("u1", "s1", "trip_1", item)

    memory = await store.load_working_memory("u1", "s1", "trip_1")
    other = await store.load_working_memory("u1", "s2", "trip_1")
    assert memory.items[0].id == "wm_1"
    assert other.items == []


@pytest.mark.asyncio
async def test_working_memory_upsert_rejects_trip_mismatch_without_overwrite(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    original = WorkingMemoryItem(
        id="wm_1",
        phase=3,
        kind="temporary_rejection",
        domains=["attraction"],
        content="先别考虑迪士尼。",
        reason="当前候选筛选需要避让。",
        status="active",
        expires={"on_trip_change": True},
        created_at="2026-04-19T00:00:00",
    )
    replacement = WorkingMemoryItem(
        id="wm_2",
        phase=3,
        kind="temporary_rejection",
        domains=["attraction"],
        content="先别去环球影城。",
        reason="当前候选筛选需要避让。",
        status="active",
        expires={"on_trip_change": True},
        created_at="2026-04-19T00:00:00",
    )

    await store.upsert_working_memory_item("u1", "s1", "trip_1", original)

    with pytest.raises(ValueError):
        await store.upsert_working_memory_item("u1", "s1", "trip_2", replacement)

    memory = await store.load_working_memory("u1", "s1", "trip_1")
    assert [item.id for item in memory.items] == ["wm_1"]


@pytest.mark.asyncio
async def test_episode_slices_skip_malformed_jsonl_rows(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    slice_1 = EpisodeSlice(
        id="slice_1",
        user_id="u1",
        source_episode_id="ep_1",
        source_trip_id="trip_1",
        slice_type="pitfall",
        domains=["pace"],
        entities={"destination": "京都"},
        keywords=["坑"],
        content="上次下午安排过密。",
        applicability="仅供同类行程参考。",
        created_at="2026-04-19T00:00:00",
    )
    path = tmp_path / "users" / "u1" / "memory" / "episode_slices.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                json.dumps(slice_1.to_dict(), ensure_ascii=False),
                '{"id": "broken"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    slices = await store.list_episode_slices("u1")
    assert [item.id for item in slices] == ["slice_1"]

    slice_2 = EpisodeSlice(
        id="slice_2",
        user_id="u1",
        source_episode_id="ep_2",
        source_trip_id="trip_1",
        slice_type="pitfall",
        domains=["hotel"],
        entities={"destination": "京都"},
        keywords=["住宿"],
        content="酒店离车站太远。",
        applicability="仅供同类行程参考。",
        created_at="2026-04-19T00:00:00",
    )

    await store.append_episode_slice(slice_2)

    slices = await store.list_episode_slices("u1")
    assert [item.id for item in slices] == ["slice_1", "slice_2"]


@pytest.mark.asyncio
async def test_episode_slice_append_is_idempotent(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    slice_ = EpisodeSlice(
        id="slice_1",
        user_id="u1",
        source_episode_id="ep_1",
        source_trip_id="trip_1",
        slice_type="pitfall",
        domains=["pace"],
        entities={"destination": "京都"},
        keywords=["坑"],
        content="上次下午安排过密。",
        applicability="仅供同类行程参考。",
        created_at="2026-04-19T00:00:00",
    )

    await store.append_episode_slice(slice_)
    await store.append_episode_slice(slice_)

    slices = await store.list_episode_slices("u1")
    assert [item.id for item in slices] == ["slice_1"]
