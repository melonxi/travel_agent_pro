from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from evals.trace_models import RubricResult, TraceEvent
from storage.database import Database


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_schema_version(event: TraceEvent) -> int:
    if event.payload_schema_version is not None:
        return event.payload_schema_version
    schema_version = event.payload.get("schema_version")
    return schema_version if isinstance(schema_version, int) else 1


def _event_values(event: TraceEvent) -> tuple[Any, ...]:
    return (
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
        event.session_id,
        event.trip_id,
        event.context_epoch,
        event.parent_event_id,
        event.root_event_id,
        event.correlation_id,
        event.actor,
        event.started_at,
        event.ended_at,
        _payload_schema_version(event),
    )


def _coerce_metadata(metadata: Any) -> dict[str, Any]:
    if isinstance(metadata, dict):
        return dict(metadata)
    return {
        "artifact_id": metadata.artifact_id,
        "run_id": metadata.run_id,
        "event_id": metadata.event_id,
        "kind": metadata.kind,
        "content_type": metadata.content_type,
        "content_hash": metadata.content_hash,
        "redaction_status": metadata.redaction_status,
        "storage_path": metadata.storage_path,
        "size_bytes": metadata.size_bytes,
        "created_at": metadata.created_at,
    }


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
        config_hash: str | None = None,
        prompt_version: str | None = None,
        model_config_json: str | None = None,
        tool_schema_hash: str | None = None,
        trace_schema_version: int = 2,
    ) -> None:
        now = _now_iso()
        await self._db.execute(
            """
            INSERT OR IGNORE INTO trace_runs
            (run_id, session_id, trip_id, context_epoch, config_hash,
             prompt_version, model_config_json, tool_schema_hash,
             trace_schema_version, started_at, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                session_id,
                trip_id,
                context_epoch,
                config_hash,
                prompt_version,
                model_config_json,
                tool_schema_hash,
                trace_schema_version,
                started_at,
                status,
                now,
                now,
            ),
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
                await self._insert_events(events)
            await self._db.conn.commit()
        except Exception:
            await self._db.conn.rollback()
            raise

    async def append_event(self, event: TraceEvent) -> None:
        await self.append_events([event])

    async def append_events(self, events: list[TraceEvent]) -> None:
        if not events:
            return
        try:
            await self._insert_events(events)
            await self._db.conn.commit()
        except Exception:
            await self._db.conn.rollback()
            raise

    async def save_artifact_metadata(self, metadata: Any) -> None:
        data = _coerce_metadata(metadata)
        await self._db.execute(
            """
            INSERT INTO trace_artifacts
            (artifact_id, run_id, event_id, kind, content_type, content_hash,
             redaction_status, storage_path, size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                run_id = excluded.run_id,
                event_id = excluded.event_id,
                kind = excluded.kind,
                content_type = excluded.content_type,
                content_hash = excluded.content_hash,
                redaction_status = excluded.redaction_status,
                storage_path = excluded.storage_path,
                size_bytes = excluded.size_bytes,
                created_at = excluded.created_at
            """,
            (
                data["artifact_id"],
                data["run_id"],
                data.get("event_id"),
                data["kind"],
                data["content_type"],
                data["content_hash"],
                data["redaction_status"],
                data.get("storage_path"),
                int(data.get("size_bytes") or 0),
                data.get("created_at") or _now_iso(),
            ),
        )

    async def load_artifact_metadata(
        self,
        run_id: str,
        event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if event_id is None:
            return await self._db.fetch_all(
                """
                SELECT * FROM trace_artifacts
                WHERE run_id = ?
                ORDER BY created_at ASC, artifact_id ASC
                """,
                (run_id,),
            )
        return await self._db.fetch_all(
            """
            SELECT * FROM trace_artifacts
            WHERE run_id = ? AND event_id = ?
            ORDER BY created_at ASC, artifact_id ASC
            """,
            (run_id, event_id),
        )

    async def load_events_by_session(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT * FROM trace_events
            WHERE session_id = ?
            ORDER BY created_at ASC, run_id ASC, sequence ASC
        """
        params: tuple[Any, ...] = (session_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (session_id, limit)
        return await self._db.fetch_all(sql, params)

    async def load_events_by_correlation(
        self,
        run_id: str,
        correlation_id: str,
    ) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            """
            SELECT * FROM trace_events
            WHERE run_id = ? AND correlation_id = ?
            ORDER BY sequence ASC
            """,
            (run_id, correlation_id),
        )

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

    async def _insert_events(self, events: list[TraceEvent]) -> None:
        await self._db.conn.executemany(
            """
            INSERT INTO trace_events
            (event_id, run_id, sequence, event_type, phase, phase2_step,
             iteration, tool_name, llm_provider, llm_model, status,
             duration_ms, cost_usd, payload_json, created_at,
             session_id, trip_id, context_epoch, parent_event_id, root_event_id,
             correlation_id, actor, started_at, ended_at, payload_schema_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [_event_values(event) for event in events],
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
