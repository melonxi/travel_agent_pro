# 旅行 Agent 设计问题分析（问题 / 证据 / 解决方案）

> 数据来源：活体 canary 会话 `sess_25e7865827a1`（Phase 1→4 全程，7 个 trace run）的 `trace_events` / `session.db` 与最终交付物（`travel_plan.md` + `checklist.md`）。
>
> 与 `canary-live-trace-analysis-2026-06-07.md` 的分工：那份讲「测试方法该怎么改」（自适应驱动 + trace 审计）；**本份讲「agent 本身该怎么修」**。
>
> 严重度分级：**P0**（影响交付物正确性 / 可信度）＞ **P1**（用户控制力 / 显式约束）＞ **P2**（成本 / 效率 / 可观测）。

## TL;DR

agent 的纪律目前靠**少数工具层硬校验点**撑着（lock、未来天气不得当定论、日程时间冲突）——这几处全程守住；而**没有硬校验兜底的地方全部失守**：锁定态不下传 Phase 3、跨阶段事实不收敛、核心路由能力近半失败被静默降级、Phase 2 步内不可控、显式 pace 约束在 Phase 3 只作为 warning、不触发重派。本质问题是一个**「描述性」而非「控制性」的阶段机**——phase/step 大多只记录「最后停在哪」，而不约束「过程做什么」。

最该先修：**P0-1（锁定 transport 下传）** 与 **P0-2（交付前一致性收敛）**，它们直接决定用户拿到的方案是不是「对的」。

---

## P0-1 锁定的大交通不下传 Phase 3 day worker

**问题**：用户锁定了具体航班后，Phase 3 的并行 day worker 在上下文里**拿不到锁定的航班标识**，于是自己 `web_search` 重搜或编造，导致最终逐日行程里的航班与用户锁定的航班不一致。

**证据**：
- `backend/agent/phase3/worker_prompt.py` 的 `build_shared_prefix()` 注入了目的地、日期、人数、画像、**住宿（区域+酒店）**、预算、偏好、约束——但**没有 `plan.transport`**。
- `DayTask`（同文件）只带 `arrival_time` / `departure_time` 两个时间字段，**没有航班标识**（航司/航班号/机场/票价）。
- trace `run6` 中 day worker 多次调用 `web_search`，**无任何读取锁定 transport 的事件**。
- 交付物 Day1 叙述「如 NH972 PVG 08:25→HND 12:25」，与用户锁定的方案A（东航 MU523 09:05→12:50 去 / 春秋 IJ005 回）**对不上**。

**解决方案**：
1. 在 `build_shared_prefix()` 增加一个**「已锁定大交通（只读）」块**：航司、航班号、出发/到达机场与时间、票价，作为权威事实注入所有 day worker。
2. 对到达日 / 离开日，把到/离港航班的具体信息注入对应日的 `build_day_suffix()`（`DayTask.arrival_time/departure_time` 旁补上航班标识）。
3. 收紧 day worker 提示词（`_WORKER_ROLE`）：**到/离港信息以锁定态为唯一来源，禁止用 `web_search` 重新查航班**；锁定态缺字段时写 notes，不得自行发明。

---

## P0-2 跨阶段事实不收敛（同一交付物自相矛盾）

**问题**：各阶段独立调用工具、独立生成内容，缺少「交付前事实收敛」步骤；后期纠正不向前传播，导致最终交付物内部自相矛盾（最典型：天气）。

