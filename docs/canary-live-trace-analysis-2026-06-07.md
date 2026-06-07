# 旅行 Agent 活体 Canary（自适应用户 + Trace 审计）分析报告

> 日期：2026-06-07 · 会话：`sess_25e7865827a1` · 范围：Phase 1 → Phase 4 全流程

## 背景与方法

本次评估的目的是用「自适应用户」方式替代原有的固定脚本 canary，对 Phase 1→4 全流程做一次活体评估。所谓自适应用户，是由 Claude 扮演真实用户：每一轮都先读取 agent 的真实输出，再决定下一句要说什么，而不是发送预先写死的固定消息。

- **对照物**：原有脚本 `scripts/run-full-phase-canary.py`。它对每个阶段发送固定的 `message`，并通过 SSE 流统计工具调用。
- **本次工具**：新写了 `scripts/canary_turn.py`，这是一个单轮自适应驱动器——发一条消息，流式接收 agent 的文本、工具调用与 plan 快照；同时修正了工具名提取的 bug（见下文「元发现」一节）。
- **测试会话**：`sess_25e7865827a1`，共 7 轮（即 7 个 trace run）。
- **人设**：上海出发、2 人、3 天东京、预算总计 ≤ 12000 元、住新宿、经济舱直飞、balanced 节奏，偏好拉面 / 甜品 / 药妆 / 街区 / 轻松购物。
- **被测纪律**：一次只推进一个阶段；绝不主动替 agent 往下游跑；只有 agent 真正给出选项之后，才显式确认 / 锁定。

## TL;DR 核心结论

1. **锁定 gate 非常稳**：所有 `select_*` / `set_accommodation` 都只在用户显式授权后才触发，绝不抢跑。
2. **天气护栏是金标准**：`generate_summary` 第一版把未来天气当定论，被工具层硬校验 `FUTURE_WEATHER_NOT_TREATED_AS_EXACT` 打回，改成「临近出发前再确认」后才放行。
3. **真正的病灶是「步内越界」**：Turn 3 用户明说「只写画像、别搜候选」，agent 无视三次，把整条 Phase 2 研究流水线一路跑到 skeleton。
4. **原版 canary 的越界检测是死代码**：它读错了工具名字段，导致 `tool_names` 恒为空，所有越界 / 预算检测从不触发——Turn 3 那种大越界会一路绿灯。
5. **SSE 审计对 Phase 3 几乎全盲**：Run 6 SSE 只看到 2 个工具，trace 真实有 74 个（漏 97%）。Phase 3 的越界 / 预算 / 可靠性只能从 trace 审计。
6. **架构应整体换向**：从「固定 message + SSE 数工具」换成「自适应驱动 + 基于 trace 的不变量检测」，并把已有的 `trace_grader` 挂上。

---

## 一、逐轮行为（turn-by-turn）

| 轮 | 我要求的 | agent 实际行为 | 判定 |
|---|---|---|---|
| 1 | 只收敛目的地、别做下游 | 搜城市对比，给 3 个候选 + 推荐 + 反问；没记死目的地、没建候选池 | 在轨 |
| 2 | 只记东京、别搜 | 单次 `update_trip_basics`，没搜 | 在轨 |
| 3 | 只写 brief、别搜候选 | brief 后一口气 candidate → shortlist → skeleton（28 工具 / 146s / 26 万 token） | **越界（核心问题）** |
| 4 | 给选项、先别锁 | 仅 `web_search` × 3，无 `select_*` | gate 守住 |
| 5 | 锁骨架 + 交通住宿 | `select_skeleton` 成功；交通住宿改调真实 API 重出选项、未锁，让我重挑 | 正确拒锁 |
| 6 | 从真实选项锁定 + 逐日 | `select_transport` + `set_accommodation` 成功 → Phase3 出 3 天 | 守住 |
| 7 | Phase4 交付 + 天气按不确定 | 交付物生成；天气被系统硬校验逼着改后才放行 | 守住 |

## 二、行为层结论

1. **锁定 gate 非常稳**：所有 `select_*` / `set_accommodation` 都只在用户显式授权后才触发；用户说「只给选项别锁」时绝不抢跑。
2. **天气是工具层硬校验**：`generate_summary` 第一版把未来天气当定论，被 `FUTURE_WEATHER_NOT_TREATED_AS_EXACT` 打回，改成「临近出发前再确认」才放行。这是该项目护栏的金标准。
3. **真正的病灶是「步内越界」（Turn 3）**：用户明说「只写画像、别搜候选」，agent 无视三次，把整条 Phase 2 研究流水线跑到 skeleton。结论：**agent 不尊重 Phase 2 内部 step 的「停在这一步」指令。**
4. **一个「对的 desync」印证了固定脚本不可行**：Turn 4 给的选项（ANA/JAL、JR 九州）只是 `web_search` 估算；到 lock 步（Turn 5）才调 `search_flights` / `search_accommodations`，真实选项整组变了（东航 MU523 / 春秋、珍珠酒店），用户预授权的选项根本不存在。agent 没硬锁虚构选项，而是重新摆出真实选项让用户重挑。固定脚本写死「锁去程 A / 住 JR 九州」必然 desync，只有自适应用户能化解。

## 三、元发现：原版 canary 的检测是死代码

原版 `run-full-phase-canary.py` 读 `event.get("name")` / `event.get("tool_name")` 来取工具名，但真实 SSE 事件里：

- 工具名嵌在 `tool_call.name`；
- `tool_result` 不带名、只带 `tool_call_id`。

