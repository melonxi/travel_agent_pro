# Split `update_plan_state` Into Single-Responsibility Tools — Design Spec

**Date**: 2026-04-15
**Status**: Draft (awaiting review)
**Related issue**: AGENT_STREAM_ERROR at `infer_phase3_step_from_state` — LLM stringified `skeleton_plans` payload, tool layer silently accepted the string and appended it as a single "item".
**Related plan**: `docs/superpowers/plans/2026-04-15-prompt-architecture-upgrade.md` (parallel prompt rework; will converge during Step 3 of this migration).

---

## 1. Background and Problem

`backend/tools/update_plan_state.py` is currently the single tool through which the LLM writes any structured state: destination, dates, budget, candidates, shortlists, skeleton plans, daily plans, backtrack, and 15+ other fields. The tool takes two parameters:

```python
update_plan_state(field: str, value: Any)
```

The `value` parameter has **no JSON Schema `type` declaration**, because its required shape depends on `field` (sometimes string, sometimes object, sometimes list of objects). The description enumerates ~20 formats with phrases like "建议传 list / 建议传 dict".

This "omnibus tool" design caused the production incident in session `sess_ab2ed313aa80`:

1. LLM tried to submit three skeleton plans (nested `list[dict]`), but serialized them into a **hand-written JSON string** with a corrupted comma/quote near character 537.
2. `_coerce_jsonish(value)` called `json.loads`; on `JSONDecodeError` it **silently returned the original string**.
3. The skeleton_plans branch saw `value` was not a list and **appended the string as a single skeleton entry**: `plan.skeleton_plans = ["<broken JSON string>"]`.
4. On the next turn, `infer_phase3_step_from_state` called `s.get("id")` on that string → `AttributeError: 'str' object has no attribute 'get'` → stream crashed.

### Root-cause analysis (four concurrent weaknesses)

| Layer | Weakness |
|-------|----------|
| LLM output | Hand-written long JSON is fragile; used `astron-code-latest` (domestic model, moderate function-call fidelity) |
| Tool JSON Schema | `value` is type-less → LLM defaults to stringifying complex nested structures as a safe bet |
| `_coerce_jsonish` | Silently swallows `JSONDecodeError`, returns original string |
| `skeleton_plans` write branch | Accepts any type; non-list values are appended as a single item |
| `infer_phase3_step_from_state` | Reads `skeleton_plans` elements with `.get()` assuming dict type |

Prompt discipline alone (as in the parallel `2026-04-15-prompt-architecture-upgrade.md` plan) cannot fix this: the schema's default path still leads LLMs toward stringification. The fix must harden the **tool layer**.

---

## 2. Goals and Non-Goals

### Goals

1. Permanently eliminate the "stringified list/dict" failure mode for all high-risk structured fields.
2. Replace the omnibus `update_plan_state(field, value)` pattern with a set of single-responsibility tools whose JSON Schemas are strong-typed.
3. Migrate without regressing existing agent flows, via a **transitional adapter period** where `update_plan_state` internally forwards to the new tools.
4. Extract `backtrack` (which is an action, not a field write) into a dedicated tool.
5. Remove `phase3_step` from LLM-writable fields (it is already documented as system-inferred).
6. Keep short-phrase tolerance (`"2 人"`, `"人均 2000"`, `"五一假期"`) for basic scalar fields (`destination / dates / travelers / budget`) via `anyOf` schema; do **not** allow strings for any complex `list[dict]` field.

### Non-Goals

- No change to the storage schema of `TravelPlanState` / `sessions.db`.
- No frontend code changes (`ChatPanel.tsx` already renders `tool.name` + `human_label` + `tool_arguments` transparently).
- No redesign of `BacktrackService` internals; we only extract its invocation into a tool.
- No changes to tools unrelated to state writing (`web_search`, `xiaohongshu_search`, `search_flights`, etc.).

---

## 3. Architecture Overview

