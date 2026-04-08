# Phase 5 骨架注入修复：逐日细化不稳定问题根因分析与解决

修复时间：2026-04-08
修复范围：`backend/context/manager.py`、`backend/agent/loop.py`、`backend/phase/prompts.py`

## 背景

Phase 5（逐日行程落地与验证）在对 Phase 3 构建的旅行骨架进行逐天细化时，表现出严重的不稳定性。具体现象包括：

- LLM 生成的逐日行程与用户在 Phase 3 选定的骨架方案不一致
- LLM 经常只在自然语言中描述行程，但不调用 `update_plan_state` 写入结构化 `daily_plans`
- 重试循环频繁触发，消耗 token 但无法稳定推进
- 有时直接跳过 expand 步骤，生成与骨架无关的「通用行程」

## Phase 5 的设计职责

Phase 5 是「最后一公里规划器」，核心职责是将 Phase 3 产出的已选骨架方案（skeleton）展开为覆盖全部日期的 `daily_plans`。工作流程为四步：

1. **expand**：把骨架中的「区域/主题/核心体验」映射到每一天
2. **assemble**：逐天组装活动顺序与时间段，生成结构化 `DayPlan`
3. **validate**：检查开放性、交通、天气、预算与节奏
4. **commit**：把完整 `daily_plans` 写入状态

阶段判定条件：`len(plan.daily_plans) < plan.dates.total_days` 则停留在 Phase 5。

## 根因分析

### 核心问题：Phase 5 的 LLM 看不到它需要展开的骨架内容

当 Phase 3 → Phase 5 切换发生时，LLM 的上下文中存在严重的信息缺失：

| 信息 | 切换前（Phase 3 LLM 能看到） | 切换后（Phase 5 LLM 能看到） | 缺失影响 |
|------|------|------|------|
| 已选骨架内容 | 完整的骨架方案数据结构 | 仅 `已选骨架：plan_A`（ID） | **致命**：Phase 5 无法知道骨架安排了什么 |
| 旅行画像 | 完整的 trip_brief 字段 | 仅 `已生成旅行画像：5 项`（计数） | 无法根据画像调整行程节奏 |
| 用户偏好/约束 | 在对话历史中可见 | 被 compress_for_transition 截断 | 可能忽视「不坐红眼航班」等约束 |
| 已完成天数详情 | N/A | 仅 `已规划 1/5 天`（计数） | 增量生成时不知道已排过什么 |

### 详细链路分析

#### 1. `build_runtime_context()` 只输出计数

```python
# 修复前
if plan.skeleton_plans:
    parts.append(f"- 骨架方案：{len(plan.skeleton_plans)} 套")
if plan.selected_skeleton_id:
    parts.append(f"- 已选骨架：{plan.selected_skeleton_id}")
if plan.trip_brief:
    parts.append(f"- 已生成旅行画像：{len(plan.trip_brief)} 项")
```

Phase 5 的 LLM 只能看到「有 3 套骨架方案，选了 plan_A」，完全不知道 plan_A 里安排了什么区域、什么景点、什么主题。

#### 2. `compress_for_transition()` 截断关键数据

Phase 3 → 5 的转场摘要使用 `_short_repr()` 将工具调用数据截断到 160 字符。skeleton_plans 这种大型结构化数据被截断后不可用。

#### 3. 缺乏 Phase 5 专用的 state repair 机制

Phase 3 有 `_build_phase3_state_repair_message`，当 LLM 只输出文本不写状态时自动提醒。Phase 5 完全没有这个机制，导致 LLM 在自然语言中描述了每天的行程后就「以为完成了」，不再调用 `update_plan_state` 写入结构化数据。

## 修复方案

### 1. 骨架内容注入（`backend/context/manager.py`）

当 `phase >= 5` 时，`build_runtime_context()` 自动注入已选骨架的完整内容：

