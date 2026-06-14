# Trace Events Evidence Requirements

This document defines what future deterministic graders must be able to answer
from persisted trace evidence. It is the contract for schema and recorder work:
do not add a grader unless its required evidence is available or the grader can
return a clear `skip`.

## Evidence Priority

| Priority | Meaning |
| --- | --- |
| Required | Must be present in `trace_events` payload/common fields for deterministic grading. |
| Optional | Useful for debugging or better messages, but not required for pass/fail. |
| Artifact-only | Large or sensitive body stored outside `payload_json`; `trace_events` stores hash, preview, artifact id, and redaction status. |

Common required fields for behavior-affecting events:

- `run_id`
- `session_id`
- `trip_id`
- `context_epoch`
- `sequence`
- `event_type`
- `phase`
- `phase2_step`
- `payload_schema_version`
- `actor`
- `status`
- `started_at`
- `ended_at`
- `correlation_id`
- `parent_event_id` where causality matters

## Phase 1 Destination Convergence

| Eval question | Required evidence | Optional evidence | Artifact-only evidence |
| --- | --- | --- | --- |
| Did the agent research before recommending destinations? | Ordered `tool_call`/`tool_result` events for search/read tools before destination recommendation or `update_trip_basics.destination`; tool names, statuses, argument hashes, result quality flags. | Search result source counts and preview. | Full redacted search results. |
| Did Phase 1 avoid downstream planning before destination was selected? | `tool_call` events with phase and tool side effect; no Phase 2/3 writer tools before destination state diff. | Available tool list per LLM call. | Prompt artifact showing Phase 1 tool instructions. |
| Did the agent write the confirmed destination into state? | `state_diff` linked to `update_trip_basics` with `field=destination`, before/after hashes, changed value preview. | User message hash that authorized the destination. | Full state snapshot before/after. |
| Did the run transition to Phase 2 for the right reason? | `phase_gate` and `phase_transition` with from/to phase, allowed decision, reason, parent state diff id. | Gate input summary. | Gate input state snapshot. |

## Phase 2 Brief, Candidate, Skeleton, Lock

| Eval question | Required evidence | Optional evidence | Artifact-only evidence |
| --- | --- | --- | --- |
| Did brief capture user facts without inventing defaults? | `tool_call`/`tool_result` and `state_diff` for `set_trip_brief`, `update_trip_basics`, `add_preferences`, `add_constraints`; user message hash; changed fields. | Extracted fact confidence/source text snippets. | Full redacted prompt and state snapshots. |
| Did candidate pool/shortlist follow search evidence? | Ordered search `tool_result` events before `set_candidate_pool`/`set_shortlist`; result quality flags; parent LLM output link. | Candidate source urls/domains. | Full search/read results. |
| Did skeleton generation use real strategy evidence before writing plans? | Search/read events before `set_skeleton_plans`; `state_diff` for `skeleton_plans`; skeleton day count and POI uniqueness summary. | Mobility/pace metadata. | Full skeleton payload and search evidence. |
| Did skeleton days match trip dates? | `state_snapshot` or final state hash with `dates.total_days`; `state_diff` for selected skeleton; selected skeleton day count. | Phase gate blocker/warning if mismatch. | Full selected skeleton artifact. |
| Did lock require explicit user authorization? | User message hash/content preview classified as explicit authorization; `tool_call`/`state_diff` for `select_transport`, `set_accommodation`, `select_skeleton`; parent LLM output. | Consent classifier result. | Full redacted recent message window. |
| Did lock tools use valid argument shape? | Full redacted arguments or argument artifact hash for transport/accommodation writer calls; schema hash/version; validation status. | Normalized argument summary. | Full redacted tool arguments. |
| Did Phase 2 step transitions happen in order? | `phase_transition` or `context_rebuild` events with `from_step`, `to_step`, reason, parent state diff. | Tool availability snapshot per step. | Prompt/context artifacts for each step. |

## Phase 3 Serial Mode

| Eval question | Required evidence | Optional evidence | Artifact-only evidence |
| --- | --- | --- | --- |
| Did the agent generate exactly the required day count? | `state_diff` for `save_day_plan`/`replace_all_day_plans`; before/after state hashes; day coverage summary. | Missing/extra day lists from tool result. | Full daily plan state snapshot. |
| Did every day contain activities with valid fields? | `validation` events for daily plan schema or tool result validation; activity count per day. | Field-level invalid item paths. | Full daily plan payload. |
| Did route/weather/availability evidence support uncertain facts? | Relevant `tool_call`/`tool_result` pairs; result quality flags; state diff fields carrying uncertainty markers. | External service error/fallback metadata. | Full route/weather/availability results. |
| Did duplicate POIs get prevented or repaired? | `validation` or `state_diff` with duplicate check result; repair `tool_call` linked to failing validation. | Normalized POI keys. | Full day plans before/after repair. |
| Did repair happen after validator warnings? | `validation` fail/warn event, following `llm_call`, repair `tool_call`, and final pass event linked by correlation id. | Feedback text injected into context. | Prompt artifacts showing injected feedback. |

