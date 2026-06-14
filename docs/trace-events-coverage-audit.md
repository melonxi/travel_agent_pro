# Trace Events Coverage Audit

This audit tracks what runtime behavior is currently persisted into
`trace_events`, what evidence is missing, and how each gap affects future trace
grading. It is intentionally incremental: each section is filled as its matching
plan checkbox is completed.

## Audit Status

| Area | Status | Notes |
| --- | --- | --- |
| `backend/storage/database.py` trace table schema | Done | Current schema inspected on 2026-06-08. |
| `backend/storage/trace_store.py` CRUD behavior | Done | Current CRUD behavior inspected on 2026-06-08. |
| `backend/evals/trace_models.py` event type definitions | Done | Current event dataclasses and literal type set inspected. |
| `backend/api/orchestration/chat/trace_persistence.py` | Done | Stats-to-trace persistence inspected. |
| `backend/telemetry/stats.py` records and payload fields | Done | LLM/tool/memory stats records inspected. |
| `backend/api/trace.py` TraceViewer data builder | Done | Session stats TraceViewer bridge inspected. |
| Chat run creation/finalization paths | Done | `chat_routes.py`, `stream.py`, and `finalization.py` inspected. |
| Tool execution and batch execution paths | Done | `tools/engine.py`, `tool_invocation.py`, and `tool_batches.py` inspected. |
| Plan writer tools and `state_changes` sources | Done | `state/plan_writers.py`, `tools/plan_tools/*`, and telemetry helpers inspected. |
| Phase transition and quality gate hooks | Done | `phase/router.py`, `phase_transition.py`, and agent hooks inspected. |
| Validator and soft judge hooks | Done | `harness/validator.py`, `harness/judge.py`, and hook wiring inspected. |
| Memory recall pipeline telemetry | Done | `memory/turn.py`, `memory/formatter.py`, and stats mapping inspected. |
| Context build/compression/rebuild paths | Done | `context/manager.py`, `message_rebuild.py`, and context epoch callback inspected. |
| Phase 3 orchestrator and worker telemetry | Done | `phase3/orchestrator.py`, `day_worker.py`, and candidate store inspected. |
| Deliverable generation/finalization path | Done | Chat and session deliverable finalization inspected. |
| Canary and failure-analysis scripts | Done | Canary audit and failure-analysis runner inspected. |

## Database Trace Schema

Source: `backend/storage/database.py`

### Existing Tables

`Database.initialize()` creates and migrates three trace tables:

| Table | Purpose | Current status |
| --- | --- | --- |
| `trace_runs` | One row per chat run trace. | Present. |
| `trace_events` | Ordered evidence rows for a run. | Present, but event columns are still the compact legacy set. |
| `trace_grades` | Deterministic grader output keyed by run and rubric. | Present. |

### `trace_runs` Columns

Current run-level evidence:

- `run_id`
- `session_id`
- `trip_id`
- `context_epoch`
- `started_at`
- `ended_at`
- `status`
- `final_phase`
- `final_phase2_step`
- `total_input_tokens`
- `total_output_tokens`
- `total_cost_usd`
- `total_duration_ms`
- `created_at`
- `updated_at`

Coverage impact:

- Run identity and aggregate token/cost/duration are covered.
- `trip_id` and `context_epoch` exist only at run scope, not per event.
- No run-level prompt/config/tool schema hashes exist yet.
- No model configuration JSON exists yet.

### `trace_events` Columns

Current event-level evidence:

- `event_id`
- `run_id`
- `sequence`
- `event_type`
- `phase`
- `phase2_step`
- `iteration`
- `tool_name`
- `llm_provider`
- `llm_model`
- `status`
- `duration_ms`
- `cost_usd`
- `payload_json`
- `created_at`

Constraints and indexes:

- `event_id` is the primary key.
- `(run_id, sequence)` is unique.
- Existing lookup indexes cover `(run_id, sequence)`, `(run_id, event_type)`, and
  `(run_id, tool_name)`.

