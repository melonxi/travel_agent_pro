# Trace Events Schema Design And Migration Plan

This design is additive and backwards compatible. Existing `trace_runs`,
`trace_events`, `trace_grades`, `/api/traces/{run_id}`, and the current
TraceViewer stats path must keep working while the new recorder is introduced.

## Schema Version Policy

Database schema version for this feature is `trace_schema_version=2`.

Implementation rule:

- Keep table/column migrations idempotent through `PRAGMA table_info(...)`.
- Do not rewrite existing rows.
- New columns must be nullable unless a safe default can be assigned.
- Old rows without new fields serialize with `None` values and
  `payload_schema_version=1` at the API/model boundary.

## `trace_runs` Additions

Add these nullable columns:

| Column | Type | Purpose |
| --- | --- | --- |
| `config_hash` | TEXT | Hash of runtime config relevant to agent behavior. |
| `prompt_version` | TEXT | Human-readable prompt/control version, initially optional. |
| `model_config_json` | TEXT | Redacted provider/model/temperature/max-token config summary. |
| `tool_schema_hash` | TEXT | Hash of available tool schemas at run start. |
| `trace_schema_version` | INTEGER | Schema version used by recorder, default `2` for new rows. |

Decision:

- Add all five columns now. The cost is low and they avoid future run-level
  migrations.
- Do not make `prompt_version` mandatory until prompt versioning is formalized.

## `trace_events` Additive Columns

Add these nullable columns:

| Column | Type | Purpose |
| --- | --- | --- |
| `session_id` | TEXT | Event-scoped session id for direct event queries without joining `trace_runs`. |
| `trip_id` | TEXT | Event-scoped trip id, needed across backtrack/reset. |
| `context_epoch` | INTEGER | Runtime context epoch visible at event time. |
| `parent_event_id` | TEXT | Direct causal parent event. |
| `root_event_id` | TEXT | Root causal event for a chain. |
| `correlation_id` | TEXT | Cross-event operation id, e.g. one LLM turn, one tool call, one memory pipeline. |
| `actor` | TEXT | Producer identity: `main_agent`, `tool_engine`, `context_manager`, etc. |
| `started_at` | TEXT | Event start timestamp. |
| `ended_at` | TEXT | Event end timestamp. |
| `payload_schema_version` | INTEGER | Version of `payload_json` shape for this event. |

Compatibility:

- Existing `created_at` remains insertion timestamp.
- For old rows, API/model adapters should treat `started_at` as `created_at`,
  `ended_at` as `created_at` when missing, and `payload_schema_version` as `1`.
- `session_id`, `trip_id`, and `context_epoch` can be backfilled opportunistically
  from `trace_runs` during reads, but the DB migration does not rewrite old rows.

## `trace_artifacts` Table

Create:

```sql
CREATE TABLE IF NOT EXISTS trace_artifacts (
    artifact_id       TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES trace_runs(run_id) ON DELETE CASCADE,
    event_id          TEXT REFERENCES trace_events(event_id) ON DELETE SET NULL,
    kind              TEXT NOT NULL,
    content_type      TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    redaction_status  TEXT NOT NULL,
    storage_path      TEXT,
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);
```

Artifact kind values:

- `llm_prompt`
- `llm_response`
- `tool_arguments`
- `tool_result`
- `context_snapshot`
- `state_snapshot`
- `deliverable_draft`
- `deliverable_final`
- `phase3_candidate`
- `validation_input`

Redaction status values:

- `not_needed`
- `redacted`
- `hash_only`
- `disabled`

## Indexes

