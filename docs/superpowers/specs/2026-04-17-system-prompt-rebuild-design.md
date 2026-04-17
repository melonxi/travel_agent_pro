# System Prompt Rebuild & Runtime Context Injection — Design Spec

> Scope 日期：2026-04-17
> 分支：`system-prompt-rebuild-fix`
> 前置背景：`docs/superpowers/specs/2026-04-17-phase-handoff-redesign-design.md`

## 1. 问题陈述

在上一轮 phase handoff 重构完成后，通过对 `backend/agent/loop.py`、`backend/context/manager.py`、`backend/phase/router.py` 代码复核，发现系统提示词（system message）的重建与各阶段 runtime context 注入存在以下缺陷：

### P0（必修）
**缺陷 A — Phase 3 子阶段切换不重建 system message**
- 位置：`backend/agent/loop.py:496-506`
- 现象：当 `sync_phase_state` 把 `plan.phase3_step` 从 `brief → candidate → skeleton → lock` 推进时，循环只刷新了 tools（`get_tools_for_phase`），没有重建 system message。
- 影响：模型在下一轮推理时看到的仍是旧子阶段 runtime context，行为会与当前子阶段提示词不一致，可能继续调用不该在新子阶段用的逻辑、或看不到刚沉淀的 shortlist / skeleton_plans。

**缺陷 B — Phase 7（出发前查漏）不展示 daily_plans 详情**
- 位置：`backend/context/manager.py:247-260`
- 现象：`if plan.phase == 5:` 才展开每天活动；Phase 7 只看到 "已规划 N/N 天" 这样的汇总。
- 影响：Phase 7 是出发前个性化 Checklist 阶段，缺行程细节就无法对"带什么、提前预约哪些"给出有针对性的建议，体验下降。

### P1（一并修）
**缺陷 C — Phase 3 skeleton 子阶段只展示骨架数量**
- 位置：`backend/context/manager.py:191-211`
- 现象：只有 `phase==3 && phase3_step=="lock" && selected_skeleton_id` 才展开已选骨架内容；`skeleton` 子阶段 LLM 只能看到"骨架方案：N 套"。
- 影响：LLM 在 skeleton 推荐/解释阶段要反复通过工具回看自己刚写的 skeleton_plans，token 浪费且易出错。

**缺陷 D — Phase 1 不注入 preferences / constraints**
- 位置：`backend/context/manager.py:228-241`
- 现象：注入条件是 `phase >= 5 or (phase==3 && step in {skeleton, lock})`。Phase 1、Phase 2、Phase 3 前期子阶段均不注入。
- 影响：Phase 1 基础信息阶段用户表达的偏好/约束在当轮之后不进入 system context，跨轮次被遗忘的概率增加。

### 非目标（明确不做）
- **不**按阶段拆分 `ContextManager.build_runtime_context` 为多个 builder（架构层重构）。
- **不**更改 phase prompt 文本（仅改 runtime context 与重建时机）。
- **不**重构 `sync_phase_state` / `phase3_step` 推断逻辑。

## 2. 设计

### 2.1 Phase 3 子阶段变化触发 system message 重建

在 `AgentLoop` 中新增轻量重建路径 `_rebuild_messages_for_phase3_step_change`：
- 输入：现有 messages、`original_user_message`、变化前后的 phase3_step。
- 行为：
  1. 调用 `context_manager.build_system_message(...)` 生成新 system message（会带最新的 runtime context，包含新子阶段下该展开的字段）。
  2. **不**插入 handoff_note（handoff_note 是跨 phase 的用户可见"已完成 Phase N，进入 N+1"语义，子阶段切换不适合这个文案，容易误导 LLM/用户）。
  3. **不**插入 backtrack_notice（子阶段只会向前推进）。
  4. 保留 `original_user_message`（语义：当前用户意图还没闭环）。
  5. 返回的消息列表构成：`[new_system_message, original_user_message]`。
- 在 `loop.py:496-506` 处，当 `phase3_step_after_batch != phase3_step_before_batch` 时调用该方法替换 messages，再刷新 tools。

**复用决策**：不复用 `_rebuild_messages_for_phase_change`。原因：该方法语义绑定 from→to phase 切换，调用它需要制造一个 `from_phase == to_phase` 的伪场景并在内部分支剔除 handoff_note；新增独立方法更直接、测试更易覆盖。

### 2.2 扩展 runtime context 注入条件

统一原则：注入条件基于 phase/phase3_step 的"语义需要"决定，而非"历史延续"。

具体调整 `build_runtime_context`：