Coverage impact:

- Ordered event replay by run is covered.
- Event type, phase, tool, provider/model, status, duration, cost, and small
  structured payloads are covered.
- Event-scoped `session_id`, `trip_id`, and `context_epoch` are missing.
- Parent/root/correlation identifiers are missing, so causality between LLM
  output, tool call, tool result, validation, repair, and phase transition cannot
  be represented as first-class schema.
- `actor`, `started_at`, `ended_at`, and `payload_schema_version` are missing.
- There is no artifact table, so large prompt/tool/result/state bodies cannot be
  referenced through durable artifact metadata.

### `trace_grades` Columns

Current grade-level evidence:

- `grade_id`
- `run_id`
- `rubric_id`
- `status`
- `score`
- `reason`
- `evidence_event_ids_json`
- `created_at`

Coverage impact:

- Grader results can link back to event IDs.
- Grade evidence quality is limited by what `trace_events` can persist today.

### Migration Behavior

`_migrate_trace_tables()` currently creates missing trace tables and indexes, but
does not add missing columns to an existing `trace_events` table. That is enough
for the current schema because trace tables are created with the latest compact
definition, but future additive event columns will need explicit
`PRAGMA table_info(trace_events)` migration logic.

## TraceStore CRUD Behavior

Source: `backend/storage/trace_store.py`

### Existing Methods

| Method | Behavior | Coverage impact |
| --- | --- | --- |
| `create_run(...)` | Inserts a `trace_runs` row with `INSERT OR IGNORE`. | Idempotent for duplicate run IDs, but duplicate starts do not refresh run metadata. |
| `update_run_summary(...)` | Updates final status, final phase/step, aggregate tokens, cost, and duration. | Covers run summary, but does not validate that a row was updated. |
| `mark_run_trace_failed(run_id)` | Marks a run as `trace_persist_failed`. | Gives persistence failure a durable run status. |
| `replace_events(run_id, events)` | Deletes all events for `run_id`, then bulk-inserts the provided `TraceEvent` list in one transaction. | Supports current stats-to-trace backfill, but not real-time event append. |
| `save_grades(run_id, grades)` | Upserts grades by `(run_id, rubric_id)`. | Grader output is idempotent, but grade evidence is only event ID lists. |
| `load_run(run_id)` | Loads one run row. | Run lookup exists. |
| `load_events(run_id)` | Loads run events ordered by `sequence`. | Ordered replay exists for one run. |
| `load_grades(run_id)` | Loads grades ordered by rubric. | Grade lookup exists for one run. |
| `cleanup_stale_running_runs(...)` | Marks old `running` runs as `crashed`. | Handles process crash cleanup at run status level. |

### Transaction and Idempotency Notes

- `replace_events(...)` is destructive by design: it deletes all existing events
  for the run before inserting the new reconstructed list.
- `replace_events(...)` manually commits on success and rolls back on failure,
  so event replacement is atomic at the SQLite transaction level.
- `save_grades(...)` also commits/rolls back explicitly and upserts by rubric.
- `create_run(...)`, `update_run_summary(...)`, and `mark_run_trace_failed(...)`
  use `Database.execute(...)`, which commits each statement immediately.

### Missing Store Capabilities

- No `append_event(...)` or `append_events(...)` API exists.
- No artifact metadata APIs exist.
- No query by `session_id`, `trip_id`, `context_epoch`, or `correlation_id`
  exists for events.
- No store-level validation checks that every event passed to
  `replace_events(run_id, events)` has the same `run_id` as the replacement
  target.
- No parent/root/correlation fields can be persisted because the table schema and
  `TraceEvent` dataclass do not expose them yet.
- No compatibility layer exists for old vs new payload schema versions because
  event payloads are currently opaque JSON blobs.

### CRUD Coverage Matrix