```
                  ┌─────────────────────────────────────┐
                  │ LLM (astron-code / compatible)      │
                  └─────────────────────────────────────┘
                            │ tool_call
                            ▼
 ┌────────────────────────────────────────────────────────┐
 │ tools/engine.py  ToolEngine                            │
 │   Phase 1/3/5/7 visibility lists                       │
 └────────────────────────────────────────────────────────┘
              │                                   │
              │ transitional path                 │ new path
              ▼                                   ▼
 ┌──────────────────────┐         ┌──────────────────────────────┐
 │ update_plan_state    │ ──────► │ 18 single-responsibility     │
 │ (adapter, Step 2-3)  │ forward │ plan-writing tools (Step 1+) │
 │   parse → validate   │         │   - set_skeleton_plans       │
 │     → forward to     │         │   - select_skeleton          │
 │     new tool         │         │   - set_candidate_pool       │
 │ Step 4: removed      │         │   - set_shortlist            │
 └──────────────────────┘         │   - ...                      │
                                  └──────────────────────────────┘
                                              │
                                              ▼
                          ┌──────────────────────────────────────┐
                          │ state/plan_writers.py (new)          │
                          │   Thin write layer. One function per │
                          │   write operation. Both new tools    │
                          │   and the adapter call this layer.   │
                          └──────────────────────────────────────┘
                                              │
                                              ▼
                          ┌──────────────────────────────────────┐
                          │ TravelPlanState (state/models.py)    │
                          └──────────────────────────────────────┘
```

### Key design: introduce `state/plan_writers.py` as a thin write layer

Rationale: during the adapter period both `update_plan_state` and the new tools need to mutate `TravelPlanState`. Without a shared layer we would duplicate write logic, risk divergent behavior, and have to undo the duplication in Step 4. With the layer:

- Each write operation is a pure function: `write_skeleton_plans(plan, plans: list[dict]) -> None`.
- Type validation lives in the **tool** (so errors get `ToolError` codes and suggestions); the writer layer performs defensive assertions but does not raise `ToolError`.
- Adapter and new tools both call the same writer → behavior is guaranteed identical.

---

## 4. New Tool Catalog (18 tools)

### 4.1 High-risk strong-schema tools (category A)

These tools receive structured `list[dict]` or nested `dict`. Their schemas forbid strings; this is where stringification is eradicated.

| Tool | Schema (key fields) | human_label | Phases |
|------|---------------------|-------------|--------|
| `set_skeleton_plans` | `plans: array[object]`, each plan has `id: string`, `name: string`, `days: array[object]`, `tradeoffs: object` | 写入骨架方案 | 3 |
| `select_skeleton` | `id: string` (must match existing `plans[].id`) | 锁定骨架方案 | 3 |
| `set_candidate_pool` | `pool: array[object]` | 写入候选池 | 3 |
| `set_shortlist` | `items: array[object]` | 写入候选短名单 | 3 |
| `set_transport_options` | `options: array[object]` | 写入交通候选 | 3 |
| `select_transport` | `choice: object` | 锁定交通方案 | 3 |
| `set_accommodation_options` | `options: array[object]` | 写入住宿候选 | 3 |
| `set_accommodation` | `area: string`, `hotel?: string` | 锁定住宿 | 3, 5 |
| `set_risks` | `list: array[object]` | 写入风险点 | 3, 5 |
| `set_alternatives` | `list: array[object]` | 写入备选方案 | 3, 5 |
| `append_day_plan` | `day: integer`, `date: string (YYYY-MM-DD)`, `activities: array[object]` | 追加一天行程 | 5 |
| `replace_daily_plans` | `days: array[object]` | 整体替换逐日行程 | 5 |
| `set_trip_brief` | `fields: object` (merged into plan.trip_brief) | 更新旅行画像 | 3 |

### 4.2 Phrase-tolerant basics tool (category B)

| Tool | Schema | human_label | Phases |
|------|--------|-------------|--------|
| `update_trip_basics` | Each of `destination`, `dates`, `travelers`, `budget`, `departure_city` is optional and uses `anyOf: [string, object]` to allow either structured input or short phrase. Parsed via existing `parse_*_value` helpers on the string path. | 更新行程基础信息 | 1, 3 |

### 4.3 Append-semantics tools (category C)

| Tool | Schema | human_label | Phases |
|------|--------|-------------|--------|
| `add_preferences` | `items: array[string \| object]` | 记录用户偏好 | 1, 3, 5 |
| `add_constraints` | `items: array[object]` | 记录用户约束 | 1, 3, 5 |
| `add_destination_candidate` | `item: object` | 追加目的地候选 | 1 |
| `set_destination_candidates` | `items: array[object]` | 整体替换候选列表 | 1 |

### 4.4 Standalone action (category D)