## Phase 3 Parallel Orchestrator-Workers Mode

| Eval question | Required evidence | Optional evidence | Artifact-only evidence |
| --- | --- | --- | --- |
| Did orchestrator start with the right worker config? | `phase3_orchestrator` start event with day task count, max workers, fallback config, selected skeleton hash. | Worker timeout, artifact root, shared prefix hash. | Compiled day tasks artifact. |
| Did every worker run and report outcome? | `phase3_worker` start/result events per day/attempt with worker actor, correlation id, status, error code, iterations. | Progress snapshots. | Worker prompt/context artifact. |
| Did worker LLM/tool events remain visible in main run trace? | Worker `llm_call`, `llm_output`, `tool_call`, `tool_result` events with `actor=phase3_worker`, day, attempt, worker correlation id. | Worker run id. | Worker prompt/tool result artifacts. |
| Did candidates get submitted but only finalized through standard writer path? | `phase3_worker` candidate submission event with artifact id; later main `tool_call/tool_result` for `replace_all_day_plans`; `state_diff` linked to commit. | Candidate count by day. | Candidate DayPlan artifacts. |
| Did orchestrator global validation run before final handoff? | `validation` event with issue types: duplicate, budget, coverage, route, semantic, pace, transport; parent orchestrator event. | Redispatch hints. | Validation input artifact. |
| Did fallback/retry decisions have evidence? | Worker failure/result event, orchestrator retry/fallback event, final action. | Failure rate. | Worker error transcript artifacts. |

## Phase 4 Deliverables

| Eval question | Required evidence | Optional evidence | Artifact-only evidence |
| --- | --- | --- | --- |
| Did `generate_summary` produce a draft? | `tool_call/tool_result` for `generate_summary`; `deliverable_draft` with source state hash and markdown hashes. | Checklist/travel plan section counts. | Full redacted draft markdown. |
| Did quality/validation pass before finalization? | `validation`, `soft_judge`, or `quality_gate` events linked to `generate_summary`; final action approved/blocked. | Suggestions and scores. | Judge prompt/response artifact. |
| Did frozen files match the approved draft? | `deliverable_finalize` with final file paths, content hashes, source draft event id, final state hash. | File sizes. | Full final markdown artifacts. |
| Did uncertain weather/route estimates remain visible? | `validation` event for estimation/uncertainty visibility; final markdown hash linked to evidence. | Marker counts. | Final markdown artifact. |

## Memory Recall

| Eval question | Required evidence | Optional evidence | Artifact-only evidence |
| --- | --- | --- | --- |
| Did Stage 0 force/skip/undecided correctly? | `memory_recall` with latest user message hash, Stage 0 signals, matched rule, decision, reason. | Matched text snippets. | Full redacted recall input window. |
| Did LLM gate run only when needed? | `memory_recall` with gate attempted flag, gate result, fallback source, error path. | Gate latency/cost. | Gate prompt/response artifact. |
| Did query plan choose correct sources? | `memory_recall` with query plan source, source targets, buckets/domains/destination, fallback. | Query top_k per source. | Full retrieval plan artifact. |
| Did candidate generation and reranking behave correctly? | Candidate counts by source/lane, selected ids, per-item score summary, final reason, fallback. | Pairwise similarity metrics. | Full candidate/evidence sidecar artifact. |
| Did selected memory get injected into context? | `memory_hit` with selected ids and a following `context_build` listing injected memory ids, linked by correlation id. | Injected context preview. | Full memory context artifact. |

## Context Compression And Rebuild

| Eval question | Required evidence | Optional evidence | Artifact-only evidence |
| --- | --- | --- | --- |
| What context did each LLM call see? | `context_build` linked to `llm_call`; system prompt hash, phase prompt hash, state hash, memory ids, available tool names/schema hash, message count, context epoch. | Token estimate and compaction mode. | Full redacted prompt/messages artifact. |
| Did compression run and preserve must-keep messages? | `context_compression` with reason, mode, input/output message counts, must-keep count, summary hash. | Usage ratio before/after. | Compacted summary artifact and before/after message hashes. |
| Did phase/step/backtrack rebuild create a new epoch? | `context_rebuild` with from/to epoch, from/to phase/step, rebuild reason, parent event id. | History seq ranges. | Old/new runtime context artifacts. |
| Did pending validation/quality feedback get injected? | `context_build` with injected app_event/runtime_notice ids linked to validation/quality events. | Feedback preview. | Prompt artifact. |

