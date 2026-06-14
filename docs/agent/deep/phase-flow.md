# Phase Flow Deep Dive

## Phase 1：灵感与目的地收敛

- 角色：旅行灵感顾问。
- 目标：用最少轮次收敛到一个目的地。
- 回复纪律：先查后说；每条建议有工具结果支撑；回复短；一次只给少量选项。
- 常用工具：小红书三件套、`web_search`、`quick_travel_search`、`update_trip_basics`。
- 完成 gate：`destination` 非空后自动进入 Phase 2。

## Phase 2：框架规划

Phase 2 由 `phase2_step` 驱动：

- `brief`：旅行画像、目标、节奏、约束、必做和避免项。
- `candidate`：候选池构建、验证、筛选短名单。
- `skeleton`：日级骨架方案，不做逐小时排程。
- `lock`：锁定交通和住宿。

Prompt 拼装：

```text
build_phase2_prompt(step)
  = PHASE2_BASE_PROMPT
  + PHASE2_STEP_PROMPTS[step]
  + render_red_flags(phase=2, phase2_step=step)
```

关键约束：

- `trip_brief` 是后续决策硬锚点。
- `set_skeleton_plans` 写入边界校验 `locked_pois` / `candidate_pois` 全局唯一性。
- Phase 2 -> 3 前校验选中骨架天数等于 `dates.total_days`。
- `_hydrate_phase3_brief()` 强制覆盖 `dates` / `total_days`，避免 stale trip_brief。

## Phase 3：日程详排

- 核心定位：路径规划优化问题。
- 串行模式：AgentLoop 内 LLM 逐日生成。
- 并行模式：Python Orchestrator 拆分 DayTask，多个 Day Worker 并发生成候选，再由 AgentLoop 标准工具路径写入最终日程。
- 状态写入工具：`save_day_plan` / `replace_all_day_plans`。
- 路线辅助工具：`optimize_day_route` 不写状态。
- 回退工具：`request_backtrack`。

Phase 3 上下文必须注入：

- 已选骨架。
- `trip_brief`。
- 偏好和约束。
- 交通住宿锁定信息。

## Phase 4：出发前查漏

- 角色：出发前查漏官。
- 扫描维度：证件签证、天气、预订确认、交通接驳、应急预案。
- 常用工具：`check_weather`、`search_travel_services`、`web_search`、小红书三件套。
- 结束时调用 `generate_summary(...)` 提交结构化交付数据。
- 质量检查通过后冻结 `travel_plan.md` 和 `checklist.md`。

## 阶段转换

- `PhaseRouter.infer_phase(plan)` 根据状态字段推断当前阶段。
- 自动转换会记录 telemetry。
- backtrack 会清除下游数据并轮转 `trip_id`。
- 每个阶段 prompt 末尾追加 `phase/red_flags.py::render_red_flags()`。

## 关键代码

- `backend/phase/router.py`
- `backend/phase/prompts.py`
- `backend/phase/red_flags.py`
- `backend/phase/backtrack.py`
- `backend/agent/execution/phase_transition.py`
- `backend/agent/execution/repair_hints.py`