| Tool | Schema | human_label | Phases |
|------|--------|-------------|--------|
| `request_backtrack` | `to_phase: integer`, `reason: string` | 请求回退阶段 | 1, 3, 5, 7 |

### 4.5 Removals

- `phase3_step` is no longer LLM-writable. Its value is inferred by `infer_phase3_step_from_state` after each write. Any existing prompt or test referencing `update_plan_state(field="phase3_step", ...)` must be removed in Step 3.

---

## 5. Data Flow: A Typical Call

Example: `set_skeleton_plans`

```
LLM emits tool_call:
  { "name": "set_skeleton_plans",
    "arguments": { "plans": [ {id, name, days, tradeoffs}, ... ] } }
    │
    ▼
ToolEngine.execute(call)
    │
    ▼
tools/plan_tools/set_skeleton_plans.py::set_skeleton_plans(plans: list[dict])
    ├─ L1: JSON Schema (enforced provider-side)
    │        array[object] declaration rejects stringified payloads upstream
    ├─ L2: Runtime type guard
    │        if not isinstance(plans, list): raise ToolError(INVALID_VALUE, ...)
    │        for p in plans:
    │            if not isinstance(p, dict): raise ToolError(INVALID_VALUE, ...)
    │            if "id" not in p: raise ToolError(INVALID_VALUE, ...)
    ├─ L3: state/plan_writers.py::write_skeleton_plans(plan, plans)
    │        Thin write. Does a final assert for internal-bug safety.
    └─ Return: { "updated_field": "skeleton_plans",
                 "count": N, "previous_count": M }
```

On `ToolError`: engine surfaces a structured error message to the LLM, which can self-correct on the next turn.

---

## 6. Four Defense Layers

| Layer | Location | Responsibility | On failure |
|-------|----------|----------------|-----------|
| **L1 JSON Schema** | Tool parameter declaration | Declare strong types (`array[object]`, etc.); forbid polymorphism on high-risk fields | Provider returns `InvalidToolInput` to LLM before Python code runs |
| **L2 Runtime type guard** | First lines of each new tool function | `isinstance` + required-field checks | Raise `ToolError(INVALID_VALUE, suggestion=...)`; LLM sees it next turn |
| **L3 plan_writers assertions** | `state/plan_writers.py` | Minimum internal-contract checks (defensive, should never fire in prod) | `AssertionError` → engine converts to `INTERNAL_ERROR`, logs to trace |
| **L4 Reader-side defense** | `infer_phase3_step_from_state` and similar readers | Filter non-dict elements before `.get()` | Skip contaminated elements, return best-effort inference |

### Additional defense in Step 2 adapter

`update_plan_state` adapter adds: on fields whose target schema is structured (skeleton_plans, candidate_pool, etc.), treat `JSONDecodeError` from `_coerce_jsonish` as **hard error**, not silent fallback. This alone closes the specific regression path from the incident.

---

## 7. Migration Plan (Four Steps, Corresponding to Approach B)

### Step 1 — Scaffold new tools + writer layer (no LLM exposure)

**Changes**:
- New directory: `backend/tools/plan_tools/` containing one file per new tool.
- New module: `backend/state/plan_writers.py`.
- Do **not** register new tools in `main.py::tool_engine.register(...)`.
- Add unit tests for every new tool covering: schema shape, type guard, successful write, `human_label` presence, `side_effect="write"` declaration.
- Add unit tests for `plan_writers.py` covering each write function as a pure function.

**Risk**: None; line-only addition, no runtime change.

**Files touched**: +19 new files (18 tools + 1 writer), +19 new test files.

### Step 2 — `update_plan_state` becomes adapter (bug fix lands)

**Changes**:
- Rewrite `backend/tools/update_plan_state.py`:
  - Keep the single-tool signature `(field, value)` exposed to LLM.
  - Internally: validate type of `value` against the target field; if field expects `list[dict]` and `value` is `str`, **raise `ToolError`** instead of silent append.
  - Forward to the matching function in `plan_writers.py` (not to the new tool wrappers — avoids double schema validation).
  - `_coerce_jsonish` now raises on `JSONDecodeError` when the field is a known structured type.
- Update `is_redundant_update_plan_state` to delegate to `plan_writers` equality checks if needed (read path unchanged).
- Add new tests asserting "stringified `skeleton_plans` now raises `INVALID_VALUE`".
- Adjust existing `tests/test_update_plan_state.py` cases: any test that relied on silent string acceptance should now assert `ToolError`.