## Tool Argument Correctness

| Eval question | Required evidence | Optional evidence | Artifact-only evidence |
| --- | --- | --- | --- |
| Were tool arguments grounded in user constraints/current state? | Full redacted arguments or argument artifact id/hash; parent LLM output; user/context/state hashes; schema hash. | Extracted constraint ids used. | Full prompt/context and arguments artifact. |
| Were invalid arguments caught before execution? | `tool_call` status/prevalidation result or `tool_result` error code, schema version, missing/invalid fields. | Suggestion text. | Full redacted invalid arguments. |
| Did repeated argument failures trigger repair or stop? | Correlated sequence of failed `tool_result` events with argument hashes, repair `llm_call`, final success/skip. | Retry count. | Prompt artifacts with repair feedback. |
| Did search/tool budget stay bounded? | Tool event count by run/phase/correlation, duration, status/error rate. | Tool category budgets. | None. |

## State Mutation Correctness

| Eval question | Required evidence | Optional evidence | Artifact-only evidence |
| --- | --- | --- | --- |
| Did only writer tools mutate state? | `state_diff` parent linked to writer `tool_result`; no state diffs for read tools. | Tool side effect classification. | Full state snapshots. |
| Did field-level changes match tool arguments/results? | `state_diff` changed fields, before/after hashes, value previews; parent tool argument/result hashes. | Normalized writer output. | Full before/after state artifacts. |
| Did no-op/failed writer calls avoid state mutation? | Failed/skipped `tool_result` and absent `state_diff` or explicit no-op diff. | Error/suggestion. | State snapshot artifact if needed. |
| Did backtrack clear downstream fields and rotate trip when required? | `state_diff` for cleared fields, `phase_transition` to earlier phase, trip id before/after, rebuild reason. | Backtrack history entry. | Full before/after state artifacts. |

## Retry And Repair Behavior

| Eval question | Required evidence | Optional evidence | Artifact-only evidence |
| --- | --- | --- | --- |
| Did the system retry only retryable errors? | `error`/failed result with retryable flag, retry action event, final outcome. | Provider status code. | Raw provider error artifact with redaction. |
| Did continuation preserve partial output safely? | `run_end` for failed/interrupted run with continuation context hash; next `run_start` linked to previous run id. | Partial text hash. | Partial transcript artifact. |
| Did quality/validation feedback cause a repair? | Validation/quality event, feedback injection in `context_build`, repair LLM/tool events, final validation pass/fail. | Feedback count limits. | Prompt artifacts. |
| Did guardrail/redundant-search skips stop loops? | Skipped `tool_result` with error code, argument hash, prior matching argument hashes. | Search-history count. | Full arguments if needed. |

## Cost And Latency Budget

| Eval question | Required evidence | Optional evidence | Artifact-only evidence |
| --- | --- | --- | --- |
| Did the run stay under total LLM/tool budget? | `run_end` aggregate tokens/cost/duration, per-event cost/duration. | Budget config hash. | None. |
| Which phase/actor consumed cost? | Event `phase`, `actor`, provider/model, tokens, duration, cost. | Cache hit/miss tokens. | None. |
| Did Phase 3 parallel improve latency without hiding failures? | Orchestrator/worker start/end, worker duration, concurrency config, failure/fallback counts. | Per-worker wait time. | Worker artifacts if debugging. |
| Did artifact persistence overhead fail safely? | Artifact write status in event payload, error event or warning field, run unaffected. | Artifact size bytes. | Artifact metadata only; no raw body required. |

## Minimum Evidence Set For First Implementation Pass

The first recorder implementation should prioritize these required items before
expanding graders:

1. Event common fields: session/trip/context epoch, actor, parent/root/correlation
   ids, started/ended timestamps, payload schema version.
2. Split `llm_call` and `llm_output`, with prompt/context hash and tool-call ids.
3. Split `tool_call` and `tool_result`, with redacted argument/result hashes and
   artifact refs.
4. `state_snapshot` and `state_diff` for writer tools.
5. `phase_gate`, `phase_transition`, `quality_gate`, `validation`, and
   `soft_judge` as independent events.
6. `context_build`, `context_compression`, and `context_rebuild` linked to LLM
   calls.
7. Artifact metadata for prompt, tool arguments/results, state snapshots, Phase 3
   candidates, and deliverables.
