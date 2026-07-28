# Phase 3 并行完成后自动进 Phase 4 却未交付双文档事故复盘

- 事故日期：2026-07-17
- 问题 session：`sess_27454b1d11fc`（北京 · 7天6晚）
- 事故范围：
  - `backend/agent/loop.py`（并行 Phase 3 入口在 orchestrator 结束后裸 `return`）
  - `backend/agent/phase3/*`（并行编排 + `replace_all_day_plans` 提交）
  - `backend/agent/execution/phase_transition.py` / phase gate（写完 `daily_plans` 后自动 3→4）
  - Phase 4 交付工具 `generate_summary` 与天气硬门（次要延迟）
- 事故类型：阶段自动推进与交付物生成路径断裂 + 并行 Phase 3 短路退出
- 事故等级：高（核心交付物路径失败，用户需主动催促才完成产品终点）
- 事故状态：已修复并补充回归测试

---

## 1. 事故摘要

在一次完整的北京 7 日规划中，用户完成交通/住宿锁定后，系统进入 **并行 Phase 3**，成功生成并提交全部 7 天 `daily_plans`，质量闸门允许 **3→4**，trace 也记录了 `phase_transition` 与 `context_rebuild`。

但该 run 在进入 Phase 4 后**立即 `run_end`**，同一轮内：

- **没有**调用 `generate_summary`
- **没有** `deliverable_draft` / `deliverable_finalize`
- 磁盘与 plan 上**没有**冻结的 `travel_plan.md` / `checklist.md`

用户随后发送「为什么没有看见文档」，才触发下一轮 Phase 4 查漏与交付。第二次 run 中第一次 `generate_summary` 又被天气表述硬门拦住一次，改写后才成功冻结。

**一句话：阶段被自动推到了 4，但「冻结双文档」没有在同一条 Agent Loop 路径上被执行。**

---

## 2. 用户可见现象

1. Phase 指示器 / plan 状态显示已到 **出发前查漏（Phase 4）**。
2. 逐日行程在右侧 Plan / 状态里已存在（7 天齐全）。
3. **右侧交付物入口 / 下载文件不可用或为空**（本轮未生成）。
4. 对话在 Phase 3 提交后自然结束，assistant **没有**主动说明「开始生成行程书与清单」。
5. 用户必须**主动追问**「为什么没有看见文档」后，系统才开始查天气、搜资料并生成两份 markdown。

---

## 3. 影响评估

### 3.1 用户影响

- 产品终点（双 markdown 交付）在「阶段已完成」的表象下缺失，信任成本高。
- 用户被迫做系统本应主动完成的闭环提示。
- 即使催促后，天气硬门再失败一次会二次拖长等待。

### 3.2 系统影响

- **Human-Agent Loop 与业务状态机语义不一致**：`phase=4` 不蕴含「交付已完成」。
- 并行 Phase 3 成功路径变成「写完行程就下班」，Phase 4 完全依赖用户下一轮消息。
- Trace 上 run 状态为 `completed` 且无 error，可观测性「看起来健康」，实则交付缺口。

### 3.3 影响面

- 凡走 **并行 Phase 3 orchestrator** 且写完 `daily_plans` 后 **自动 3→4** 的 session，均可能复现。
- 串行 Phase 3 若仍在同一 loop 内继续 LLM 回合，风险形态可能不同（本报告以并行路径实锤为准）。

---

## 4. 证据

### 4.1 Session / Plan

| 字段 | 值 |
|------|-----|
| `session_id` | `sess_27454b1d11fc` |
| `title` | 北京 · 7天6晚 |
| 最终 `phase` | 4 |
| `dates` | 2026-07-18 → 2026-07-24（7 天） |
| `daily_plans` | 7 天齐全 |
| 最终 `deliverables` | 有（`generated_at=2026-07-17T09:51:50.996250+00:00`，在用户催促**之后**） |
| 最终 `status` | archived |

### 4.2 Trace runs（关键两轮）

| run_id | started_at (UTC) | ended_at | final_phase | 说明 |
|--------|------------------|----------|-------------|------|
| `fc5d5f56-e55e-4308-8fe6-cedd31f5e327` | 09:46:27 | 09:49:33 | 4 | 锁住宿 + 并行 Phase3 + **自动 3→4 后立即结束**；**无交付事件** |
| `5e5a6f3f-7bd5-4c8c-8f5d-0dacebf0397c` | 09:50:23 | 09:51:57 | 4 | 用户催促后：查漏 + `generate_summary`（失败一次后成功）+ finalize |

### 4.3 第一轮 run（`fc5d5f56…`）末尾事件序