| Behavior | Current source | Persisted in `trace_events` | Payload completeness | Missing evidence | Eval impact | Proposed event family |
| --- | --- | --- | --- | --- | --- | --- |
| Run creation | `TraceStore.create_run` | No event row; only `trace_runs` | N/A | `run_start` event payload, config/prompt/tool hashes | Cannot grade run start context directly from event stream | `run_start` |
| Run finalization | `TraceStore.update_run_summary` | No event row; only `trace_runs` | N/A | `run_end` event payload and final state snapshot link | Cannot tie final status to last causal event | `run_end`, `state_snapshot` |
| Event persistence | `TraceStore.replace_events` | Yes, as full replacement | Depends on upstream event builder | append semantics, parent/root/correlation ids, payload schema version | Reconstructed trace can lose moment-of-decision evidence | All event families |
| Grade persistence | `TraceStore.save_grades` | No, stored in `trace_grades` | Medium | richer evidence references beyond event IDs | Grader can cite events, but not artifacts or field-level evidence | Trace grades plus artifact refs |
| Event lookup | `TraceStore.load_events` | Yes, by run only | N/A | event lookup by session/correlation/artifact | Failure analysis cannot efficiently traverse causality | All event families |
| Artifact persistence | None | No | None | artifact metadata table and save/load APIs | Prompt/tool/result bodies cannot be audited safely | Artifact-backed events |

## Trace Event Model Definitions

Source: `backend/evals/trace_models.py`

Current `TraceEventType` only admits:

- `llm_call`
- `tool_call`
- `memory_recall`
- `memory_hit`
- `phase_transition`
- `internal_task`
- `context_compression`

Coverage impact:

- The model has a few event families that the target plan needs, but the
  persistence builder only emits `llm_call`, `tool_call`, `memory_recall`, and
  `memory_hit` today.
- `TraceEvent` has no common fields for event-scoped `session_id`, `trip_id`,
  `context_epoch`, parent/root/correlation, actor, started/ended timestamps, or
  payload schema version.
- There is no event model for `run_start`, `run_end`, `llm_output`,
  `tool_result`, `state_snapshot`, `state_diff`, `phase_gate`, `quality_gate`,
  `soft_judge`, `validation`, `context_build`, `context_rebuild`,
  `phase3_orchestrator`, `phase3_worker`, `deliverable_draft`,
  `deliverable_finalize`, or `error`.

## Stats-To-Trace Persistence

Source: `backend/api/orchestration/chat/trace_persistence.py`

Current persistence behavior:

- `ensure_trace_run_started(...)` creates a `trace_runs` row at chat/continue run
  start and stores run-scoped stats offsets.
- `persist_trace_run_safely(...)` runs in the chat stream `finally` block.
- `build_trace_events_from_stats(...)` reconstructs events from
  `SessionStats.recall_telemetry`, `SessionStats.llm_calls`,
  `SessionStats.tool_calls`, and `SessionStats.memory_hits`.
- Events are sorted by `(timestamp, priority)` and sequence is assigned only at
  persistence time.
- Persistence failures are swallowed with warnings; the run can be marked
  `trace_persist_failed`.

Coverage impact:

- Trace writes do not break chat execution, which matches the target safety rule.
- Run-scoped offsets prevent later runs from copying earlier stats.
- The trace is post-hoc reconstruction, not a moment-of-decision recorder.
- SSE-only events such as `phase_transition`, `internal_task`, and
  `context_compression` are not persisted unless a matching `SessionStats` record
  exists; today no such stats record exists for them.
- LLM output text/tool-call ids are not persisted as `llm_output`.
- Tool calls and tool results are collapsed into one `tool_call` event.

## SessionStats Coverage

Source: `backend/telemetry/stats.py`

Current records:

- `LLMCallRecord`: provider, model, input/output tokens, duration, phase,
  iteration, metadata, timestamp.