结果是它的 `tool_names` **恒为空列表**，`forbidden_tool_prefixes` 越界检测和 `tool_budget` 检测**永远不触发**——Turn 3 那种大越界，原版 canary 会一路绿灯。

`canary_turn.py` 已修正：读 `tool_call.name`，并用 `tool_call_id` 把 `tool_result` 回连到工具名。

---

## 四、Trace / session.db 数据分析

### 4.1 规模

- 7 个 run、79 次 `llm_call`；
- 输入 ~1,540,957 token、输出 ~72,399 token；
- 模型墙钟 ~1016s；
- `total_cost_usd` 全为 0（该 provider 未配置计价，canary 报不了成本，只能报 token）。

### 4.2 头号发现：SSE 可见工具数 vs trace 真实工具数

| run | 阶段 | SSE 看到 | trace 真实 |
|---|---|---|---|
| 1 | P1 | 14 | 14 |
| 2 | P2 | 1 | 1 |
| 3 | P2 | 28 | 28 |
| 4 | P2 | 3 | 3 |
| 5 | P2 | 18 | 18 |
| 6 | P3 逐日 | 2 | **74（漏 97%）** |
| 7 | P4 | 14 | 14 |

Run 6（Phase 3）的并行 day-worker 扇出（`get_poi_info` × 23、`calculate_route` × 20、`web_search`、`optimize_day_route`、`submit_day_plan_candidate`）跑在 `phase3_orchestration` 内部任务里，不作为 SSE `tool_call` 外发，只落 trace。

**结论：SSE 驱动的 canary 对 Phase 3 几乎全盲；Phase 3 的越界 / 预算 / 可靠性只能从 trace 审计。**

### 4.3 全会话工具成败统计（关键项）

| 工具 | 成功 / 失败 | 错误码 / 说明 |
|---|---|---|
| `calculate_route` | 11 / 9 | 9 个失败全是 `NO_ROUTE`（~45% 失败率）。逐日交通时长退化成「web 估算」的根因。 |
| `submit_day_plan_candidate` | 3 / 2 | `INVALID_DAYPLAN_TIME_CONFLICT`（day worker 先产出时间冲突排程，被验证层打回重试）。 |
| `generate_summary` | 1 / 2 | `INVALID_ARGUMENTS`、`FUTURE_WEATHER_NOT_TREATED_AS_EXACT`。 |
| `set_skeleton_plans` | 1 / 1 | `INVALID_VALUE`（POI 全局唯一约束重试）。 |
| `web_search` | 33（次） | — |
| `xiaohongshu_read_note` | 20（次） | — |
| `xiaohongshu_search_notes` | 16（次） | — |
| `get_poi_info` | 23（次） | — |

### 4.4 Trace 保真度注意点

- **`phase2_step` 列记录的是 run 的终态，不是每个事件的实时步**：在 run 3 中所有事件的 `phase2_step` 列都标 `"skeleton"`（连开头写 brief 的工具也是）；run 6 中事件 phase 已是 3，但 `phase2_step` 仍停 `"lock"`。因此无法用 step 列看出 brief → skeleton 的步内跃迁，**必须用写工具序列（`set_candidate_pool` → `set_shortlist` → `set_skeleton_plans` 出现在同一 run）重建**。
- **`trace_grades` 表对本会话 0 行**：最近提交的确定性 grader 存在，但没挂到这次 canary 运行上。
- **`trace_events.payload_json` 已带现成抓手**：`side_effect`、`validation_errors`、`state_changes`、`parallel_group`（可把 P3 事件归到具体 day-worker）。canary 可直接消费，不必自己手搓检测。

---

## 五、最终综合判断

### 5.1 模型纪律是「分层」的，且分得挺对

- **有兜底（全程守住）**：不可逆 / 高风险动作（lock、未来天气、日程时间冲突）有工具层硬校验。
- **没人管的三处**：

| 级别 | 问题 | 表现 |
|---|---|---|
| **P0** | Phase 2 步内越界 | 无视「停在这一步」指令，brief 一路跑到 skeleton（Turn 3）。 |
| **P1** | pace 上限无 gate | 只有 `soft_judge` warning、不 gate，导致 Day3 五个活动超 4 漏进交付物。 |
| **P1** | Phase 3 路由可靠性 | 45% `NO_ROUTE`；且锁定状态不回传给 day worker，导致逐日叙述出现 NH972，与锁定的 MU523 不一致。 |

### 5.2 Canary 架构应改向

- **自适应用户**（负责走出真实路径）
- **+ 基于 trace 的不变量检测**：写工具序列查越界、`validation_errors` / 错误码查可靠性、`parallel_group` 查 day-worker。
- **+ 把已有 `trace_grader` 挂上。**
- **审计必须落到 trace 而非 SSE，否则 Phase 3 永远看不见。**

### 5.3 一句话总结

原先对「固定 message + SSE 检测」的两点质疑被数据全部坐实——固定 message 会因晚生成选项 desync；SSE 检测对 P3 全盲（2 vs 74）且原版连字段都读错。canary 想可信，得从「SSE 数工具 + 固定脚本」整个换成「自适应驱动 + trace 审计」。

---

## 六、建议的后续动作

| 编号 | 动作 |
|---|---|
| **A** | 把工具名字段修正回灌原版 `run-full-phase-canary.py`，并让 violations 真正 gate 退出码。 |
| **B** | 新建一个基于 `trace_events` 的审计基线（越界序列、错误率、pace 报告），替代基于 SSE 的检测。 |
| **C** | 把 `trace_grader` 自动挂到 canary 运行上。 |
