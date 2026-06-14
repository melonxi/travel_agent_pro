# Phase 3 Parallel Deep Dive

## 什么时候读

当任务涉及 Phase 3 并行开关、Orchestrator、Day Worker、候选 artifact、降级、repair 或进度 SSE 时读取。

## 运行模型

```text
AgentLoop
  -> should_enter_parallel_phase3_now / at_iteration_boundary
  -> Phase3Orchestrator.run()
  -> _compile_day_tasks()
  -> N 个 Day Worker 并发
  -> worker-only submit_day_plan_candidate
  -> run-scoped artifact
  -> Orchestrator 全局验证
  -> AgentLoop 内部 replace_all_day_plans 工具调用
```

## 配置

`config.yaml` 的 `phase3.parallel` 控制：

- `enabled`
- `max_workers`
- `worker_timeout_seconds`
- `fallback_to_serial`

失败率超过 50% 时自动降级串行。

## DayTask 约束

DayTask 携带：

- `locked_pois`
- `candidate_pois`
- `forbidden_pois`
- `area_cluster`
- `mobility_envelope`
- `date_role`
- `repair_hints`
- `day_budget`
- `day_constraints`
- `arrival_time`
- `departure_time`

`_build_constraint_block` 把这些字段渲染为中文硬约束块。

## Worker 收敛保护

- 重复查询抑制。
- 补救链阈值。
- 后半程强制收口。
- JSON 修复回合上限。
- 保守落地：修复失败时返回当前已有结果，不无限重试。

## 结构化错误码

- `REPEATED_QUERY_LOOP`
- `RECOVERY_CHAIN_EXHAUSTED`
- `JSON_EMIT_FAILED`
- `TIMEOUT`
- `LLM_ERROR`
- `NEEDS_PHASE3_REPLAN`

## 写入边界

- Day Worker 只能提交候选，不改 `TravelPlanState.daily_plans`。
- Orchestrator 只负责拆分、派发、收集、验证和 handoff。
- 最终写入必须由 AgentLoop 内部构造 `replace_all_day_plans` 工具调用，走标准 `_execute_tool_batch -> detect_phase_transition` 链路。

## 关键代码

- `backend/agent/phase3/parallel.py`
- `backend/agent/phase3/orchestrator.py`
- `backend/agent/phase3/day_worker.py`
- `backend/agent/phase3/worker_prompt.py`
- `backend/agent/phase3/candidate_store.py`