- `ToolCallRecord`: tool name, duration, status, error code, phase, previews,
  state changes, parallel group, validation errors, judge scores, suggestion,
  metadata, timestamp.
- `MemoryHitRecord`: source counts, selected ids, matched reasons, timestamp.
- `RecallTelemetryRecord`: Stage 0 decision/signals, gate result, query plan
  source, candidate count, zero-hit flag, reranker selections, reranker scores,
  dual recall and Stage 3 profile/episode summaries.

Coverage impact:

- Cost/latency aggregate evidence is covered for LLM and tools.
- Memory recall telemetry is relatively rich and survives zero-hit cases.
- Tool argument/result evidence is limited to 120-char previews.
- `state_changes` are optional and produced outside the state mutation boundary.
- Validation and soft judge evidence can attach to the latest tool record, but
  they are not independent `validation`, `quality_gate`, or `soft_judge` events.
- LLM prompt context, tool schema, tool choice, message count, context epoch,
  prompt hash, and output content/tool call ids are absent.

## TraceViewer Data Builder

Source: `backend/api/trace.py`

Current behavior:

- `/api/sessions/{session_id}/trace` builds the frontend TraceViewer view from
  live in-memory `SessionStats`.
- Each LLM call starts an iteration; tool calls before the next LLM timestamp are
  grouped into that iteration.
- Orphan tool calls are shown in a synthetic iteration.
- Compression and memory records are matched to an LLM call by timestamp window.
- Significance is derived from state changes, write tools, validation/judge
  fields, compression, and memory presence.

Coverage impact:

- The TraceViewer path is useful for current live sessions.
- It does not read `trace_events`, so it is not a persisted flight recorder view.
- Causality is timestamp-based and heuristic.
- New common fields/artifacts would need a bridge before the UI can inspect them.

## Chat Run Lifecycle

Sources:

- `backend/api/routes/chat_routes.py`
- `backend/api/orchestration/chat/stream.py`
- `backend/api/orchestration/chat/finalization.py`
- `backend/run.py`

Current behavior:

- A `RunRecord` is created for chat and continue endpoints.
- `ensure_trace_run_started(...)` is called before `AgentLoop.run(...)`.
- `run_agent_stream(...)` records usage/tool stats while forwarding SSE events.
- `finalize_agent_run(...)` saves plan/messages/session metadata and emits final
  state/done events.
- `persist_trace_run_safely(...)` runs in `finally`, after state/message fallback
  persistence.
- Context rebuild callbacks persist old runtime messages, then increment
  `current_context_epoch`.

Coverage impact:

- Run start/end state exists in `trace_runs`, but no `run_start` or `run_end`
  event exists in `trace_events`.
- Error and timeout paths update `RunRecord`, but there is no structured `error`
  event linked to the failing LLM/tool/phase context.
- Context epoch changes are persisted on messages, not trace events.
- Continued runs get their own run IDs, but no causal link to the previous
  interrupted run is stored.

## Tool Execution And Batch Execution

Sources:

- `backend/tools/engine.py`
- `backend/agent/execution/tool_invocation.py`
- `backend/agent/execution/tool_batches.py`
- `backend/api/orchestration/common/telemetry_helpers.py`

Current behavior:

- `ToolEngine.execute(...)` records OTel span events with truncated input/output.
- Tool schema required fields are prevalidated.
- `execute_tool_batch(...)` runs read tools in parallel and write tools
  sequentially.
- `parallel_group` is attached to result metadata for parallel read batches.
- Guardrail/redundant-search skips return synthetic `ToolResult`.
- Chat stream records stats when `TOOL_RESULT` chunks are serialized.

Coverage impact:

- Persisted trace has one `tool_call` event per tool result after execution.
- There is no separate pre-execution `tool_call` event and post-execution
  `tool_result` event.
- Full structured arguments/results are not persisted; only previews are.
- Tool schema hashes, argument hashes, result hashes, quality flags, and parent
  `llm_output` links are missing.
