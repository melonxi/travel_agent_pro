from __future__ import annotations

from api.orchestration.session.context_segments import (
    ContextSegment,
    derive_context_segments,
)


def test_derive_context_segments_groups_by_context_epoch():
    rows = [
        {
            "session_id": "sess-1",
            "context_epoch": 0,
            "phase": 1,
            "phase3_step": None,
            "trip_id": "trip-a",
            "run_id": "run-1",
            "history_seq": 0,
            "rebuild_reason": None,
        },
        {
            "session_id": "sess-1",
            "context_epoch": 1,
            "phase": 3,
            "phase3_step": "brief",
            "trip_id": "trip-a",
            "run_id": "run-1",
            "history_seq": 1,
            "rebuild_reason": "phase_forward",
        },
        {
            "session_id": "sess-1",
            "context_epoch": 1,
            "phase": 3,
            "phase3_step": "brief",
            "trip_id": "trip-a",
            "run_id": "run-2",
            "history_seq": 2,
            "rebuild_reason": None,
        },
    ]

    segments = derive_context_segments(rows)

    assert segments == [
        ContextSegment(
            session_id="sess-1",
            context_epoch=0,
            phase=1,
            phase3_step=None,
            trip_id="trip-a",
            run_ids=("run-1",),
            start_history_seq=0,
            end_history_seq=0,
            message_count=1,
            rebuild_reason=None,
        ),
        ContextSegment(
            session_id="sess-1",
            context_epoch=1,
            phase=3,
            phase3_step="brief",
            trip_id="trip-a",
            run_ids=("run-1", "run-2"),
            start_history_seq=1,
            end_history_seq=2,
            message_count=2,
            rebuild_reason="phase_forward",
        ),
    ]


def test_repeated_phase3_visits_after_backtrack_produce_distinct_segments():
    rows = [
        {"session_id": "sess-1", "context_epoch": 2, "phase": 3, "phase3_step": "skeleton", "trip_id": "trip-a", "run_id": "run-3", "history_seq": 20, "rebuild_reason": "phase3_step_change"},
        {"session_id": "sess-1", "context_epoch": 3, "phase": 5, "phase3_step": None, "trip_id": "trip-a", "run_id": "run-4", "history_seq": 30, "rebuild_reason": "phase_forward"},
        {"session_id": "sess-1", "context_epoch": 4, "phase": 3, "phase3_step": "skeleton", "trip_id": "trip-a", "run_id": "run-5", "history_seq": 40, "rebuild_reason": "backtrack"},
    ]

    segments = derive_context_segments(rows)

    phase3_segments = [segment for segment in segments if segment.phase == 3]
    assert [segment.context_epoch for segment in phase3_segments] == [2, 4]
    assert [segment.rebuild_reason for segment in phase3_segments] == [
        "phase3_step_change",
        "backtrack",
    ]


def test_legacy_rows_without_context_epoch_do_not_break_new_segments():
    rows = [
        {"session_id": "sess-1", "context_epoch": None, "phase": None, "phase3_step": None, "trip_id": None, "run_id": None, "history_seq": None, "rebuild_reason": None},
        {"session_id": "sess-1", "context_epoch": 0, "phase": 1, "phase3_step": None, "trip_id": "trip-a", "run_id": "run-1", "history_seq": 0, "rebuild_reason": None},
    ]

    segments = derive_context_segments(rows)

    assert len(segments) == 1
    assert segments[0].context_epoch == 0
    assert segments[0].message_count == 1