Create these indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_trace_events_session_created
ON trace_events(session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_trace_events_session_run_sequence
ON trace_events(session_id, run_id, sequence);

CREATE INDEX IF NOT EXISTS idx_trace_events_correlation
ON trace_events(run_id, correlation_id, sequence);

CREATE INDEX IF NOT EXISTS idx_trace_events_parent
ON trace_events(parent_event_id);

CREATE INDEX IF NOT EXISTS idx_trace_events_root
ON trace_events(root_event_id);

CREATE INDEX IF NOT EXISTS idx_trace_events_context_epoch
ON trace_events(session_id, context_epoch, sequence);

CREATE INDEX IF NOT EXISTS idx_trace_events_actor
ON trace_events(run_id, actor);

CREATE INDEX IF NOT EXISTS idx_trace_artifacts_run
ON trace_artifacts(run_id, created_at);

CREATE INDEX IF NOT EXISTS idx_trace_artifacts_event
ON trace_artifacts(event_id);

CREATE INDEX IF NOT EXISTS idx_trace_artifacts_hash
ON trace_artifacts(content_hash);

CREATE INDEX IF NOT EXISTS idx_trace_artifacts_kind
ON trace_artifacts(run_id, kind);
```

Existing indexes remain:

- `idx_trace_runs_session_created`
- `idx_trace_runs_trip`
- `idx_trace_events_run_sequence`
- `idx_trace_events_type`
- `idx_trace_events_tool`
- `idx_trace_grades_run`
- `idx_trace_grades_status`

## SQLite Migration Plan

Implementation outline for `Database._migrate_trace_tables()`:

1. Keep the current `CREATE TABLE IF NOT EXISTS` statements.
2. Add `trace_runs` missing columns by inspecting `PRAGMA table_info(trace_runs)`.
3. Add `trace_events` missing columns by inspecting `PRAGMA table_info(trace_events)`.
4. Create `trace_artifacts` if missing.
5. Create all indexes with `IF NOT EXISTS`.
6. Commit once from `Database.initialize()`.

Column additions:

```sql
ALTER TABLE trace_runs ADD COLUMN config_hash TEXT;
ALTER TABLE trace_runs ADD COLUMN prompt_version TEXT;
ALTER TABLE trace_runs ADD COLUMN model_config_json TEXT;
ALTER TABLE trace_runs ADD COLUMN tool_schema_hash TEXT;
ALTER TABLE trace_runs ADD COLUMN trace_schema_version INTEGER;

ALTER TABLE trace_events ADD COLUMN session_id TEXT;
ALTER TABLE trace_events ADD COLUMN trip_id TEXT;
ALTER TABLE trace_events ADD COLUMN context_epoch INTEGER;
ALTER TABLE trace_events ADD COLUMN parent_event_id TEXT;
ALTER TABLE trace_events ADD COLUMN root_event_id TEXT;
ALTER TABLE trace_events ADD COLUMN correlation_id TEXT;
ALTER TABLE trace_events ADD COLUMN actor TEXT;
ALTER TABLE trace_events ADD COLUMN started_at TEXT;
ALTER TABLE trace_events ADD COLUMN ended_at TEXT;
ALTER TABLE trace_events ADD COLUMN payload_schema_version INTEGER;
```

Do not add foreign keys through `ALTER TABLE` for parent/root fields; SQLite does
not support adding FK constraints after table creation without a table rebuild.
Use application-level validation instead.

## Event Payload Version Policy

Payload schema versions are per event family.

Rules:

- Existing stats-reconstructed events are version `1`.
- New recorder-emitted events are version `2`.
- Event payloads must include `schema_version` matching the column
  `payload_schema_version`.
- Event-specific required fields are documented in
  `docs/trace-events-evidence-requirements.md`.
- Payloads must be small, structured, and redacted.
- Large strings and sensitive bodies use artifacts.

Version compatibility:

- Readers must accept missing `payload_schema_version`.
- Graders must skip with explicit reason when required v2 fields are missing.
- Do not mutate old payload JSON to look like v2.

## Artifact Storage Policy

Default local path:

```text
backend/data/trace_artifacts/{session_id}/{run_id}/{artifact_id}.{ext}
```

Storage path rules:

- Store relative paths when possible.
- Validate `session_id`, `run_id`, and generated filenames before writing.
- Do not use user-provided strings as path segments.
- `storage_path` may be `NULL` for `hash_only` and `disabled`.

Retention:

- Keep artifact metadata as long as the run exists.
- Delete artifact files when the run is deleted or through a future cleanup job.
- Initial implementation only needs metadata plus local file writes; retention
  cleanup can be a follow-up unless storage growth blocks tests.

## Failure Behavior

Trace writes must not break chat execution.

If artifact write succeeds but event write fails:

- Keep the artifact file and metadata write attempt isolated from chat.
- If artifact metadata was already saved, it remains orphaned or event-linked
  with `event_id=NULL`.
- Log a warning with run/event/artifact ids.
- Mark the run `trace_persist_failed` if the failure occurs in the final
  persistence path.
- A later cleanup job may delete artifacts that are not linked to any event.

If event write succeeds but artifact write fails:

- Persist the event with artifact status fields set to
  `redaction_status="hash_only"` or `redaction_status="disabled"` depending on
  config.
- Include `artifact_error=true` and a redacted error code/message in payload.
- Do not roll back the event solely because the large body could not be stored.

If both event and artifact writes fail:

- Swallow the error after warning.
- Mark the run `trace_persist_failed` when a `TraceStore` run exists.

## TraceStore API Implications

New store APIs:

- `append_event(event: TraceEvent) -> None`
- `append_events(events: list[TraceEvent]) -> None`
- `save_artifact_metadata(metadata: TraceArtifact) -> None`
- `load_artifact_metadata(run_id: str, event_id: str | None = None) -> list[dict]`
- `load_events_by_session(session_id: str, limit: int | None = None) -> list[dict]`
- `load_events_by_correlation(run_id: str, correlation_id: str) -> list[dict]`

Existing APIs remain:

- `create_run`
- `update_run_summary`
- `mark_run_trace_failed`
- `replace_events`
- `save_grades`
- `load_run`
- `load_events`
- `load_grades`
- `cleanup_stale_running_runs`

`replace_events(...)` remains for the legacy stats reconstruction path during
migration. New recorder paths should append events instead of replacing them.

## Old Row Compatibility

Reader behavior for old rows:

- Missing common columns return `None`.
- Missing `payload_schema_version` is treated as `1`.
- Missing `session_id`, `trip_id`, and `context_epoch` can be filled in API
  response from the joined run row if the response builder performs a join; the
  raw store method may still return `None`.
- Missing artifacts returns an empty artifact list.

TraceViewer compatibility:

- Existing `/api/sessions/{session_id}/trace` continues using in-memory stats.
- `/api/traces/{run_id}` keeps returning `{"run": ..., "events": ..., "grades": ...}`.
- New fields are additive in event rows.

## Implementation Tests Required

Database tests:

- Fresh DB creates new columns and `trace_artifacts`.
- Legacy DB with old trace tables migrates without losing rows.
- Old trace event row without new columns can still load.
- New indexes exist.

TraceStore tests:

- `append_event` preserves sequence ordering and rejects duplicate `(run_id, sequence)` through DB constraint.
- `append_events` commits atomically.
- Artifact metadata save/load works by run and event.
- Query by session and correlation works.
- Existing `replace_events` and grader tests still pass.
