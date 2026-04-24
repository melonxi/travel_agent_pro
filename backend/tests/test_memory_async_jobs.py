from __future__ import annotations

import asyncio

import pytest

from memory.async_jobs import (
    MemoryJobScheduler,
    MemoryJobSnapshot,
    build_extraction_user_window,
    build_gate_user_window,
)


def test_build_gate_user_window_uses_recent_user_messages_and_char_cap():
    window = build_gate_user_window(
        [
            "第一条：我想先看看东京。",
            "第二条：我不吃辣。",
            "第三条：不要住青旅。",
            "第四条：节奏慢一点。",
        ],
        max_messages=3,
        max_chars=24,
    )

    assert window == [
        "第三条：不要住青旅。",
        "第四条：节奏慢一点。",
    ]


def test_build_gate_user_window_preserves_latest_message_when_over_char_cap():
    latest = "最后一条：" + "我想去有好吃的地方玩，不能吃辣，也不喜欢鱼。" * 80

    window = build_gate_user_window(
        [
            "第一条：我想先看看东京。",
            "第二条：我不吃辣。",
            latest,
        ],
        max_messages=3,
        max_chars=120,
    )

    assert window == [latest]


def test_build_gate_user_window_drops_older_messages_as_whole_units():
    window = build_gate_user_window(
        [
            "第一条：" + "A" * 20,
            "第二条：" + "B" * 20,
            "第三条：" + "C" * 20,
        ],
        max_messages=3,
        max_chars=55,
    )

    assert window == [
        "第二条：" + "B" * 20,
        "第三条：" + "C" * 20,
    ]


def test_build_extraction_user_window_keeps_legacy_tail_clipping():
    latest = "最后一条：" + "这是一条很长的待提取信息。" * 20

    window = build_extraction_user_window(
        [latest],
        last_consumed_user_count=0,
        submitted_user_count=1,
        max_messages=8,
        max_chars=30,
    )

    assert window == [latest[-30:]]


def test_build_extraction_user_window_uses_incremental_messages_since_last_consumed():
    window = build_extraction_user_window(
        [
            "第一条：我不吃辣。",
            "第二条：不要住青旅。",
            "第三条：节奏慢一点。",
            "第四条：这次预算三万。",
        ],
        last_consumed_user_count=1,
        submitted_user_count=4,
        max_messages=8,
        max_chars=100,
    )

    assert window == [
        "第二条：不要住青旅。",
        "第三条：节奏慢一点。",
        "第四条：这次预算三万。",
    ]


@pytest.mark.asyncio
async def test_memory_job_scheduler_coalesces_pending_snapshots_to_latest():
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    started_turn_ids: list[str] = []

    async def runner(snapshot: MemoryJobSnapshot) -> None:
        started_turn_ids.append(snapshot.turn_id)
        if snapshot.turn_id == "turn-1":
            first_started.set()
            await release_first.wait()

    scheduler = MemoryJobScheduler(runner=runner)

    scheduler.submit(
        MemoryJobSnapshot(
            session_id="s1",
            user_id="u1",
            turn_id="turn-1",
            user_messages=["第一条"],
            submitted_user_count=1,
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)

    scheduler.submit(
        MemoryJobSnapshot(
            session_id="s1",
            user_id="u1",
            turn_id="turn-2",
            user_messages=["第一条", "第二条"],
            submitted_user_count=2,
        )
    )
    scheduler.submit(
        MemoryJobSnapshot(
            session_id="s1",
            user_id="u1",
            turn_id="turn-3",
            user_messages=["第一条", "第二条", "第三条"],
            submitted_user_count=3,
        )
    )

    assert scheduler.pending_snapshot is not None
    assert scheduler.pending_snapshot.turn_id == "turn-3"

    release_first.set()
    await asyncio.wait_for(scheduler.wait_for_idle(), timeout=1)

    assert started_turn_ids == ["turn-1", "turn-3"]