| 字段 | 当前条件 | 新条件 | 理由 |
|------|---------|--------|------|
| `daily_plans` 每日活动展开 | `phase == 5` | `phase in (5, 7)` | Phase 7 查漏需要看每日行程 |
| `skeleton_plans` 详情展开 | `phase>=5 && selected_skeleton_id` 或 `phase==3 && step==lock` | 上述条件 **OR** `phase==3 && step in {skeleton, lock}` 时展开**紧凑摘要**（每套仅展示 id/name/tradeoffs + 每天 theme） | skeleton 子阶段 LLM 需看到选项内容；lock 已展开完整 selected 方案，skeleton 用紧凑摘要避免 token 爆炸 |
| `preferences` / `constraints` | `phase>=5 || (phase==3 && step in {skeleton, lock})` | **一律注入**（若存在） | 用户偏好/约束在任何阶段都有价值；Phase 1/2/3 前期的遗忘问题直接消除；体积小（典型几条），token 压力可忽略 |

#### skeleton 紧凑摘要格式（新增）
```
- 骨架方案：N 套
  - [id=skeleton_01] 名称：经典版 | 权衡：稳妥，适合首访
    - D1: 老城徒步
    - D2: 峡谷日游
    - ...
  - [id=skeleton_02] ...
```
字段来源：每个 skeleton dict 的 `id`/`name`/`tradeoffs` + `days[].theme`（若无 theme 则退化为 `days[].title` 或 `第N天`）。

### 2.3 loop.py 中 phase3_step 变化检测点

新增 step 变化检测时机与 phase 变化对齐：
- 循环顶部已记录 `phase3_step_before_batch`。
- 现有代码在工具批次结束后比对；本 spec 要求在此处补调 system 重建。
- phase 整体切换已由上方 `_rebuild_messages_for_phase_change` 处理（并会在其中内嵌正确的新 system message），故 phase 切换路径不受影响。

## 3. 测试策略

仅新增/调整与本 spec 直接相关的 pytest，不跑全量。

### 3.1 新增/扩展测试

`backend/tests/test_context_manager.py`：
- `test_runtime_context_phase7_expands_daily_plans` — phase=7 + daily_plans 非空 → 文本包含每日活动名。
- `test_runtime_context_skeleton_step_shows_summary` — phase=3 / step=skeleton / skeleton_plans 两套 → 文本包含两套 id 与 tradeoffs，每套至少一行 theme。
- `test_runtime_context_preferences_injected_phase1` — phase=1 / preferences 非空 → 文本含 `用户偏好`。
- `test_runtime_context_constraints_injected_phase1` — phase=1 / constraints 非空 → 文本含 `用户约束`。
- `test_runtime_context_skeleton_lock_still_shows_selected_full` — phase=3 / step=lock → 既展开 selected 完整内容，skeleton 列表不再重复展开详情（避免回归）。

`backend/tests/test_agent_loop.py`：
- `test_phase3_step_change_rebuilds_system_message` — 构造一个 plan 从 `brief→candidate`（通过 mock phase3_step 变化），断言重建后的 messages[0] 是 SYSTEM 且内容随子阶段变化而变化（例如 candidate 子阶段展开了 trip_brief）。
- `test_phase3_step_change_no_handoff_note` — 断言重建后的 messages 不含跨 phase handoff_note 文本关键字（如"已完成 Phase"）。

### 3.2 回归验证范围
仅跑以下文件：
```
pytest backend/tests/test_context_manager.py \
       backend/tests/test_agent_loop.py \
       backend/tests/test_phase_transition_event.py -q
```
基线：75 passed（已确认）。

## 4. 风险与缓解
| 风险 | 缓解 |
|------|------|
| preferences/constraints 一律注入 → 早期阶段 token 上升 | 典型条数 <10，纯文本每条 <60 字符，上限增加约数百 token，可接受 |
| skeleton 紧凑摘要字段名不稳定（`name`/`title`/`theme`） | 用 getter 兜底（`name or title or str(d)[:60]`），沿用 shortlist 已有写法 |
| phase3_step 重建与 phase 切换同批次发生 | loop 现有顺序：先做 phase 切换 rebuild 并 `continue`，phase3_step 检测在其后；phase 切换路径 rebuild 已含新 system，不会重复 |

## 5. 验收
- [ ] loop.py:496-506 处调用 `_rebuild_messages_for_phase3_step_change` 并替换 messages
- [ ] `build_runtime_context` 按表格更新条件
- [ ] 新增 7 条针对性测试全部通过
- [ ] `test_context_manager.py` + `test_agent_loop.py` + `test_phase_transition_event.py` 全绿，数量 ≥ 75+7