**证据**：
- Phase 3 Day2 写「天气参考：**小雨 / 18°C**，建议带伞」；Phase 4 summary 写「7月东京…**平均约 28.7°C，体感湿热**」——两处天气在**最终产物里直接打架**。
- `backend/tools/generate_summary.py` 接收的是 **Phase 4 agent 自撰的 `travel_plan_markdown` / `checklist_markdown`**，工具只校验 H1 / 逐日章节 / 清单项的**结构**，不校验与 plan 状态、与 Phase 3 内容的**一致性**。
- `FUTURE_WEATHER_NOT_TREATED_AS_EXACT` 是**实时 hook**（非工具内）做的——它只查「未来天气是否被当成精确预报」，**不查前后是否一致**。
- `check_weather` 在 Phase 3、Phase 4 被独立调用，且返回值不可靠：超出 OpenWeather 5 天预报窗口时，当前实现会返回 `forecast_list[0]` 最近一条作为参考，6 月运行去查 7 月日期就可能拿到当下天气（如 18°C），被 Phase 4 用常识纠正为 28.7°C 后也未回灌 Phase 3 已生成内容。

**解决方案**：
1. **天气单一事实源**：整个会话只解析一次天气，写入 plan 状态（含「精确预报不可用 / 参考来源类型」标注）；Phase 3 day worker 与 Phase 4 summary **共读同一来源**，禁止各自独立 `check_weather`。
2. **`check_weather` 数据质量**：无精确预报时不要把最近 5 天预报伪装成目标日期天气；若要返回**季节气候均值**，需先补 climatology 表 / 气候数据源 / 明确 fallback 策略，并显式标注不确定。
3. **交付前一致性 hook**：扩展现有实时校验 hook，交付前交叉核对最终 markdown 与 plan 状态——天气、锁定的交通/住宿、关键数值（预算、日期）若与权威状态矛盾则拦截重写。

---

## P0-3 核心能力 `calculate_route` 近半失败且被静默降级

**问题**：地理路由（产品主打的「动线效率 / 地理可行性」）在 Phase 3 近半失败，系统静默退化为 web 估算，把**没真正算出来的通勤时长当成计划呈现**，且只在交付物里埋一句小字 caveat。

**证据**：
- trace 全会话 `calculate_route`：**11 成功 / 9 失败，9 个失败全是 `NO_ROUTE`（~45%）**。
- `backend/tools/calculate_route.py`：默认 `mode="transit"`，但请求参数（第 121-126 行）**只有 origin/destination/mode/key，缺 `departure_time`**。这是 `NO_ROUTE` 的高概率贡献因素之一；但当前 trace 没记录 Google raw status / mode / 坐标样本，暂不能把 9 次失败完全归因到这一条。短距离 transit 也常无解（本应 walking）。
- 工具把错误抛回、靠 LLM 自行换 mode（`_STATUS_ERROR_MAP` 的 suggestion 也这么写），但 day worker 往往不换、直接退化估算（`_ROUTE_UNAVAILABLE_PROMPT`）。
- 结果：交付物 Day2「交通时长为保守估算（路线工具未返回可用结果，基于 web 搜索估算）」。

**解决方案**：
1. **transit 请求带 `departure_time`**（用计划中的活动时间，或保守取「下一个工作日同时段」），并在 trace 中记录 mode / 坐标 / Google raw status，用样本验证它对 `NO_ROUTE` 的实际改善。
2. **工具内置 mode 回退链**：transit→ZERO_RESULTS 时，对短距离自动改 `walking` 重试再返回，而不是把回退责任丢给 LLM。
3. **坐标前置校验**：调用前确保经过 `get_poi_info` 拿到可靠坐标（减少 `NOT_FOUND`）。
4. **估算必须显著标注**：退化为估算时在 activity 上打结构化标记（非自由文本小字），交付层统一渲染为可见提示。

---

## P1-4 Phase 2 步内不可控（摁不住）

**问题**：用户明确要求「只做到某一步、先别往下」时，agent 无视并一口气把 Phase 2 研究流水线跑到底——用户对中间过程**零控制力**。这对一个主打「人在环、显式确认」的产品是内部矛盾。