**Risk**: Medium. This is where behavior changes for live users. LLMs that used to stringify will now see errors. Expected recovery: LLM re-emits with structured value (tested via eval). If eval regresses, fall back by temporarily widening the adapter.

**Files touched**: `tools/update_plan_state.py`, `tests/test_update_plan_state.py`, new `tests/test_update_plan_state_strict.py`.

**After Step 2 merges**: the production incident is already prevented. Steps 3–4 are about replacing the LLM-facing surface with clearer tools.

### Step 3 — Expose new tools + migrate prompts, guardrails, forced-call logic

**Changes**:
- `main.py`: register all 18 new tools.
- `tools/engine.py`: add every new tool to the matching Phase visibility set; `update_plan_state` stays in the whitelists (transitional).
- `phase/prompts.py`: replace 15+ example snippets of `update_plan_state(field=..., value=...)` with the corresponding new tool call. Each phase prompt's "状态写入契约" section gets rewritten as a list of "preferred tools + call examples". This aligns with the parallel `prompt-architecture-upgrade` plan.
- `agent/loop.py`: replace guardrail nudge text and `is_redundant_update_plan_state` call site as needed. The literal strings "update_plan_state" in nudge messages become the appropriate new tool names (e.g. "请调用 `set_trip_brief(...)`").
- `agent/reflection.py`, `harness/guardrail.py`: same text replacements.
- `agent/tool_choice.py::_FORCED`: either replace with a context-aware selector (pick which new tool to force based on missing field) **or** remove the forcing for now and rely on prompt discipline. Decision: **remove the hard force in Step 3**; re-evaluate via eval metrics in a follow-up. If LLM compliance drops, reintroduce as a selector.
- Golden-case evals (`backend/evals/golden_cases/`) run to verify no regression.

**Risk**: High-surface changes to prompts and guardrails; mitigated by E2E eval pass before merge.

**Files touched**: `main.py`, `tools/engine.py`, `phase/prompts.py` (major), `agent/loop.py`, `agent/reflection.py`, `harness/guardrail.py`, `agent/tool_choice.py`, plus prompt-related tests (`test_prompt_architecture.py`, etc.).

### Step 4 — Remove `update_plan_state`