```python
# 修复后
if plan.phase >= 5 and plan.selected_skeleton_id:
    selected = self._find_selected_skeleton(plan)
    if selected:
        parts.append(f"- 已选骨架方案（{plan.selected_skeleton_id}）：")
        for key, val in selected.items():
            if key == "id":
                continue
            parts.append(f"  - {key}: {val}")
```

新增 `_find_selected_skeleton()` 方法支持精确匹配和模糊匹配：
- 优先按 `id` 字段精确匹配
- 退化到 `name` 字段匹配
- 最后尝试部分字符串匹配（容错 LLM 可能写的 `planA` vs `plan_A`）

### 2. 旅行画像内容注入

```python
# 修复后：Phase 5+ 输出具体字段，Phase 3 保持计数
if plan.trip_brief:
    if plan.phase >= 5:
        parts.append("- 旅行画像：")
        for key, val in plan.trip_brief.items():
            parts.append(f"  - {key}: {val}")
    else:
        parts.append(f"- 已生成旅行画像：{len(plan.trip_brief)} 项")
```

### 3. 偏好/约束/已完成天数注入

```python
# Phase 5+: 显示具体偏好和约束内容
if plan.preferences and plan.phase >= 5:
    pref_strs = [f"{p.key}: {p.value}" for p in plan.preferences]
    parts.append(f"- 用户偏好：{'; '.join(pref_strs)}")

# Phase 5: 显示已完成天数的摘要和待规划天数
if plan.phase == 5:
    for dp in plan.daily_plans:
        act_names = [a.name for a in dp.activities[:5]]
        parts.append(f"  - 第{dp.day}天（{dp.date}）：{'、'.join(act_names)}")
    # 显示待规划天数列表
    missing = [d for d in range(1, total_days + 1) if d not in planned_days]
    parts.append(f"  - 待规划天数：{', '.join(map(str, missing))}")
```

### 4. Phase 5 State Repair（`backend/agent/loop.py`）

新增 `_build_phase5_state_repair_message()` 方法：

- 检测 Phase 5 LLM 输出中是否包含逐日行程文本（通过正则匹配「第N天」「Day N」「HH:MM」等模式）
- 如果检测到行程文本但 `daily_plans` 仍不完整，注入提醒消息
- 提醒 LLM 必须调用 `update_plan_state(field="daily_plans", value=...)` 写入结构化数据
- 与 Phase 3 repair 共享同一防重入机制（`repair_hints_used`）

### 5. Phase 5 Prompt 增强（`backend/phase/prompts.py`）

新增三个关键段落：

1. **「关键：你的输入来源」**：明确告知 LLM 骨架内容、画像、偏好已注入到运行时状态中
2. **「增量生成」指引**：告知 LLM 如何处理部分已有 daily_plans 的场景
3. **「最重要的提醒」**：强调不能只输出文本不写状态，鼓励一次性 list[dict] 提交

## 测试覆盖

### 新增测试（`test_context_manager.py`）

| 测试 | 覆盖点 |
|------|--------|
| `test_phase5_runtime_context_injects_selected_skeleton` | 验证选中骨架完整内容注入 |
| `test_phase5_runtime_context_injects_trip_brief_content` | 验证旅行画像字段级注入 |
| `test_phase5_runtime_context_injects_preferences_and_constraints` | 验证偏好/约束注入 |
| `test_phase5_runtime_context_shows_daily_plans_progress` | 验证已完成天数摘要和待规划天数 |
| `test_phase3_runtime_context_shows_count_only` | 回归：Phase 3 不受影响 |
| `test_find_selected_skeleton_by_id` | 精确匹配 |
| `test_find_selected_skeleton_fallback_partial_match` | 模糊匹配 |
| `test_find_selected_skeleton_returns_none_when_no_match` | 无匹配降级 |

### 新增测试（`test_agent_loop.py`）

| 测试 | 覆盖点 |
|------|--------|
| `test_phase5_text_only_daily_plan_triggers_state_repair` | 验证 Phase 5 repair 机制：LLM 只输出文本 → 注入提醒 → LLM 写入 daily_plans |

