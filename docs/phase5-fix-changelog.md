# Phase 5 逐日行程不稳定：发现、修复与复盘

> 时间：2026-04-08 | 涉及 3 个核心文件 + 12 个新增测试 | 2 轮修复（初始修复 + Code Review 修正）

---

## 一、我们发现了什么

### 1.1 现象

Phase 5（逐日行程落地）在把 Phase 3 选定的骨架方案展开为逐天行程时，表现极不稳定：

- LLM 生成的行程与用户选定的骨架方案**完全不一致**，像是凭空编造
- LLM 经常把行程**只写在自然语言回复里**，不调用 `update_plan_state` 写入结构化数据
- 重试循环反复触发但无法推进，白白消耗 token
- 有时直接忽略骨架，输出与用户需求无关的「通用行程模板」

### 1.2 根因：Phase 5 的 LLM 是「瞎子」

深入代码后发现，根本原因是 **Phase 5 的 LLM 看不到它需要展开的骨架内容**。

Phase 3 → Phase 5 切换时，系统做了两件事：
1. `compress_for_transition()` 把历史消息压缩成摘要，其中 `_short_repr()` 将骨架数据截断到 160 字符 → 不可用
2. `build_runtime_context()` 只输出计数信息（`骨架方案：3 套`、`已选骨架：plan_A`），不包含任何骨架具体内容

所以 Phase 5 LLM 的处境是：**知道选了 plan_A，但不知道 plan_A 里写了什么**。它只能靠猜。

具体信息丢失清单：

| 信息 | Phase 3 能看到 | Phase 5 能看到 | 影响 |
|------|--------------|--------------|------|
| 骨架方案内容 | 完整数据结构 | 仅 `已选骨架：plan_A`（ID） | **致命**：无法基于骨架展开 |
| 旅行画像 | 完整字段 | 仅 `已生成旅行画像：5 项`（计数） | 无法按画像调节节奏 |
| 用户偏好/约束 | 对话历史可见 | 被转场压缩截断 | 可能忽视「不坐红眼航班」等约束 |
| 已完成天数 | N/A | 仅 `已规划 1/5 天`（计数） | 增量生成时不知道已排过什么 |

### 1.3 缺失的安全网

Phase 3 已有 `_build_phase3_state_repair_message()`——当 LLM 只输出文本不写状态时自动提醒。但 Phase 5 **完全没有类似机制**，导致 LLM 口头描述完行程就「以为完成了」。

---

## 二、第一轮修复（初始方案）

### 修复 1：骨架内容注入（context/manager.py）

当 `phase >= 5` 时，`build_runtime_context()` 把已选骨架的**完整内容**注入到 LLM 系统消息中。

```
修复前：
  - 骨架方案：3 套
  - 已选骨架：plan_A

修复后：
  - 已选骨架方案（plan_A）：
    - theme: 经典大阪
    - day1: 道顿堀 + 心斋桥
    - day2: 大阪城 + 天守阁
    - day3: 环球影城
```

同时注入旅行画像字段、用户偏好/约束、已完成天数摘要和待规划天数列表。

### 修复 2：Phase 5 State Repair（agent/loop.py）

新增 `_build_phase5_state_repair_message()` 方法：

- 用正则检测 LLM 输出中是否包含逐日行程文本（`第N天`、`HH:MM`、活动关键词）
- 如果检测到行程文本但 `daily_plans` 仍不完整 → 注入提醒消息要求调用 `update_plan_state`
- 与 Phase 3 repair 共享 `repair_hints_used` 防重入机制

### 修复 3：Phase 5 Prompt 增强（phase/prompts.py）

在 Phase 5 系统提示词中新增三段关键指引：
1. **输入来源说明**：告知 LLM 骨架和画像已注入到运行时状态
2. **增量生成指引**：如何处理部分已完成的 daily_plans
3. **最重要提醒**：不能只输出文本不写状态，鼓励一次性 `list[dict]` 提交

### 测试结果

- 新增 9 个单元测试（8 个 context_manager + 1 个 agent_loop）
- 全量 124 个后端测试通过
- Playwright E2E 通过

---

## 三、Code Review 发现的问题

提交初始修复后，通过独立 Rubber-Duck 审查发现了 **4 个实质性问题**：

### 问题 1：repair_hints_used 去重键不一致 ⚠️ 阻断级

```
Phase 3 repair 检查的键：step（如 "brief"）
Phase 5 repair 检查的键："p5_daily"
主循环实际添加的键：f"p{current_phase}_{phase3_step}"
                      → 对 Phase 3 是 "p3_brief"
                      → 对 Phase 5 是 "p5_skeleton"（phase3_step 在 Phase 5 上下文中已无意义）
```

**后果**：键永远匹配不上，repair hint 会**无限重复注入**，直到 `max_retries` 耗尽。Phase 3 和 Phase 5 的 repair 去重都是坏的。

### 问题 2：_find_selected_skeleton 子串匹配不安全 ⚠️ 高危

```python
# 初始代码的模糊回退
if sid in skeleton_id or skeleton_id in sid:
    return skeleton
```

当存在 `plan_A` 和 `plan_A_plus` 两个骨架时，选中 `plan_A` 可能错误匹配到 `plan_A_plus`。注入了**错误的骨架**意味着 Phase 5 LLM 会按错误的方案排行程，比不注入更危险。

### 问题 3：repair 检测只认中文格式 ⚠️ 中等

