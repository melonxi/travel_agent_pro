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
- `BLACKBOARD_REJECT`（共享黑板提交即拒）

## D3 纵向再协商 + 共享黑板

- Worker 通过 `report_skeleton_infeasible(kind, reason, move_poi?, to_day?)` 上报结构化请求：
  - `INFEASIBLE_DAY`：无法自动改骨架 → 部分交付 + 用户提示
  - `OVERLOADED`：裁剪超限 candidate，只重派该天
  - `SUGGEST_MOVE`：把 POI 迁到目标天（校验容量），只重派 `{source, to}` 两天
- 再协商只改 **骨架副本**（`_skeleton_copy`），不直接写 `plan.skeleton_plans`；改动记入 `_skeleton_amendments` + trace。
- 熔断：每天最多再协商 1 次；受影响天上限 `ceil(总天数/2)`。
- 共享黑板（Orchestrator 单写）：
  - `poi_registry`：POI 认领登记
  - `budget_ledger`：跨天活动成本 + 已锁交通/住宿 precommitted
  - `day_boundaries`：日边界锚点
- 候选收集时 `try_accept_dayplan` 查表即拒；POI 优先按 `poi_id/place_id/location.name`
  归一化；存在稳定 location 时不再用自由文本 activity name 生成身份键，泛化的
  「午餐/自由活动」也不进入认领簿；`location.name` 归一化后产不出有效键（泛化名/空）
  时回退用 activity name，避免真实 POI 逃过跨天去重；P2-1 事后 locked POI 校验保留作兜底。
- 日边界校验取真实最早开始 / 最晚结束（activities 允许乱序），end 落在凌晨
  （≤06:00）且早于 start 的活动按跨午夜折算到次日，不误拒夜间行程。
- `candidate_pois` 是备选池，不是已占用活动；MOVE 目标天容量只按 locked POI 判断，
  worker 再按剩余 slot 从候选池选取。
- 候选 artifact 先写入 staging，并用每个 day 单调递增的 `seq` 表示真实写入顺序；最终
  先按 `seq` 选该天最后版本，再要求其状态为 `accepted`。最后版本被拒或旧版本因重派
  被作废时，该天缺失，不被动回退复活旧 accepted artifact。
- 重派前的乐观作废带回滚（C1）：steering / late-steering / 再协商重派前会作废旧候选并
  释放黑板认领，同时记录 `_RedispatchRollback`（旧 accepted attempt + 旧成功结果）。
  重派失败时 `_handle_redispatch_failure` 把旧候选重新标 accepted 并 bump 到最新
  `seq`、旧结果放回 successes、黑板重新登记——恢复旧版本总比静默丢天好。8b 修复
  重派失败时同样恢复磁盘口径与黑板认领（交付本就保留 in-memory 旧 dayplan）。
- 黑板重试会把拒绝原因和运行时 forbidden 快照下发到下一次 worker，避免同一候选确定性重试。

## D4 运行中引导（Steering）

- 入口：`POST /api/chat/{session_id}/steer`，queue 挂 `session["_steer_queue"]` / `agent.steer_queue`。
- Phase 3：worker 收集循环每步 drain；解析「第 N 天」→ `repair_hints`；已完成天或
  已经开始运行的目标天标记 `STEERING_REDISPATCH` 后重派，等待 semaphore 的 worker
  则直接带 hint 首次执行。
- 已完成天不静默回滚；用户引导只影响未完成 / 被显式点名重派的天。
- SSE：`agent_status.stage=steering_ack`。
- Steering queue 有界（64 条）；生产 drain 不静默裁剪已入队消息，队列满时 `/steer` 返回 429，
  客户端文案明确为“排队并在下一个安全点尝试调整”。
- step7 刚完成的目标天仍视为 active，必须进入 attempt=5 重派；进入再协商/最终修复等
  bounded 收口段后到达的 steering 会收到“本轮未能应用，请重新发送”的终结 ack。
- Agent run 返回后会先关闭 `/steer` 入队入口，再对队列残留逐条发送终结 ack；finally
  只做幂等清理，不再静默吞掉 run 尾部消息。取消（cancelled）路径的终结 ack 先于
  `done` 事件发出；断连/取消导致无法 yield 时，teardown 会把残留引导记入 warning
  日志而非无声丢弃。Steering queue 容量统一使用 `agent.steering.MAX_STEER_QUEUE_SIZE`。

## 写入边界

- Day Worker 只能提交候选，不改 `TravelPlanState.daily_plans`。
- Orchestrator 只负责拆分、派发、收集、验证、再协商（骨架副本）和 handoff。
- 最终写入必须由 AgentLoop 内部构造 `replace_all_day_plans` 工具调用，走标准 `_execute_tool_batch -> detect_phase_transition` 链路。
- 全天提交触发 3→4 后，Orchestrator 的 `DONE` 只表示 Phase 3 子流程结束；AgentLoop 必须在同一 run 内切换到 Phase 4 工具和重建后的消息，继续推进 `generate_summary`。仅在仍停留 Phase 3（部分交付、提交失败等）时结束本轮。

## 关键代码

- `backend/agent/phase3/parallel.py`
- `backend/agent/phase3/orchestrator.py`
- `backend/agent/phase3/day_worker.py`
- `backend/agent/phase3/worker_prompt.py`
- `backend/agent/phase3/candidate_store.py`
- `backend/agent/phase3/renegotiation.py`
- `backend/agent/steering.py`
- `backend/api/orchestration/chat/steering.py`
