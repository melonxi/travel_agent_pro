from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextSegment:
    session_id: str
    context_epoch: int
    phase: int | None
    phase3_step: str | None
    trip_id: str | None
    run_ids: tuple[str, ...]
    start_history_seq: int
    end_history_seq: int
    message_count: int
    rebuild_reason: str | None


def derive_context_segments(rows: list[dict[str, Any]]) -> list[ContextSegment]:
    normalized_rows = [
        row
        for row in rows
        if row.get("context_epoch") is not None and row.get("history_seq") is not None
    ]
    normalized_rows.sort(
        key=lambda row: (
            str(row.get("session_id") or ""),
            int(row["context_epoch"]),
            int(row["history_seq"]),
        )
    )

    segments: list[ContextSegment] = []
    current_key: tuple[str, int] | None = None
    current_rows: list[dict[str, Any]] = []

    def flush_current() -> None:
        if not current_rows:
            return
        first = current_rows[0]
        tagged = next(
            (row for row in current_rows if row.get("phase") is not None),
            first,
        )
        run_ids = tuple(
            dict.fromkeys(
                str(row["run_id"])
                for row in current_rows
                if row.get("run_id") is not None
            )
        )
        history_seqs = [int(row["history_seq"]) for row in current_rows]
        rebuild_reason = next(
            (
                str(row["rebuild_reason"])
                for row in current_rows
                if row.get("rebuild_reason")
            ),
            None,
        )
        segments.append(
            ContextSegment(
                session_id=str(first["session_id"]),
                context_epoch=int(first["context_epoch"]),
                phase=tagged.get("phase"),
                phase3_step=tagged.get("phase3_step"),
                trip_id=tagged.get("trip_id"),
                run_ids=run_ids,
                start_history_seq=min(history_seqs),
                end_history_seq=max(history_seqs),
                message_count=len(current_rows),
                rebuild_reason=rebuild_reason,
            )
        )

    for row in normalized_rows:
        key = (str(row["session_id"]), int(row["context_epoch"]))
        if current_key is not None and key != current_key:
            flush_current()
            current_rows = []
        current_key = key
        current_rows.append(row)
    flush_current()

    return segments