**Changes**:
- Deregister `update_plan_state` in `main.py`.
- Remove it from all Phase visibility sets in `tools/engine.py`.
- Delete `backend/tools/update_plan_state.py` (or reduce to a deprecated stub for one release cycle).
- Provide `tests/helpers/register_plan_tools.py`: one-call helper to register all 18 new tools on a `ToolEngine`, enabling batch migration of the 30+ test files.
- Migrate each of the 30+ tests that currently call `make_update_plan_state_tool(plan)` to use the helper and structured calls. Some tests become simpler (they don't need to construct `field=/value=` kwargs).
- Update `PROJECT_OVERVIEW.md` tool catalog.
- Optionally add a migration note in `docs/learning/<date>-plan-writing-tools-migration.md`.

**Risk**: Low after Step 3 stabilizes; mostly mechanical.

**Files touched**: `main.py`, `tools/engine.py`, `backend/tools/update_plan_state.py` (removed), 30+ test files.

### Each step is independently committable and reversible.

---

## 8. Testing Strategy

| Test layer | Scope |
|------------|-------|
| Per-new-tool unit tests (`tests/test_plan_tools/*`) | Schema shape, required fields, type guard rejects non-list/non-dict, redundancy, `human_label`, `side_effect` |
| `plan_writers.py` unit tests | Each write function as pure data mutation |
| Adapter regression tests (Step 2) | Existing `tests/test_update_plan_state.py` cases re-run; new cases assert stringified structured fields raise `INVALID_VALUE` |
| Prompt alignment tests (Step 3) | No example in `phase/prompts.py` contains `update_plan_state(`; each Phase whitelist contains the expected new tools |
| Guardrail text tests (Step 3) | Generated nudges no longer contain the literal string `"update_plan_state"` after migration |
| E2E golden path (`evals/golden_cases/`) | Full Phase 1→3→5→7 runs; assert new tools are called at expected points |

### Test helper

`backend/tests/helpers/register_plan_tools.py`:

```python
def register_all_plan_tools(engine: ToolEngine, plan: TravelPlanState) -> None:
    """Register every plan-writing tool bound to the given plan."""
    # ~18 engine.register(...) calls
```

Used in Step 4 to batch-migrate the 30+ test files that currently do `engine.register(make_update_plan_state_tool(plan))`.

---

## 9. Frontend / Prompt / Documentation Impact

### Frontend

- **Zero code changes**. `ChatPanel.tsx:466` reads `toolCall.human_label` for the label; `line 469` reads `toolCall.name` for the raw name. Both are pass-through.
- UX improvement: users will see more distinctive tool labels (写入骨架方案 / 锁定骨架方案 / 请求回退阶段) instead of the uniform "更新旅行计划". This makes the agent's actions more legible.
- Historical sessions (DB rows where `tool_calls.name == "update_plan_state"`) continue to render correctly because the frontend stores and displays `name` verbatim.

### Prompts

- `phase/prompts.py` receives the largest textual change in Step 3.
- Each phase's "状态写入契约" section is rewritten as a table: `场景 → 推荐工具 → 调用示例`.
- Converges with `docs/superpowers/plans/2026-04-15-prompt-architecture-upgrade.md` (skill-card prompt rework). No conflict: that plan reshapes prompt *structure*; this design changes the *tools referenced within the prompt*. Step 3 of this spec and Task 5 of that plan can be merged or sequenced.

### Documentation

- `PROJECT_OVERVIEW.md`: update the tool catalog section to list the 18 new tools grouped by category.
- Legacy spec `docs/superpowers/specs/2026-04-04-backtrack-into-update-plan-state-design.md`: add a short note that the decision was revisited here; `backtrack` is now a standalone `request_backtrack` tool.
- Optionally: `docs/learning/<date>-plan-writing-tools-migration.md` as a post-mortem of the incident + migration recap.

---

## 10. Open Questions (Deferred to Implementation Plan)

1. **`tool_choice._FORCED` strategy after removal of `update_plan_state`** — Step 3 removes the hard force. If eval shows LLM skipping state writes too often, reintroduce as a context-aware selector. Measure before reintroducing.
2. **Test helper shape** — Step 4 helper could also optionally register backtrack-tool + other non-writer tools; decide during implementation based on how many tests want a one-line fixture.
3. **Deprecated stub vs. deletion** — Step 4: keep `update_plan_state.py` as a deprecated stub for one release, or delete outright. Defaulting to **delete**; no external callers exist.
4. **Convergence order with `prompt-architecture-upgrade`** — If that plan's Task 5/7 (Phase 5/7 rewrite) have not yet landed when Step 3 of this design starts, reconcile the edits together to avoid merge pain. Owner decides at Step 3 kickoff.

---

## 11. Success Criteria

- [ ] The exact production incident (stringified `skeleton_plans` → `AttributeError`) is impossible to reproduce. Covered by a dedicated test that emits the faulty tool_call shape and asserts `ToolError(INVALID_VALUE)` instead of a crash.
- [ ] After Step 2, all high-risk structured fields reject stringified input with an LLM-recoverable `ToolError`.
- [ ] After Step 3, the LLM sees 18 single-responsibility tools instead of one omnibus tool; no prompt example references `update_plan_state(`.
- [ ] After Step 4, grep for `update_plan_state` in `backend/` returns zero hits (outside of migration notes).
- [ ] Golden-case evals pass at each step.
- [ ] Frontend renders new tool calls without code changes.

---

## 12. Appendix: File Change Summary

| Step | Added | Modified | Removed |
|------|-------|----------|---------|
| 1 | `tools/plan_tools/*` (18 files), `state/plan_writers.py`, `tests/test_plan_tools/*` (18 files), `tests/test_plan_writers.py` | — | — |
| 2 | `tests/test_update_plan_state_strict.py` | `tools/update_plan_state.py` (major rewrite), `tests/test_update_plan_state.py` | — |
| 3 | — | `main.py`, `tools/engine.py`, `phase/prompts.py` (major), `agent/loop.py`, `agent/reflection.py`, `harness/guardrail.py`, `agent/tool_choice.py`, prompt-related tests | — |
| 4 | `tests/helpers/register_plan_tools.py` | `main.py`, `tools/engine.py`, 30+ test files, `PROJECT_OVERVIEW.md` | `tools/update_plan_state.py` |

Estimated total diff: ~2000 lines across ~60 files.