**证据**：
- Turn 3：用户说「只把这些写进画像就行，先别去搜候选」，agent 在同一轮跑完 brief→candidate→shortlist→skeleton。
- trace `run3` 工具序列：`set_candidate_pool`(#23) → `set_shortlist`(#28) → `set_skeleton_plans`(#38 err→#40 ok)，全在一个 run。
- prompt 与工具描述对状态机语义互相打架：`backend/phase/prompts.py` 写「`shortlist` 写入不触发推进——你仍在 candidate」，但 `backend/tools/plan_tools/phase2_tools.py` 的 `set_shortlist` 描述写「shortlist 写入后，系统会自动推进到 skeleton 子阶段」。
- 对照：**lock 那条线做到了 gate**（select_* 仅在显式授权后触发），但**研究/构建这条线完全没 gate**——状态机靠 agent 动量推进。

**解决方案**：
1. **step 级停止点**：识别用户「只到这一步 / 先别往下」的意图，在 phase2 step 边界插入显式停止，等用户确认再推进（把 lock 的确认模式推广到研究/构建步）。
2. 先修正 `set_shortlist` 工具描述，使其与 prompt 一致：写入 shortlist 后只完成 candidate 产物，不鼓励同轮继续写 skeleton。
3. 或在 orchestration hook 层对 `set_candidate_pool/set_shortlist/set_skeleton_plans` 加**前置确认门**：当本轮用户消息含「停在此步」信号时，拦截下游写工具并要求确认。

---

## P1-5 pace mismatch 只 warning，不触发 Phase 3 重派

**问题**：结构合法性（时间冲突）是硬门，但**体验质量中的 pace mismatch（恰恰是用户点名的约束）在 Phase 3 被标成 warning**，不会进入重派修复回路；后续 soft_judge 虽有阻断路径，但达到重试上限后仍会放行。

**证据**：
- trace：`submit_day_plan_candidate` 的 `INVALID_DAYPLAN_TIME_CONFLICT` 是**硬 gate**（2 次被打回重试）；而 pace 超限只有 soft_judge `warning`。
- `backend/agent/phase3/orchestrator.py` 的 `_validate_pace()` 把超节奏写成 `severity="warning"`；重派回路只取 `severity == "error"` 的 issue，所以 `pace_mismatch` 永不触发 day worker 重排。
- `backend/api/orchestration/agent/hooks.py` 的 soft_judge 对 `overall < quality_gate.threshold` 会注入修复反馈、对 `generate_summary` 置 `status="blocked"`；但 `feedback_count >= max_retries` 后 `should_inject_feedback=False`，最终放行。
- 结果：用户明确要 balanced / 不要太密，**Day3 仍排了 5 个活动、超 balanced 上限 4**，warning 之后照样进了最终方案。

**解决方案**：
1. 把**「用户显式约束」类质量升级为硬门**：pace 超 balanced 上限时触发 day worker 重排或 orchestrator 修补回路，而非仅 warning。
2. 对**用户显式点名的维度**设置不可超重试放行的确定性门槛；soft_judge 可以继续给整体质量建议，但 pace 这类可规则化约束应由 orchestrator / validator 硬拦。

---

## P2-6 Phase 2 与 Phase 3 研究重复（昂贵且是不一致温床）

**问题**：Phase 2 candidate/shortlist 已经做过 POI 研究，Phase 3 day worker 又重新 `get_poi_info`/`web_search`——既贵，又是 P0-1/P0-2 那些「重搜＝重新发明事实」的根源。

**证据**：
- trace `run6` 一轮：**81 万输入 token / 41 次 llm_call / `get_poi_info`×23 + 多次 `web_search`**。
- 而 Phase 2 阶段 `xiaohongshu_*` 与 `web_search` 已对同一批 POI 搜过一遍。

**解决方案**：
1. Phase 2 的候选研究产物（POI 坐标/票价/营业信息）**结构化存入 plan 状态**；Phase 3 day worker **优先复用**，仅对缺失项补查。
2. `get_poi_info` 走**会话级缓存**，跨阶段共享，避免重复外呼。

---

## P2-7 成本不可见 + 该会话未产出自动评分

**问题**：runtime 拿不到成本信号，无法预算/止损；该次 canary 会话没有产出 / 保存 deterministic grades，导致这次问题分析仍靠人工审 trace。

**证据**：
- trace_runs 的 `total_cost_usd` **全为 0**（该 provider 未配置计价；token 维度正常：79 llm_call / 154 万输入 / 7.2 万输出）。
- `trace_grades` 表对本会话 **0 行**。注意：当前代码里 `POST /api/traces/{run_id}/grade` 已会 `save_grades()`，canary 脚本也已接 `_grade_run()`；本会话早于这条接线，不能再概括成「grader 未挂」。

**解决方案**：
1. 为所用 provider/model 配置计价，使每个 llm_call 的 `cost_usd` 真实可算，runtime 能做预算/止损。
2. 对历史/现有 canary 报告明确标注是否已执行 grader；后续 canary / CI 必须把 grades 写回 `trace_grades`，并在报告里展示分数与回归基线。

---

## 修复优先级建议

| 优先级 | 项 | 理由 |
|---|---|---|
| 1 | **P0-1 锁定 transport 下传** | 直接决定交付物是否与用户锁定一致 |
| 2 | **P0-2 交付前一致性收敛** | 消除同一产物自相矛盾（天气/数值） |
| 3 | **P0-3 路由可靠性** | 核心卖点能力，近半失败 + 静默冒充实算 |
| 4 | P1-5 pace 硬门 | 用户显式约束不该被 warning 放行 |
| 5 | P1-4 步内可控 | 兑现「人在环」产品承诺 |
| 6 | P2-6 / P2-7 | 降本 + 可观测，支撑前面几项的回归验证 |

P0-1 与 P0-2 是同一类病根（**锁定/权威事实不向下游传播、且无收敛复核**）的两个面，建议合并为一个「事实一致性」工作流一起做。

---

## 补充案例：adaptive canary `sess_2b04b8dbf6c1` 没能完成交付

> 数据来源：`scripts/run-adaptive-canary.py`，报告文件 `data/canary_runs/adaptive-sess_2b04b8dbf6c1.json`，最终 plan 快照 `data/sessions/sess_2b04b8dbf6c1/plan.json`。

**结果**：
- `RESULT=FAIL`
- `goal_met=False`
- `has_deliverables=false`
- 最终状态：`phase=4`、`phase2_step=lock`、`daily_plan_count=3`
- 前置 violation：turn 3 `lock_without_consent:select_skeleton`，turn 4 `lock_without_consent:select_transport`

**第 13 轮之后发生了什么**：

1. 第 12 轮用户明确选择回程 `JL085 14:25 羽田 -> 浦东`，但该轮 `tool_calls=[]`，没有写入 `selected_transport`。
2. 第 13 轮用户同意回退到 Phase 3，要求重排航班时间、每天 3-4 个活动，并标注天气需要临近确认。
3. agent 调用了 `request_backtrack`，随后 Phase 3 worker 重新生成 3 天行程。
4. 但 worker 读到的权威状态仍是旧交通方案 `plan_a`：春秋/成田晚班，因为 JL085 只被口头确认，没有写进状态。
5. 新生成的 Day 3 又写成「前往成田机场 / Narita Express」，与用户刚选的羽田 JL085 冲突。
6. Day 2 仍有 6 个活动，超过 balanced 上限 4。
7. 系统因为 `daily_plans` 数量够 3 天，又回到 Phase 4。
8. 第 14 轮用户指出问题后，Phase 4 只能查天气，不能修改 `daily_plans`，agent 于是再次请求回退，没有生成 `generate_summary` 交付物。
9. canary 默认最多 14 轮，到这里结束，因此 `has_deliverables=false`。

**直接根因**：

- **口头锁定和状态锁定脱节**：用户选了 JL085，但 Phase 4 没有可用写工具更新 `selected_transport`，状态仍保留旧的成田晚班。
- **Phase 4 不能修上游状态**：Phase 4 prompt 明确禁止修改 `daily_plans` / `accommodation` / `selected_transport`，遇到行程事实错误只能回退。
- **回退后仍读旧权威状态**：Phase 3 worker 以 `TravelPlanState` 为权威，继续按旧 `selected_transport` 规划。
- **Phase 3 -> Phase 4 gate 只看天数**：`PhaseRouter.infer_phase()` 只判断 `len(daily_plans) >= total_days`，不检查机场、航班、pace、路线冲突是否合格。
- **pace 校验没有硬阻断**：`_validate_pace()` 在 `trip_brief.pace` 不是标准枚举时把超限标为 warning，只提示「发现问题」，不阻断进入 Phase 4。
- **backtrack 工具语义诱导停顿**：`request_backtrack` 实际会立刻回退，但返回 `next_action="请向用户确认回退结果，不要继续调用其他工具"`，容易让 agent 在修复闭环中反复请求确认。

**代码证据**：

- `backend/phase/prompts.py`：Phase 4 不可调用行程规划工具，且不修改 `daily_plans` / `accommodation` / `selected_transport`。
- `backend/phase/router.py`：Phase 4 推进条件是 `daily_plans` 数量覆盖出行天数。
- `backend/state/plan_writers.py`：`request_backtrack` 返回的 `next_action` 要求确认回退结果，不继续调用工具。
- `backend/agent/phase3/orchestrator.py`：pace mismatch 只有 error 才重派；非标准 `trip_brief.pace` 下超 balanced 只算 warning。
- `data/sessions/sess_2b04b8dbf6c1/plan.json`：最终 `selected_transport` 仍是旧 `plan_a`，Day 3 仍是成田/N'EX。

**需要补的设计能力**：

1. **Phase 4 中的锁定修正入口**：用户在 Phase 4 选择新航班时，要么允许受控调用 `select_transport`，要么提供专门的 `revise_locked_transport` 工具，不能只口头确认。
2. **回退携带修正事实**：`request_backtrack` 应支持带入用户刚确认的变更事实，或回退前先写入权威状态，避免 Phase 3 读旧值。
3. **Phase 3 完成 gate 加强**：进入 Phase 4 前检查锁定交通和到达/离开日机场、时间是否一致；检查 explicit/implicit pace 上限；检查严重路线和时间冲突。
4. **pace 标准化**：用户说「balanced / 不要太赶」时必须写入标准 `trip_brief.pace="balanced"`，让 Phase 3 校验升级为 error。
5. **backtrack 闭环语义重写**：区分「申请回退给用户确认」和「已获授权，立即回退并继续修复」。本案例第 13 轮用户已授权，不应再次停在确认。

**本轮已落地修复**：

- P0：`select_transport` 受控开放到 Phase 4；Phase 4 prompt 要求「先写新交通权威状态，再回退 Phase 3 重排」，避免 JL085 只停在口头确认。
- P0：Phase 3 worker / orchestrator 支持 `going` / `return` 嵌套交通结构，把航班号、机场、到离港时间下传到到达日 / 离开日。
- P0：`validate_hard_constraints()` 增加交付前硬门：天数唯一覆盖、锁定交通与到达/离开日机场/航班号冲突、显式 pace 超限都会阻断 Phase 3→4。
- P1：`trip_brief.pace` 写入时标准化自然表达（如「不要太赶，每天 3-4 个活动」→ `balanced`），Phase 3 pace mismatch 从 warning 升级为 error。
- P1：`request_backtrack` 的 `next_action` 改为「已获授权则继续修复，否则再确认」，不再诱导 agent 在第 13 轮这种已授权回退场景里停住。

这个案例和前面的 P0-1 / P0-2 是同一类问题的更完整暴露：**用户确认的事实没有进入权威状态，阶段推进又只看字段填充，不看事实一致性，最后 Phase 4 只能发现问题但不能修问题。**