| sequence | phase | event_type | 要点 |
|----------|-------|------------|------|
| 360 | 3 | `phase3_orchestrator` handoff | 7 dayplans 就绪 |
| 361–363 | 3 | `replace_all_day_plans` + state_diff | 提交成功 |
| 364–365 | 3 | `soft_judge` | overall 4.5，warning（有建议，不挡提交） |
| 366 | 3 | `phase_gate` | **allowed** 3→4 |
| 367–368 | 3 | `quality_gate` | pending → success（4.0，allow） |
| 369 | 3 | `phase_transition` | **from 3 → to 4** |
| 370 | 3 | `context_rebuild` | epoch 5→6，`phase_forward` |
| 371 | 4 | `state_snapshot` | scope=`run_end` |
| 372 | 4 | `run_end` | **completed** |

**该 run 在 phase=4 上几乎只有 snapshot + run_end。**  
全 session 统计：`deliverable_draft` / `deliverable_finalize` 各 1 次，全部落在第二轮 run。

### 4.4 消息序（SQLite `messages`）

| seq | phase | role | 内容（截断） |
|-----|-------|------|----------------|
| 118 | 2 | user | `1`（选住宿） |
| 119–120 | 2 | assistant/tool | `set_accommodation` 成功 |
| 121–123 | 3 | assistant/tool | 阶段交接 + 并行提交 `replace_all_day_plans` 成功，7 天 |
| **124** | **4** | **user** | **`为什么没有看见文档`** |
| 125+ | 4 | assistant/tool | 才开始 `check_weather` / `web_search` / `generate_summary` |

说明：进入 Phase 4 后的**第一条用户消息就是催促**，中间没有「系统已主动交付」的 assistant 回合。

### 4.5 第二轮 run 中交付与次要失败

| sequence | event | 结果 |
|----------|-------|------|
| … | 多次 `check_weather` / `web_search` | success |
| … | `generate_summary` #1 | **error** `FUTURE_WEATHER_NOT_TREATED_AS_EXACT` |
| … | `validation` | fail：近似参考天气不得写成确定预报 |
| … | `generate_summary` #2 | success |
| … | `deliverable_draft` → `soft_judge` → `deliverable_finalize` | 双文档冻结 |

天气硬门属于**二次延迟**，不是「第一轮完全没交付」的根因。

---

## 5. 根因分析

### 5.1 直接根因：并行 Phase 3 路径在 orchestrator 结束后整轮 `return`

`backend/agent/loop.py` 主循环顶部：

```python
if should_enter_parallel_phase3_now(
    self.plan,
    self.phase3_parallel_config,
    user_message=original_user_message,
):
    async for chunk in self._run_parallel_phase3_orchestrator(
        messages=messages,
        original_user_message=original_user_message,
    ):
        yield chunk
    return  # ← 整轮 Agent Loop 结束
```

含义：

1. 一旦判定「现在应跑并行 Phase 3」，本轮**只**跑 orchestrator。
2. Orchestrator 内部完成 worker → 全局校验 → `replace_all_day_plans` →（可能）phase gate / quality gate / **3→4 转换与 rebuild**。
3. 控制流回到主 loop 后 **`return`**，**不会**再进入 Phase 4 的 `run_llm_turn`。
4. 因此 `generate_summary` 没有调用者，除非用户再开一轮 Human-Agent Loop。

### 5.2 促成条件：写完 daily_plans 后自动 3→4

第一轮末尾：

- `phase_gate` reason=`check_and_apply_transition`，`allowed=true`
- `quality_gate` 3→4 评分 4.0，`final_action=allow`
- `phase_transition` 已落库

业务上「可以进 Phase 4」被执行了，但 Phase 4 的**交付义务**没有绑定到同轮执行器。

### 5.3 语义错位

| 系统实际 | 用户/产品期望 |
|----------|----------------|
| `phase=4` = 状态机已前进 | Phase 4 ≈ 查漏 + **主动交付** 两份文档 |
| run `completed` 且无 error | 规划闭环应给出可下载产物 |
| 交付依赖下一轮 LLM 自觉点工具 | 进入 4 后应确定性或强约束推进 `generate_summary` |

### 5.4 次要因素（用户催促之后）

- 模型在 Phase 4 先做大量探索（天气、搜索），再交付。
- `check_weather` 对远期日期返回 `openweather_nearest_reference` / `exact_date_available=false` 时，交付物若写成「确定天气」会被硬门拒绝（设计正确，但首次生成易踩坑）。

### 5.5 非根因

- 不是 SQLite / 前端未刷新导致「看不见文件」：第一轮结束时 deliverables 目录与 plan 字段确实尚未生成。
- 不是 quality_gate 拒绝进 4：闸门明确 allow。
- 不是 soft_judge 阻断提交：soft_judge 为 warning，daily_plans 已写入。

---

## 6. 为何监控/Trace 不易一眼看出

| 信号 | 表现 | 误导 |
|------|------|------|
| `run_end` status | `completed` | 像正常收工 |
| `final_phase` | 4 | 像业务已完成 |
| `phase_transition` | 有 3→4 | 像流水线顺畅 |
| `deliverable_*` 事件 | 本 run 为 0 | 需跨 run 对比才发现缺口 |
| `last_run_error` | null | 无失败告警 |

