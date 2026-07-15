# Tools Slice

## 什么时候读

当任务涉及工具声明、工具选择、读写并行、状态写入、Phase 2 工具门控、工具错误、guardrail 或 plan writer 时读取。

## 最小事实

- 工具用 `@tool` 声明名称、描述、阶段、参数 schema、side effect。
- `ToolEngine` 按 phase / phase2_step 过滤工具后传给 LLM。
- 读工具可并行；写工具顺序执行。
- 状态写入必须走 `backend/state/plan_writers.py` 的共享 mutation layer。
- `tools.plan_tools.*` 负责 schema、输入规范化和错误边界，再委托 writer。
- `PLAN_WRITER_TOOL_NAMES` 同时驱动 AgentLoop 的 state-write 判定，确保写工具触发 phase transition 检查。
- `ToolGuardrail` 在执行前后做确定性规则校验。

## Phase 2 工具门控

- `brief`：`set_trip_brief`、`add_preferences`、`add_constraints`，并前瞻开放候选写入工具。
- `candidate`：`set_candidate_pool`、`set_shortlist`，并前瞻开放骨架写入工具。
- `skeleton`：`set_skeleton_plans`、`select_skeleton`。
- `lock`：交通、住宿、风险、备选方案写入工具。
- 每个子阶段向前开放下一阶段写入工具，是为了避免 LLM 跳阶时状态丢失。

## 骨架写入校验（D2）

- `set_skeleton_plans` 的每天 `date_role` 必填：`arrival_day` / `departure_day` / `full_day`。
- 多天行程：首日必须 `arrival_day`，末日必须 `departure_day`，中间日 `full_day`。
- 到达/离开日轻排：`core_activities` 与 `locked_pois` 均不超过 2 项。

## 航班搜索

- `search_flights` 仅在 flyai 可用时注册；已移除 Amadeus sandbox 分支。
- flyai 不可用时由 lock 阶段 prompt 引导 `web_search` 查航线/价格带。

## 工具错误

- `ToolError` 返回 `error_code` + `suggestion` 给 LLM。
- 缺必填参数在函数调用前由 schema required 校验拦截，返回 `INVALID_ARGUMENTS`。
- 重复搜索会被 AgentLoop 滑动窗口拦截并返回 `REDUNDANT_SEARCH`。

## 关键代码

- `backend/tools/base.py`
- `backend/tools/engine.py`
- `backend/tools/plan_tools/`
- `backend/state/plan_writers.py`
- `backend/agent/execution/tool_batches.py`
- `backend/agent/execution/phase_transition.py`
- `backend/agent/execution/repair_hints.py`
- `backend/harness/guardrail.py`

## 深入阅读

- 状态写工具清单：`../deep/tool-state-writes.md`
- Harness：`../deep/harness-architecture.md`
