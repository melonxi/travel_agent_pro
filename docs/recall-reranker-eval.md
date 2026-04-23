# pass@k 稳定性评估报告

- 运行时间：2026-04-23 10:53
- k = 5
- 覆盖 case 数：6

## 总览

| 指标 | 值 |
|------|-----|
| 总体 pass@k | 0.90 |
| 不稳定 case（pass_rate < 1.0） | 1 |
| 高度不稳定 case（pass_rate < 0.6） | 1 |

## 按 Case 详情

| Case | 难度 | pass@5 | 不稳定断言 | 工具一致性 | 平均成本 |
|------|------|--------|-----------|-----------|---------|
| recall-001-style-force-query-fallback | memory | 5/5 | memory_recall_field:final_recall_decision (0.00), memory_recall_field:query_plan_source (0.00), memory_recall_field:recall_attempted_but_zero_hit (0.00) | 1.00 | $0.00 |
| recall-002-current-trip-fact-skip | memory | 5/5 | memory_recall_field:final_recall_decision (0.00), memory_recall_field:recall_skip_source (0.00) | 1.00 | $0.00 |
| recall-003-gate-failure-profile-cue | memory | 2/5 | memory_recall_field:final_recall_decision (0.00), memory_recall_field:fallback_used (0.00), memory_recall_field:query_plan_source (0.00) | 1.00 | $0.00 |
| recall-004-ack-preference-force | memory | 5/5 | memory_recall_field:stage0_matched_rule (0.00), memory_recall_field:final_recall_decision (0.00) | 1.00 | $0.00 |
| recall-005-negated-profile-signal | memory | 5/5 | memory_recall_field:stage0_decision (0.00), memory_recall_field:stage0_matched_rule (0.00) | 0.00 | $0.00 |
| recall-006-recommend-fallback | memory | 5/5 | memory_recall_field:final_recall_decision (0.00), memory_recall_field:query_plan_source (0.00) | 1.00 | $0.00 |

## 高方差断言（一致性 < 0.6）

| Case | 断言 | 一致性 | 说明 |
|------|------|--------|------|
| recall-001-style-force-query-fallback | memory_recall_field:final_recall_decision | 0.00 | 0/5 次通过 |
| recall-001-style-force-query-fallback | memory_recall_field:query_plan_source | 0.00 | 0/5 次通过 |
| recall-001-style-force-query-fallback | memory_recall_field:recall_attempted_but_zero_hit | 0.00 | 0/5 次通过 |
| recall-002-current-trip-fact-skip | memory_recall_field:final_recall_decision | 0.00 | 0/5 次通过 |
| recall-002-current-trip-fact-skip | memory_recall_field:recall_skip_source | 0.00 | 0/5 次通过 |
| recall-003-gate-failure-profile-cue | memory_recall_field:final_recall_decision | 0.00 | 0/5 次通过 |
| recall-003-gate-failure-profile-cue | memory_recall_field:fallback_used | 0.00 | 0/5 次通过 |
| recall-003-gate-failure-profile-cue | memory_recall_field:query_plan_source | 0.00 | 0/5 次通过 |
| recall-004-ack-preference-force | memory_recall_field:stage0_matched_rule | 0.00 | 0/5 次通过 |
| recall-004-ack-preference-force | memory_recall_field:final_recall_decision | 0.00 | 0/5 次通过 |
| recall-005-negated-profile-signal | memory_recall_field:stage0_decision | 0.00 | 0/5 次通过 |
| recall-005-negated-profile-signal | memory_recall_field:stage0_matched_rule | 0.00 | 0/5 次通过 |
| recall-006-recommend-fallback | memory_recall_field:final_recall_decision | 0.00 | 0/5 次通过 |
| recall-006-recommend-fallback | memory_recall_field:query_plan_source | 0.00 | 0/5 次通过 |

## 成本与延迟统计

| Case | 成本 min | 成本 max | 成本 stddev | 延迟 mean | 延迟 stddev |
|------|---------|---------|------------|----------|------------|
| recall-001-style-force-query-fallback | $0.00 | $0.00 | $0.00 | 17519ms | 8123ms |
| recall-002-current-trip-fact-skip | $0.00 | $0.00 | $0.00 | 16863ms | 9678ms |
| recall-003-gate-failure-profile-cue | $0.00 | $0.00 | $0.00 | 52138ms | 56936ms |
| recall-004-ack-preference-force | $0.00 | $0.00 | $0.00 | 26003ms | 29742ms |
| recall-005-negated-profile-signal | $0.00 | $0.00 | $0.00 | 35106ms | 24393ms |
| recall-006-recommend-fallback | $0.00 | $0.00 | $0.00 | 14022ms | 5849ms |
