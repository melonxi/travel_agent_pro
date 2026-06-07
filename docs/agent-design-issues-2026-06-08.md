# 旅行 Agent 设计问题分析（问题 / 证据 / 解决方案）

> 数据来源：活体 canary 会话 `sess_25e7865827a1`（Phase 1→4 全程，7 个 trace run）的 `trace_events` / `session.db` 与最终交付物（`travel_plan.md` + `checklist.md`）。
>
> 与 `canary-live-trace-analysis-2026-06-07.md` 的分工：那份讲「测试方法该怎么改」（自适应驱动 + trace 审计）；**本份讲「agent 本身该怎么修」**。
>
> 严重度分级：**P0**（影响交付物正确性 / 可信度）＞ **P1**（用户控制力 / 显式约束）＞ **P2**（成本 / 效率 / 可观测）。

## TL;DR

agent 的纪律目前靠**少数工具层硬校验点**撑着（lock、未来天气不得当定论、日程时间冲突）——这几处全程守住；而**没有硬校验兜底的地方全部失守**：锁定态不下传 Phase 3、跨阶段事实不收敛、核心路由能力近半失败被静默降级、Phase 2 步内不可控、显式 pace 约束只 warn 不 gate。本质问题是一个**「描述性」而非「控制性」的阶段机**——phase/step 大多只记录「最后停在哪」，而不约束「过程做什么」。

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
- `check_weather` 在 Phase 3、Phase 4 被独立调用，且返回值不可靠（7 月东京报 18°C，明显失真，被 Phase 4 用常识纠正为 28.7°C，但纠正未回灌 Phase 3 已生成内容）。

**解决方案**：
1. **天气单一事实源**：整个会话只解析一次天气，写入 plan 状态（含「精确预报不可用 / 季节均值」标注）；Phase 3 day worker 与 Phase 4 summary **共读同一来源**，禁止各自独立 `check_weather`。
2. **`check_weather` 数据质量**：无精确预报时返回**季节气候均值**并显式标注不确定，而不是返回一个失真的「确定值」（18°C）让下游误用。
3. **交付前一致性 hook**：扩展现有实时校验 hook，交付前交叉核对最终 markdown 与 plan 状态——天气、锁定的交通/住宿、关键数值（预算、日期）若与权威状态矛盾则拦截重写。

---

## P0-3 核心能力 `calculate_route` 近半失败且被静默降级

**问题**：地理路由（产品主打的「动线效率 / 地理可行性」）在 Phase 3 近半失败，系统静默退化为 web 估算，把**没真正算出来的通勤时长当成计划呈现**，且只在交付物里埋一句小字 caveat。

**证据**：
- trace 全会话 `calculate_route`：**11 成功 / 9 失败，9 个失败全是 `NO_ROUTE`（~45%）**。
- `backend/tools/calculate_route.py`：默认 `mode="transit"`，但请求参数（第 121-126 行）**只有 origin/destination/mode/key，缺 `departure_time`**。Google Directions 的 transit 模式**没有出发时间常直接返回 ZERO_RESULTS → NO_ROUTE**；短距离 transit 也常无解（本应 walking）。
- 工具把错误抛回、靠 LLM 自行换 mode（`_STATUS_ERROR_MAP` 的 suggestion 也这么写），但 day worker 往往不换、直接退化估算（`_ROUTE_UNAVAILABLE_PROMPT`）。
- 结果：交付物 Day2「交通时长为保守估算（路线工具未返回可用结果，基于 web 搜索估算）」。

**解决方案**：
1. **transit 请求带 `departure_time`**（用计划中的活动时间，或保守取「下一个工作日同时段」），消除「无出发时间→ZERO_RESULTS」这一大类失败。
2. **工具内置 mode 回退链**：transit→ZERO_RESULTS 时，对短距离自动改 `walking` 重试再返回，而不是把回退责任丢给 LLM。
3. **坐标前置校验**：调用前确保经过 `get_poi_info` 拿到可靠坐标（减少 `NOT_FOUND`）。
4. **估算必须显著标注**：退化为估算时在 activity 上打结构化标记（非自由文本小字），交付层统一渲染为可见提示。

---

## P1-4 Phase 2 步内不可控（摁不住）

**问题**：用户明确要求「只做到某一步、先别往下」时，agent 无视并一口气把 Phase 2 研究流水线跑到底——用户对中间过程**零控制力**。这对一个主打「人在环、显式确认」的产品是内部矛盾。

**证据**：
- Turn 3：用户说「只把这些写进画像就行，先别去搜候选」，agent 在同一轮跑完 brief→candidate→shortlist→skeleton。
- trace `run3` 工具序列：`set_candidate_pool`(#23) → `set_shortlist`(#28) → `set_skeleton_plans`(#38 err→#40 ok)，全在一个 run。
- 对照：**lock 那条线做到了 gate**（select_* 仅在显式授权后触发），但**研究/构建这条线完全没 gate**——状态机靠 agent 动量推进。

**解决方案**：
1. **step 级停止点**：识别用户「只到这一步 / 先别往下」的意图，在 phase2 step 边界插入显式停止，等用户确认再推进（把 lock 的确认模式推广到研究/构建步）。
2. 或在 orchestration hook 层对 `set_candidate_pool/set_shortlist/set_skeleton_plans` 加**前置确认门**：当本轮用户消息含「停在此步」信号时，拦截下游写工具并要求确认。

---

## P1-5 体验质量只 warn 不 gate（显式约束被违反进交付物）

**问题**：结构合法性（时间冲突）是硬门，但**体验质量（节奏 pace，恰恰是用户点名的约束）只产生 soft_judge warning**、无否决权，最终违反用户显式约束的内容直接进交付物。

**证据**：
- trace：`submit_day_plan_candidate` 的 `INVALID_DAYPLAN_TIME_CONFLICT` 是**硬 gate**（2 次被打回重试）；而 pace 超限只有 soft_judge `warning`。
- `backend/harness/judge.py` 的 `SoftScore`（pace/geography/coherence/personalization 各 1-5）**没有否决路径**，只是评分+建议。
- 结果：用户明确要 balanced / 不要太密，**Day3 仍排了 5 个活动、超 balanced 上限 4**，warning 之后照样进了最终方案。

**解决方案**：
1. 把**「用户显式约束」类质量升级为硬门**：pace 超 balanced 上限时触发 day worker 重排或 orchestrator 修补回路，而非仅 warning。
2. soft_judge 低于阈值（如 `QualityGateConfig.threshold`）时，对**用户显式点名的维度**阻断交付 / 触发修复，与时间冲突同等对待。

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

## P2-7 成本不可见 + 自动评分未挂

**问题**：runtime 拿不到成本信号，无法预算/止损；已有的确定性 grader 没接到主流程，质量回归无人值守。

**证据**：
- trace_runs 的 `total_cost_usd` **全为 0**（该 provider 未配置计价；token 维度正常：79 llm_call / 154 万输入 / 7.2 万输出）。
- `trace_grades` 表对本会话 **0 行**（确定性 grader 存在但未挂到运行上）。

**解决方案**：
1. 为所用 provider/model 配置计价，使每个 llm_call 的 `cost_usd` 真实可算，runtime 能做预算/止损。
2. 把 `trace_grader` 自动挂到 canary / CI 运行后，对每个会话产出分数与回归基线。

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