### 回归测试结果

```
tests/test_context_manager.py          17 passed  (含 8 新增)
tests/test_agent_loop.py               12 passed  (含 1 新增)
tests/test_state_models.py             13 passed
tests/test_generate_summary.py          8 passed
tests/test_update_plan_state.py        18 passed
tests/test_phase_router.py             21 passed
tests/test_phase_integration.py         6 passed
tests/test_appendix_issues.py           8 passed
tests/test_e2e_golden_path.py           1 passed
tests/test_loop_payload_compaction.py   14 passed
tests/test_openai_provider.py           6 passed
=========================================
                             124 passed
```

### Playwright E2E

```
npx playwright test e2e-test.spec.ts
  1 passed (15.2s)
```

## 修复前后对比

### 修复前：Phase 5 LLM 看到的运行时状态

```
- 阶段：5
- 目的地：大阪
- 日期：2026-04-15 至 2026-04-18（3 天）
- 已生成旅行画像：5 项          ← 只有计数
- 骨架方案：3 套                ← 只有计数
- 已选骨架：plan_A              ← 只有 ID
- 住宿区域：心斋桥
- 已规划 0/3 天
```

### 修复后：Phase 5 LLM 看到的运行时状态

```
- 阶段：5
- 目的地：大阪
- 日期：2026-04-15 至 2026-04-18（3 天）
- 出行人数：2 成人
- 旅行画像：                     ← 完整字段内容
  - goal: 经典大阪 + 美食之旅
  - pace: relaxed
  - focus: 美食、历史建筑、购物
- 已选骨架方案（plan_A）：        ← 完整骨架内容
  - theme: 经典大阪
  - day1: 道顿堀 + 心斋桥
  - day2: 大阪城 + 天守阁
  - day3: 环球影城
- 预算：6000 CNY，已分配：0
- 住宿区域：心斋桥
- 住宿酒店：Cross Hotel Osaka
- 用户偏好：pace: 轻松; food: 喜欢美食
- 用户约束：[hard] 不坐红眼航班
- 已规划 0/3 天
  - 待规划天数：1, 2, 3
```

## 设计决策

1. **为什么只在 Phase 5+ 注入详细内容？** Phase 3 对话中 LLM 自己生成了骨架，不需要重新注入。Phase 5 是全新的 LLM 上下文（经过转场压缩），需要看到前一阶段的完整产出。

2. **为什么用模糊匹配 `_find_selected_skeleton`？** LLM 在写入 `selected_skeleton_id` 和 skeleton plan 的 `id` 字段时可能有微小差异（如 `planA` vs `plan_A`）。模糊匹配提高了鲁棒性。

3. **为什么同时做 Prompt 增强和 State Repair？** Prompt 引导是第一道防线（降低 LLM 犯错概率），State Repair 是第二道防线（即使 LLM 忘了写状态，也能被自动纠正）。两者互补。

4. **为什么 Phase 3 保持计数格式？** Phase 3 中骨架正在被 LLM 自己构建，注入完整内容反而会让上下文膨胀且无额外价值。

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `backend/context/manager.py` | 改进 | `build_runtime_context` 增加 Phase 5 骨架/画像/偏好/约束/进度注入；新增 `_find_selected_skeleton` |
| `backend/agent/loop.py` | 新增 | `_build_phase5_state_repair_message` 方法；repair 链路整合 |
| `backend/phase/prompts.py` | 增强 | Phase 5 prompt 增加输入来源说明、增量生成指引、最重要提醒 |
| `backend/tests/test_context_manager.py` | 新增 | 8 个 Phase 5 注入相关测试 |
| `backend/tests/test_agent_loop.py` | 新增 | 1 个 Phase 5 repair 测试 |
| `docs/phase5-skeleton-injection-fix.md` | 新增 | 本文档 |
