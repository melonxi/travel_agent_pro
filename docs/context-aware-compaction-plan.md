# Plan: Context-Aware Progressive Tool Result Compaction

## Goal
Replace the fixed 220-char truncation with context-window-aware progressive compaction.

## Rubber-duck critique findings adopted
1. **Token estimator is broken** — `len(m.content or "") // 3` misses `tool_result.data`.
   Fix: new estimator that covers content + tool_result + tool_calls.
2. **Don't drop URLs** — they're citation/grounding handles. Keep title+url, trim body.
3. **Need list caps** — 50 results × any chars still blows up. Cap to top N + omitted_count.
4. **2 tiers for v1** — existing full-history compressor handles extreme case. Use
   preserve (< 0.6) vs compact (≥ 0.6). Avoid overlapping with the 0.5 full-compress.
5. **Use prompt budget** — reserve output_tokens (4096) + safety_margin (2000).

## Design (revised)

### Prompt budget & tiers
- `prompt_budget = context_window - 4096 (output) - 2000 (safety)`
- `usage_ratio = estimate_messages_tokens(messages) / prompt_budget`
- **< 0.6** → FULL: no truncation
- **≥ 0.6** → COMPACT:
  - `web_search`: answer→400, content→200, keep title+url, cap results to 5, omitted_count
  - `xhs/read_note`: desc→300, keep title+note_id
  - `xhs/get_comments`: content→200, cap to 8 comments, omitted_count
  - `xhs/search_notes`: keep title+note_id per item, cap to 8

### Token estimation fix
- `estimate_message_tokens(msg)`: content + json.dumps(tool_result.data) + tool_calls args

## Implementation todos
1. Create `agent/compaction.py` — compaction logic + token estimation
2. Update `agent/loop.py` — remove old code, add context_window, compute ratio
3. Update `main.py` — pass context_window, use shared estimator in on_before_llm
4. Rewrite `tests/test_loop_payload_compaction.py` — both tiers + estimation + caps

## Status
- [ ] Create compaction module
- [ ] Update AgentLoop
- [ ] Update main.py
- [ ] Rewrite tests
- [ ] Run tests
