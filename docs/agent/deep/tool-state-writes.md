# Tool State Writes Deep Dive

## 18 个状态写工具

| 阶段 | 工具 |
|------|------|
| Phase 1 / 共用基础 | `update_trip_basics`、`add_preferences`、`add_constraints` |
| Phase 2 brief | `set_trip_brief` |
| Phase 2 candidate | `set_candidate_pool`、`set_shortlist` |
| Phase 2 skeleton | `set_skeleton_plans`、`select_skeleton` |
| Phase 2 lock | `set_transport_options`、`select_transport`、`set_accommodation_options`、`set_accommodation`、`set_risks`、`set_alternatives` |
| Phase 2/3 证据链 | `set_excluded_candidates` |
| Phase 3 | `save_day_plan`、`replace_all_day_plans` |
| 跨阶段 | `request_backtrack` |

`generate_summary` 也有写副作用，但它负责 Phase 4 交付物候选和冻结链路，不计入 17 个 plan-writing tools。

## 写入分层

```text
tools.plan_tools.*
  -> 参数 schema / 兼容 legacy 输入 / 错误边界
  -> state.plan_writers.*
  -> TravelPlanState mutation
  -> state manager save
  -> phase transition / hooks / stats
```

不要绕过 `state.plan_writers` 直接修改状态。

## 写后处理

写工具成功后通常会触发：

- `state_mgr.save(plan)`
- session meta 更新，如 phase/title
- `PhaseRouter.infer_phase(plan)`
- incremental validator
- lock budget validator
- soft judge 或 quality gate
- `SessionStats.state_changes`
- SSE `tool_result` / `state_update` / `phase_transition`

## 特殊约束

- `update_trip_basics.budget` 接受数值、对象、可解析数值字符串；非正数和非数字拒绝。
- `set_skeleton_plans` 要校验单个 skeleton 内 POI 在 `locked_pois` / `candidate_pois` 间全局唯一。
- `save_day_plan` 新增或替换单日。
- `replace_all_day_plans` 覆盖全量日程，并校验 day/date/activity/notes schema。
- activity 可选 `visit_info`（role / recommendation_reason / needs_recheck / evidence[]），校验见 `tools/plan_tools/evidence.py`：UGC 的 fact 不允许 confirmed；anchor 必须有「official/web + fact + confirmed + source_url」的可靠事实来源，否则 needs_recheck。并行路径在 `Phase3CandidateStore._validate_dayplan` 提交时做同一校验（`INVALID_DAYPLAN_EVIDENCE`）。
- `set_excluded_candidates` 整体替换淘汰记录（name / reason / category / reconsider_when / source_candidate_id）。
- `request_backtrack` 只保留 rebuild / transition 语义，不伪造字段 diff。

## 关键代码

- `backend/tools/plan_tools/__init__.py`
- `backend/tools/plan_tools/trip_basics.py`
- `backend/tools/plan_tools/append_tools.py`
- `backend/tools/plan_tools/phase2_tools.py`
- `backend/tools/plan_tools/daily_plans.py`
- `backend/tools/plan_tools/backtrack.py`
- `backend/state/plan_writers.py`
