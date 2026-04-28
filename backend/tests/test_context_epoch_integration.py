from __future__ import annotations

import pytest

from api.orchestration.session.context_segments import derive_context_segments
from storage.database import Database
from storage.message_store import MessageStore
from storage.session_store import SessionStore


@pytest.mark.asyncio
async def test_repeated_phase3_visits_after_backtrack_are_distinct_persisted_segments():
    db = Database(":memory:")
    await db.initialize()
    try:
        sessions = SessionStore(db)
        messages = MessageStore(db)
        await sessions.create("sess-segments", "user-1")
        await messages.append_batch(
            "sess-segments",
            [
                {"role": "user", "content": "去成都", "seq": 0, "history_seq": 0, "phase": 1, "phase3_step": None, "run_id": "run-1", "trip_id": "trip-a", "context_epoch": 0},
                {"role": "system", "content": "进入框架", "seq": 1, "history_seq": 1, "phase": 3, "phase3_step": "brief", "run_id": "run-1", "trip_id": "trip-a", "context_epoch": 1, "rebuild_reason": "phase_forward"},
                {"role": "tool", "content": "第一次 Phase 3 工具体", "seq": 2, "history_seq": 2, "phase": 3, "phase3_step": "skeleton", "run_id": "run-2", "trip_id": "trip-a", "context_epoch": 2, "rebuild_reason": "phase3_step_change"},
                {"role": "system", "content": "进入逐日", "seq": 3, "history_seq": 3, "phase": 5, "phase3_step": None, "run_id": "run-3", "trip_id": "trip-a", "context_epoch": 3, "rebuild_reason": "phase_forward"},
                {"role": "system", "content": "回退框架", "seq": 4, "history_seq": 4, "phase": 3, "phase3_step": "skeleton", "run_id": "run-4", "trip_id": "trip-a", "context_epoch": 4, "rebuild_reason": "backtrack"},
                {"role": "user", "content": "第二次 Phase 3", "seq": 5, "history_seq": 5, "phase": 3, "phase3_step": "skeleton", "run_id": "run-4", "trip_id": "trip-a", "context_epoch": 4},
            ],
        )

        rows = await messages.load_all("sess-segments")
        segments = derive_context_segments(rows)
    finally:
        await db.close()

    phase3_skeleton_segments = [
        segment
        for segment in segments
        if segment.phase == 3 and segment.phase3_step == "skeleton"
    ]
    assert [segment.context_epoch for segment in phase3_skeleton_segments] == [2, 4]
    assert [segment.rebuild_reason for segment in phase3_skeleton_segments] == [
        "phase3_step_change",
        "backtrack",
    ]
    assert phase3_skeleton_segments[0].end_history_seq == 2
    assert phase3_skeleton_segments[1].start_history_seq == 4