- Skipped and degraded paths may be visible through status/error/suggestion, but
  not linked to a retry/repair chain.

## State Writes And State Changes

Sources:

- `backend/state/plan_writers.py`
- `backend/tools/plan_tools/trip_basics.py`
- `backend/tools/plan_tools/phase2_tools.py`
- `backend/tools/plan_tools/daily_plans.py`
- `backend/api/orchestration/common/telemetry_helpers.py`

Current behavior:

- State mutation lives in pure writer functions on `TravelPlanState`.
- Tool wrappers validate inputs and return partial write metadata.
- `select_skeleton` and selected transport/accommodation style tools often
  return `previous_value`/`new_value`.
- Many writer tools only return `updated_field`, counts, or coverage metadata.
- `_plan_writer_state_changes(...)` builds optional diffs from tool arguments and
  result payloads after the fact.

Coverage impact:

- Some state changes appear in `tool_call.payload.state_changes`.
- There is no stable state hash before/after writer execution.
- There is no guaranteed field-level diff for every writer.
- Backtrack and downstream clearing behavior is not represented as a first-class
  state diff.
- `decision_events`/`lesson_events` are part of plan state, but not independently
  persisted as trace evidence.

## Phase Transition And Quality Gate

Sources:

- `backend/phase/router.py`
- `backend/agent/execution/phase_transition.py`
- `backend/api/orchestration/agent/hooks.py`

Current behavior:

- `PhaseRouter.infer_phase(...)` derives phase/Phase 2 step from
  `TravelPlanState`.
- `check_and_apply_transition(...)` invokes `before_phase_transition` gate hooks.
- Quality gate emits `InternalTask(kind="quality_gate")` into the chat stream.
- `AgentLoop._handle_phase_transition(...)` emits an SSE `phase_transition`.
- OTel span `phase.transition` records a small plan snapshot.

Coverage impact:

- Persisted `trace_events` do not currently include `phase_gate` or
  `phase_transition` rows.
- Gate decisions, blockers, warnings, retry counts, and feedback injection are
  not queryable from persisted trace.
- Phase 2 step changes rebuild context without a persisted transition event.
- Transition causality is not linked to the writer tool or gate that caused it.

## Validation And Soft Judge

Sources:

- `backend/harness/validator.py`
- `backend/harness/judge.py`
- `backend/api/orchestration/agent/hooks.py`

Current behavior:

- `validate_incremental(...)`, `validate_lock_budget(...)`, and deliverable gate
  checks run after relevant tool calls.
- Validation feedback is staged in pending system notes and can attach to the
  next `ToolCallRecord`.
- Soft judge runs after `save_day_plan`, `replace_all_day_plans`, and
  `generate_summary`.
- Soft judge emits `InternalTask(kind="soft_judge")`; score summaries can attach
  to the latest tool stats record.

Coverage impact:

- Validator and judge details are not independent persisted events.
- Rule ids, affected fields/days/tools, severity, action items, and blocking vs
  advisory decisions are incomplete or only in text.
- Deliverable gating can block freezing, but that decision is not linked to
  `generate_summary` in persisted trace.

## Memory Recall Telemetry

Sources:

- `backend/api/orchestration/memory/turn.py`
- `backend/memory/formatter.py`
- `backend/api/orchestration/common/telemetry_helpers.py`

Current behavior:

- Memory recall emits chat `internal_task` start/end and a `memory_recall` SSE
  event.
- `RecallTelemetryRecord` is appended for every recall turn.
- `MemoryHitRecord` is appended only when selected memory sources are non-empty.
- Persisted trace reconstructs `memory_recall` and `memory_hit` events from
  stats.

Coverage impact:

- Stage 0/gate/query/reranker evidence is relatively complete in payloads.
- No correlation id ties Stage 0 through Stage 4 to the context build that used
  the memory.
