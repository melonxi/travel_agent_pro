from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from evals.trace_models import RubricResult, TraceEvent
from storage.database import Database


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TraceStore:
    def __init__(self, db: Database):
        self._db = db

    async def create_run(
        self,
        *,
        run_id: str,
        session_id: str,
        trip_id: str | None,
        context_epoch: int | None,
        started_at: str,
        status: str,
    ) -> None:
        now = _now_iso()
        await self._db.execute(
            """
            INSERT OR IGNORE INTO trace_runs
            (run_id, session_id, trip_id, context_epoch, started_at, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, session_id, trip_id, context_epoch, started_at, status, now, now),
        )

    async def update_run_summary(
        self,
        *,
        run_id: str,
        ended_at: str | None,
        status: str,
        final_phase: int | None,
        final_phase2_step: str | None,
        total_input_tokens: int,
        total_output_tokens: int,
        total_cost_usd: float,
        total_duration_ms: float,
    ) -> None:
        await self._db.execute(
            """
            UPDATE trace_runs
            SET ended_at = ?, status = ?, final_phase = ?, final_phase2_step = ?,
                total_input_tokens = ?, total_output_tokens = ?,
                total_cost_usd = ?, total_duration_ms = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (
                ended_at,
                status,
                final_phase,
                final_phase2_step,
                total_input_tokens,
                total_output_tokens,
                total_cost_usd,
                total_duration_ms,
                _now_iso(),
                run_id,
            ),
        )

    async def mark_run_trace_failed(self, run_id: str) -> None:
        await self._db.execute(
            "UPDATE trace_runs SET status = ?, updated_at = ? WHERE run_id = ?",
            ("trace_persist_failed", _now_iso(), run_id),
        )

    async def replace_events(self, run_id: str, events: list[TraceEvent]) -> None:
        try:
            await self._db.conn.execute(
                "DELETE FROM trace_events WHERE run_id = ?",
                (run_id,),
            )
            if events:
                await self._db.conn.executemany(
                    """
                    INSERT INTO trace_events
                    (event_id, run_id, sequence, event_type, phase, phase2_step,
                     iteration, tool_name, llm_provider, llm_model, status,
                     duration_ms, cost_usd, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            event.event_id,
                            event.run_id,
                            event.sequence,
                            event.event_type,
                            event.phase,
                            event.phase2_step,
                            event.iteration,
                            event.tool_name,
                            event.llm_provider,
                            event.llm_model,
                            event.status,
                            event.duration_ms,
                            event.cost_usd,
                            json.dumps(event.payload, ensure_ascii=False),
                            event.created_at or _now_iso(),
                        )
                        for event in events
                    ],
                )
            await self._db.conn.commit()
        except Exception:
            await self._db.conn.rollback()
            raise

    async def save_grades(self, run_id: str, grades: list[RubricResult]) -> None:
        if not grades:
            return
        now = _now_iso()
        try:
            await self._db.conn.executemany(
                """
                INSERT INTO trace_grades
                (grade_id, run_id, rubric_id, status, score, reason,
                 evidence_event_ids_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, rubric_id) DO UPDATE SET
                    status = excluded.status,
                    score = excluded.score,
                    reason = excluded.reason,
                    evidence_event_ids_json = excluded.evidence_event_ids_json,
                    created_at = excluded.created_at
                """,
                [
                    (
                        str(uuid.uuid4()),
                        run_id,
                        grade.rubric_id,
                        grade.status,
                        grade.score,
                        grade.reason,
                        json.dumps(grade.evidence_event_ids, ensure_ascii=False),
                        now,
                    )
                    for grade in grades
                ],
            )
            await self._db.conn.commit()
        except Exception:
            await self._db.conn.rollback()
            raise

    async def load_run(self, run_id: str) -> dict[str, Any] | None:
        return await self._db.fetch_one(
            "SELECT * FROM trace_runs WHERE run_id = ?",
            (run_id,),
        )

    async def load_events(self, run_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM trace_events WHERE run_id = ? ORDER BY sequence ASC",
            (run_id,),
        )

    async def load_grades(self, run_id: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM trace_grades WHERE run_id = ? ORDER BY rubric_id ASC",
            (run_id,),
        )

    async def cleanup_stale_running_runs(self, max_age_seconds: int = 86400) -> int:
        """Mark stale 'running' traces as 'crashed'.

        Processes crash without properly closing trace runs, leaving them
        stuck in 'running' status with zero tokens/cost/duration. This
        method finds runs that have been running longer than
        *max_age_seconds* and marks them as 'crashed'.
        """
        from datetime import datetime, timezone

        cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(
            seconds=max_age_seconds
        )
        cutoff_iso = cutoff.isoformat()
        await self._db.execute(
            """
            UPDATE trace_runs
            SET status = 'crashed',
                ended_at = ?,
                updated_at = ?
            WHERE status = 'running'
              AND started_at < ?
            """,
            (cutoff_iso, cutoff_iso, cutoff_iso),
        )
        cursor = await self._db.conn.execute(
            "SELECT changes()"
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