建议的可观测补强（修复时一并考虑）：

- run_end 时若 `phase>=4` 且 `deliverables` 为空 → 记 `warning` / 专用 event（如 `deliverable_gap`）。
- 或 quality 面板展示「Phase 4 但未 finalize」。

---

## 7. 时间线（摘要）

```text
用户锁定住宿
  → run fc5d5f56：Phase2 写 accommodation
  → 自动 2→3
  → 并行 Phase3 workers 完成 7 天
  → replace_all_day_plans 成功
  → soft_judge / quality_gate 允许 3→4
  → phase_transition + context_rebuild
  → ★ loop return，无 Phase4 LLM，无 generate_summary
  → 用户感知：到了第四阶段，但没有两份文档

用户：「为什么没有看见文档」
  → run 5e5a6f3f：Phase4 查漏
  → generate_summary #1 被天气硬门拒绝
  → generate_summary #2 成功并 finalize
  → 双文档落地，session 可归档
```

---

## 8. 修复实施

已采用方案 A：并行 Phase 3 完成后，AgentLoop 会区分子流程 `DONE` 与整轮 `DONE`。若提交已触发 3→4 且交付物尚未冻结，则使用 phase transition 返回的重建消息和 Phase 4 工具继续同一 run；若仍停留 Phase 3，则正常结束，保留部分交付和失败提示语义。

同时补充同一 run 进入 Phase 4 并生成交付物的回归测试；run 结束在 Phase 4 但交付物仍为空时记录 `deliverable_gap` warning。

### 原评估方案

按侵入性从低到高：

### 方案 A — Loop 续跑（推荐优先评估）

并行 orchestrator 返回后：

- **不要**无条件 `return`。
- 若 `plan.phase == 4` 且 deliverables 未冻结，**继续**主 loop 至少一轮 Phase 4（可用 tool_choice / runtime notice 强制 `generate_summary`）。

### 方案 B — 确定性交付钩子

`replace_all_day_plans` 成功且 3→4 允许后，由 harness **直接调度**交付流水线（或 internal task），不依赖模型「想起来点工具」。

### 方案 C — 产品语义改闸门

禁止「无交付就自动标 phase=4」：

- 3 完成后停在「待确认 / 待交付」
- UI 明确「生成行程书与清单」动作
- 用户确认后再进 4 并生成

### 方案 D — 体验兜底（不治本）

- 3→4 后若无文档，前端固定文案：「行程已排完，回复任意内容开始生成出发前文档」或自动发 continue。
- 仅缓解「静默结束」，不消除多一轮成本。

**建议组合：A 或 B 治本 + 可观测补强；C 若产品希望强确认再交付。**

### 次要跟进

- Phase 4 prompt / 工具说明强化：远期天气必须标「参考/非精确」。
- 评估 soft_judge 建议是否应在交付前强制展示给用户（本次非主因）。

---

## 9. 验证清单（修复后）

1. 复现路径：并行 Phase 3 全天覆盖 → 确认 **同一 run** 内出现 `generate_summary` 成功 + `deliverable_finalize`，或明确的用户确认门闩（方案 C）。
2. Trace：`phase_transition` 3→4 之后，在**同一 `run_id`** 内应有交付事件，或显式 `deliverable_gap` + 非静默 `completed`。
3. 用户路径：不发送「为什么没有文档」也能拿到两份 md。
4. 回归：用户在 Phase 3 明确「先等等 / 先改」时，defer 并行逻辑仍生效。
5. 天气硬门：含 `exact_date_available=false` 的样例首次生成应通过或给出一次可恢复错误后成功。

---

## 10. 结论

| 项 | 判定 |
|----|------|
| 是否数据丢失 | 否；行程状态完整，缺的是交付物生成步骤 |
| 是否模型单点失误 | **否**（第一轮根本没有 Phase 4 工具回合）；第二轮天气表述是次要问题 |
| 是否架构/循环缺陷 | **是**：并行 Phase 3 短路 `return` + 自动 3→4 未绑定交付 |
| 当前状态 | 已采用方案 A 并上线；同一 run 会续跑到 Phase 4 生成交付物，且已补回归测试与 `deliverable_gap` 告警。 |

---

## 11. 参考

- Trace DB：`data/sessions.db` → `trace_runs` / `trace_events`，`session_id=sess_27454b1d11fc`
- 关键 run：`fc5d5f56-e55e-4308-8fe6-cedd31f5e327`（缺口）、`5e5a6f3f-7bd5-4c8c-8f5d-0dacebf0397c`（补交付）
- Plan / 交付物：`data/sessions/sess_27454b1d11fc/plan.json`、`.../deliverables/*.md`
- 代码：`backend/agent/loop.py` 并行 Phase 3 分支；`detect_phase_transition` / quality gate 3→4