- Latest user message hash, previous user message hash/count, and injected
  context hash are missing.
- `memory_hit` cannot be causally linked to a following `context_build`.

## Context Build, Compression, And Rebuild

Sources:

- `backend/context/manager.py`
- `backend/agent/execution/llm_turn.py`
- `backend/agent/execution/message_rebuild.py`
- `backend/api/orchestration/chat/finalization.py`

Current behavior:

- Chat routes build static system and turn context messages before each run.
- `before_llm_call` hook flushes pending notes and compacts messages when needed.
- Compression details are appended to `session["compression_events"]` and emitted
  as SSE `context_compression`.
- Phase/step/backtrack rebuilds call the context rebuild callback, persist old
  messages, and increment `current_context_epoch`.

Coverage impact:

- `context_build` is not persisted.
- `context_compression` is visible in session trace UI but not in persisted
  `trace_events`.
- `context_rebuild` is visible through message metadata, not event rows.
- LLM-call trace cannot prove which prompt, tools, memory ids, state snapshot, or
  context epoch the model saw.

## Phase 3 Orchestrator And Workers

Sources:

- `backend/agent/phase3/orchestrator.py`
- `backend/agent/phase3/day_worker.py`
- `backend/agent/phase3/candidate_store.py`

Current behavior:

- Orchestrator emits frontend progress through `AGENT_STATUS`.
- Day workers record their LLM/tool calls into the shared `SessionStats` with
  metadata such as `scope=phase3_worker`, day, attempt, iteration, and
  worker_run_id.
- Candidate DayPlans are written to run-scoped JSON artifacts under the Phase 3
  candidate store root.
- Orchestrator validates global issues and hands final day plans back to
  AgentLoop for a standard `replace_all_day_plans` commit.

Coverage impact:

- Worker LLM/tool records can appear in persisted trace as generic `llm_call` and
  `tool_call` events.
- Orchestrator start/end, task compilation, candidate artifact writes, global
  validation issues, retry dispatch, and final handoff are not first-class
  persisted events.
- Candidate artifacts are not represented in `trace_artifacts`.
- Worker events rely on payload metadata instead of actor/correlation fields.

## Deliverable Finalization

Sources:

- `backend/api/orchestration/chat/deliverables.py`
- `backend/api/orchestration/session/deliverables.py`
- `backend/api/orchestration/agent/hooks.py`

Current behavior:

- `generate_summary` result is held in `_pending_phase4_deliverables`.
- Soft judge or force-finalization decides whether deliverables can be frozen.
- `persist_phase4_deliverables(...)` writes `travel_plan.md` and `checklist.md`,
  then updates `plan.deliverables`.
- Failed file writes clear deliverables and re-raise.

Coverage impact:

- There is no `deliverable_draft` event for generated markdown.
- There is no `deliverable_finalize` event for frozen files.
- Draft/final artifact hashes and source state hash are missing.
- Finalization is not causally linked to `generate_summary` or quality gate
  evidence in persisted trace.

## Canary And Failure Analysis Consumers

Sources:

- `backend/evals/trace_grader.py`
- `backend/evals/canary_audit.py`
- `scripts/run-full-phase-canary.py`
- `scripts/failure-analysis/run_and_analyze.py`
- `backend/api/routes/artifact_routes.py`

Current behavior:

- `/api/traces/{run_id}` returns persisted run/events/grades.
- `/api/traces/{run_id}/grade` converts event rows to `TraceEvent`, runs
  deterministic rubrics, and saves `trace_grades`.
- The full-phase canary fetches persisted trace events and grades each run.
- `canary_audit.py` audits persisted tool events instead of SSE tool events.
- Failure analysis still extracts tool calls from stored messages and response
  snippets, not persisted trace.

Coverage impact:

- Canary already depends on persisted trace, so missing event families directly
  limit canary diagnosis.
