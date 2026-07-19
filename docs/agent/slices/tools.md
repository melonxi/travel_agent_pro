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

## 证据/来源记录校验（阶段 A / A+）

> 定位：结构化来源记录 + 确定性硬规则 + source_ref 绑定。注入 SourceRegistry
> 的写入路径（生产默认）上，confirmed fact 必须回溯到检索工具铸造的 source_id，
> 伪造引用 fail closed；summary/结论文本本身仍由 LLM 压缩，不做内容级防伪。

- Activity 可选 `visit_info`：`{role, recommendation_reason, needs_recheck, evidence[]}`；校验在 `tools/plan_tools/evidence.py`。
- 硬规则 1：UGC 来源（xiaohongshu/user）的 `fact` 不允许 `confidence="confirmed"`（`UGC_FACT_NOT_CONFIRMABLE`）。
- 硬规则 2：`role="anchor"` 必须有一条可靠事实来源——`official/web + claim_type=fact + confidence=confirmed + http(s) source_url`（`is_reliable_fact_record`）；否则必须 `needs_recheck=true`（`ANCHOR_NEEDS_RELIABLE_SOURCE`）。
- 硬规则 3（source_ref 绑定）：`web_search` 每条结果经 `tools/source_registry.py::SourceRegistry` 铸造 `source_id`（`data/sources/<session>.jsonl`，(session,url) 决定性哈希幂等）；evidence.source_ref 必须可解析（`UNKNOWN_SOURCE_REF`）、URL 必须一致（`SOURCE_REF_URL_MISMATCH`）、confirmed fact 必须携带（`CONFIRMED_FACT_NEEDS_SOURCE_REF`）。未注入 registry 的 legacy 路径只做格式校验。
- 校验点覆盖两条写入路径：串行 `save_day_plan`/`replace_all_day_plans`（工具层，registry 由 `build_tool_engine` 注入），并行 `submit_day_plan_candidate` → `Phase3CandidateStore._validate_dayplan`（store 持 registry，root 来自 `phase3.parallel.source_registry_root`；提交时失败，错误码 `INVALID_DAYPLAN_EVIDENCE` 供 Worker 自修复）。
- `set_excluded_candidates`（phases 2/3；Phase 2 子阶段 candidate/skeleton/lock，brief 不开放）整体替换淘汰记录；Phase 4 `generate_summary` 从状态确定性生成「出发前需复核」「已排除/暂缓项目」章节。

## 航班搜索

- `search_flights` 仅在 flyai 可用时注册；已移除 Amadeus sandbox 分支。
- flyai 不可用时由 lock 阶段 prompt 引导 `web_search` 查航线/价格带。

## 小红书工具（默认下线）

- `xiaohongshu_search_notes` / `read_note` / `get_comments` 由 `config.xhs.enabled` 门控，**代码默认 False**（CLI 走登录态抓取，有封号风险）。
- UGC 内容统一由 `web_search` + `include_domains=["xiaohongshu.com",...]` 域内搜索承担；证据模型中的 `source_type="xiaohongshu"` 只是出处标签，不代表调用 XHS CLI。
- 显式 opt-in 才注册；工具执行层另有 `_ensure_enabled` 双保险。

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
