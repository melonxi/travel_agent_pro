# Trace Failure Taxonomy

This taxonomy is used by failure-analysis reports and trace-grade triage.

| Root cause | Meaning | Primary trace evidence |
|---|---|---|
| planning | Wrong planning strategy or phase objective | `phase_gate`, `phase_transition`, `state_diff` |
| tool selection | Required tool skipped or wrong tool chosen | `llm_output`, `tool_call`, `tool_result` |
| tool args | Tool arguments do not reflect user constraints | `tool_call.arguments_hash`, argument artifact |
| tool result quality | Empty, partial, low-confidence, or errored tool result | `tool_result.quality_flags`, `validation` |
| state write | Incorrect write, missing diff, or unclear lock source | `state_diff`, writer `tool_result` |
| phase transition | Move/block decision lacks gate evidence | `phase_gate`, `quality_gate`, `phase_transition` |
| memory recall | Recall skipped, false recalled, or injected irrelevant memory | `memory_recall`, `memory_hit`, `context_build` |
| context pollution | Compression/rebuild dropped or polluted context | `context_build`, `context_compression`, `context_rebuild` |
| quality gate | Validator/judge failed to block bad output | `validation`, `soft_judge`, `quality_gate` |
| external service | Third-party service failure or missing data | errored `tool_result`, `error` |

Reports should cite concrete `event_id` values whenever possible.