- Current trace grader can check broad workflow facts, but cannot inspect exact
  arguments/results, prompt context, state hashes, quality gate causality, or
  artifact evidence.
- Failure reports cannot cite trace event ids or root-cause event chains yet.

### Schema Coverage Matrix

| Behavior | Current source | Persisted in `trace_events` | Payload completeness | Missing evidence | Eval impact | Proposed event family |
| --- | --- | --- | --- | --- | --- | --- |
| Run lifecycle identity | `trace_runs` | Partially, through `run_id` only | Low at event level | event `session_id`, `trip_id`, `context_epoch` | Cross-run/session grading needs joins and can lose context per event | `run_start`, `run_end` |
| Ordered event replay | `trace_events.sequence` | Yes | Medium | causal parent/root/correlation ids | Can replay order but cannot prove which event caused which outcome | All event families |
| LLM call metadata | `trace_events` columns plus payload | Partially | Low until CRUD/payload audit is complete | prompt hash/artifact, tool schema hash, tool choice, message counts | Cannot safely grade prompt/context/tool visibility | `llm_call`, `llm_output` |
| Tool execution metadata | `trace_events` columns plus payload | Partially | Low until CRUD/payload audit is complete | full redacted args/results or artifact refs, argument/result hashes, parent LLM output | Cannot grade tool argument correctness or result handling | `tool_call`, `tool_result` |
| State mutation evidence | `payload_json` only if producers include it | Not guaranteed by schema | Low | before/after hashes, stable state diff columns/artifact refs | Cannot reliably grade mutation correctness | `state_snapshot`, `state_diff` |
| Artifact-backed evidence | None | No | None | `trace_artifacts` table and artifact ids in payloads | Large prompts/results cannot be audited safely | Artifact metadata attached to all large-body events |

## Overall Coverage Matrix