检测条件只匹配 `第N天` / `Day N` 加上时间/活动关键词。遗漏了：
- JSON 格式输出：`{"day": 1, "date": "2026-04-15", "activities": [...]}`
- 纯日期格式：`2026-04-15 金阁寺 09:00`

### 问题 4：天数完成度用 list 长度计算 ⚠️ 中等

```python
planned_count = len(self.plan.daily_plans)  # 有重复条目时会误判
```

如果 LLM 写了两个 `day: 1` 条目，list 长度是 2 但实际只完成了 1 天。

---

## 四、第二轮修复（Review 修正）

### 修正 1：统一 repair 去重键

```python
# Phase 3 repair - 检查和添加使用相同格式
repair_key = f"p3_{step}"  # 如 "p3_brief"

# Phase 5 repair - 检查和添加使用相同格式
repair_key = "p5_daily"

# 主循环 - 根据 current_phase 添加正确的键
if current_phase == 5:
    repair_hints_used.add("p5_daily")
else:
    repair_hints_used.add(f"p{current_phase}_{step}")
```

### 修正 2：移除子串匹配，改为安全回退

```python
def _find_selected_skeleton(self, plan):
    # 1. 精确 id 或 name 匹配
    for skeleton in plan.skeleton_plans:
        if skeleton.get("id") == sid or skeleton.get("name") == sid:
            return skeleton
    # 2. 唯一骨架回退（无歧义风险）
    valid = [s for s in plan.skeleton_plans if isinstance(s, dict)]
    if len(valid) == 1:
        return valid[0]
    # 3. 多骨架无精确匹配 → 返回 None（宁可不注入也不注入错的）
    return None
```

### 修正 3：扩展检测范围

```python
# 新增 JSON schema 标记检测
has_json_markers = sum(
    1 for kw in ('"day"', '"date"', '"activities"', '"start_time"')
    if kw in text
) >= 2

# 新增日期格式检测
has_date_patterns = bool(re.search(r"\d{4}-\d{2}-\d{2}", text))

# 三路触发条件
if (day_pattern_count >= 1 and (has_time_slots or has_activity_markers)) \
        or has_json_markers \
        or (has_date_patterns and has_activity_markers):
```

### 修正 4：唯一天数计算

```python
planned_days = set()
for dp in self.plan.daily_plans:
    if hasattr(dp, "day"):
        planned_days.add(dp.day)
    elif isinstance(dp, dict):
        planned_days.add(dp.get("day"))
planned_count = len(planned_days)
```

### 测试结果

- 新增 2 个测试（repair 去重验证 + JSON 格式检测）
- 修正 2 个测试断言（适配新匹配逻辑）
- 全量 362 个后端测试通过（5 个预存失败与本次修复无关）

---

## 五、变更文件清单

| 文件 | 变更 | 说明 |
|------|------|------|
| `backend/context/manager.py` | 改进 | Phase 5+ 骨架/画像/偏好/约束/进度注入；`_find_selected_skeleton` 安全匹配 |
| `backend/agent/loop.py` | 新增+修正 | Phase 5 repair 机制；去重键修正；JSON/日期检测；唯一天数 |
| `backend/phase/prompts.py` | 增强 | Phase 5 prompt 增加输入来源/增量指引/最重要提醒 |
| `backend/tests/test_context_manager.py` | 新增 | 9 个测试（骨架注入、画像、偏好、进度、匹配安全） |
| `backend/tests/test_agent_loop.py` | 新增 | 3 个测试（repair 触发、去重、JSON 检测） |
| `docs/phase5-skeleton-injection-fix.md` | 新增 | 详细根因分析文档 |
| `docs/phase5-fix-changelog.md` | 新增 | 本文档 |

---

## 六、关键设计决策

1. **Phase 条件注入**：仅 Phase 5+ 注入完整内容。Phase 3 LLM 自己生成骨架，不需要重注入。

2. **双重防线**：Prompt 引导（降低出错概率）+ State Repair（出错后自动纠正）。两者互补，单用任何一个都不够稳定。

3. **宁缺勿错**：骨架匹配失败时返回 None 而非猜测。注入错误骨架比不注入更危险——前者让 LLM 按错误方案排行程，后者至少 LLM 会发现信息不足并主动询问。

4. **一次性写入**：鼓励 LLM 用 `list[dict]` 一次写入所有天数的 daily_plans，而非逐天调用——减少工具调用次数和出错机会。

---

## 七、Git 提交记录

```
1a3b50c 🐛 fix: address critique findings for Phase 5 repair and skeleton matching
37f1600 🐛 fix: inject skeleton content into Phase 5 context for stable daily plan generation
51e0b0e ✨ feat: harden phase 5 daily_plans pipeline and phase-transition compression
```

---

## 八、后续可改进方向

| 方向 | 优先级 | 说明 |
|------|--------|------|
| 运行时上下文大小预算 | 中 | 大骨架可能撑爆 context window，可加截断/摘要机制 |
| 骨架 ID 归一化 | 低 | 在 Phase 3 写入骨架时即标准化 ID 格式，减少 Phase 5 匹配复杂度 |
| 更多 repair 格式支持 | 低 | 支持 Markdown 表格格式的行程输出检测 |
| E2E 全链路测试 | 中 | 当前 Playwright 测试只覆盖 Phase 1 流程，应扩展到 Phase 3→5 |