| Behavior | Current source | Persisted in `trace_events` | Payload completeness | Missing evidence | Eval impact | Proposed event family |
| --- | --- | --- | --- | --- | --- | --- |
| Run start | `RunRecord`, `ensure_trace_run_started`, `trace_runs` | No event row | N/A | run config, prompt/tool schema hashes, initial state hash | Cannot grade starting context or compare run setup regressions | `run_start`, `state_snapshot` |
| Run end | `finalize_agent_run`, `persist_trace_run_safely`, `trace_runs` | No event row | N/A | final state hash, terminal error/continuation link | Cannot tie final status to causal event chain | `run_end`, `state_snapshot`, `error` |
| LLM call input | `SessionStats.LLMCallRecord`, `run_llm_turn` | Partially as `llm_call` | Low | prompt hash/artifact, tool schema hash, tool choice, message count, memory ids, context epoch | Cannot grade context pollution or prompt/tool visibility | `llm_call`, `context_build` |
| LLM output | `run_llm_turn` text/tool stream | No | None | output text hash/preview, emitted tool call ids/names, provider state link | Cannot link model output to tool execution | `llm_output` |
| Tool execution | `ToolEngine`, `execute_tool_batch`, tool result stats | Collapsed into `tool_call` after result | Low | separate call/result rows, full redacted args/results, hashes, schema version, parent LLM output | Cannot grade exact argument correctness or empty-result misuse | `tool_call`, `tool_result` |
| State mutation | plan writer tools and telemetry helpers | Sometimes in `tool_call.payload.state_changes` | Low/partial | before/after state hashes, guaranteed diffs, backtrack clearing diff | Cannot reliably grade mutation correctness | `state_snapshot`, `state_diff` |
| Phase gate | `PhaseRouter.check_and_apply_transition`, quality gate hook | No | None | gate inputs, blockers, scores, retries, allowed/blocked decision | Cannot explain blocked or allowed transitions | `phase_gate`, `quality_gate` |
| Phase transition | SSE `phase_transition`, OTel span | No | None | from/to step, reason, parent tool/gate link, context epoch change | Cannot reconstruct phase causality from persisted trace | `phase_transition`, `context_rebuild` |
| Validation | validator hook and pending system notes | Only optional strings on latest tool stats | Low | rule id, severity, field/day/tool, pass/fail/warn, parent tool result | Cannot grade validator behavior or repair loops | `validation` |
| Soft judge | hook internal task and latest tool stats | Only optional score summary on latest tool stats | Low/medium | prompt/output hash, suggestions, blocking/advisory decision, parent tool link | Cannot prove quality feedback caused repair/freeze | `soft_judge`, `quality_gate` |
| Memory recall | memory turn telemetry and stats | Yes: `memory_recall`, sometimes `memory_hit` | Medium/high | correlation id, user message hashes, injected context hash, link to context build | Can grade false skip/recall partly, but not injection relevance | `memory_recall`, `memory_hit`, `context_build` |
| Context build | chat route/context manager/message rebuild | No | None | prompt/system hash, state hash, tool names/schema hash, memory ids, token estimate | Cannot prove what LLM saw | `context_build` |
| Context compression | `compression_events` and SSE | No persisted run event | Medium in session UI only | compacted summary hash/artifact, before/after message hashes, parent LLM call | Cannot explain context loss regressions from persisted trace | `context_compression` |
| Context rebuild | context epoch callback and message metadata | No event row | Low via messages | from/to epoch, reason, from/to phase/step, old/new prompt hashes | Cannot query rebuilds from trace evidence | `context_rebuild` |
| Internal tasks | `InternalTask` SSE | No | Medium in stream only | task lifecycle events in persisted trace | Cannot audit hidden work after run | `internal_task` |
| Phase 3 orchestrator | orchestrator progress/status and logs | No | None | task compile hash, worker config, validation issues, retries, fallback | Parallel Phase 3 remains opaque except worker stats | `phase3_orchestrator`, `validation` |
| Phase 3 worker | worker stats metadata | Partially as generic LLM/tool events | Medium for stats, low for artifacts | actor/correlation ids, worker start/end, candidate artifact ids | Hard to prove candidate submitted, failed, retried, or finalized | `phase3_worker`, `tool_call`, `tool_result` |
| Deliverable draft/finalize | generate_summary result and deliverable writer | Only `generate_summary` tool preview and final plan state | Low | draft/final artifact hashes, source state hash, freeze decision | Cannot prove exact final files came from exact state | `deliverable_draft`, `deliverable_finalize` |
| Canary/failure evidence | trace grader/canary audit/failure scripts | Canary yes, failure-analysis no | Limited by events | failure event ids, root-cause chain, artifact refs | Reports cannot diagnose exact root cause | trace grades plus all above |

## Gap Ranking By Eval Impact

1. LLM/context evidence is missing: no prompt/context/tool schema hashes, memory
   injection ids, tool choice, or context epoch per event. This blocks prompt,
   context pollution, and memory-injection graders.
2. Tool call/result evidence is only previews and lacks separate call/result
   causality. This blocks tool argument correctness, empty-result misuse, and
   repeated argument failure graders.
3. State mutation evidence lacks guaranteed before/after state hashes and diffs.
   This blocks reliable state mutation correctness and backtrack clearing checks.
4. Phase gate/transition/quality decisions are not persisted as event rows. This
   blocks phase-transition causality, blocked-gate, retry, and repair grading.
5. Deliverable finalization has no draft/final artifact hashes or source state
   hash. This blocks proof that final markdown came from the final state.
6. Phase 3 orchestrator artifacts and validation are outside trace metadata.
   Worker stats help, but orchestrator decisions, candidate artifacts, retries,
   and final handoff remain weak evidence.
7. Memory recall telemetry is the strongest current area, but it still lacks
   correlation to context build and user/context hashes.
8. API/UI consumers are split between in-memory session trace and persisted run
   trace, so new flight-recorder fields need a compatibility bridge.
