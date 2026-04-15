# 提示词架构升级设计文档

> **状态**：等待评审
> **作者**：AI Assistant
> **日期**：2026-04-15
> **关联**：`docs/Questions.md`、`docs/learning/2026-04-15-superpowers提示词架构调研.md`

---

## 1. 问题陈述

用户在 `docs/Questions.md` 中提出了 8 个问题。经过对 `backend/phase/prompts.py`（431 行）、`backend/phase/router.py`（123 行）、`backend/context/manager.py`（400 行）的代码审查，结合实际使用体验，**确认 6 个为真实问题，2 个为部分真实**。

### 1.1 问题验证表

| # | 问题摘要 | 判定 | 根因定位 |
|---|---------|------|---------|
| 1 | Phase 1 回复含大量无关内容，意见缺乏外部信息支撑 | ✅ 真实 | prompt 无回复纪律（无字数约束、无"先查后说"硬规则）|
| 2 | 大交通应在确认时间后锁，而非放到 lock 最后 | ✅ 真实 | lock 子阶段把交通和住宿捆在一起处理，不符合人类规划心智模型 |
| 3 | daily_plans 应按天增量更新，不应一次性全量 | ✅ 真实 | Phase 5 prompt 第 417 行明确说"优先一次性用 list[dict] 提交全部天数" |
| 4 | 骨架搭建需要专门的思考框架 | ✅ 真实 | skeleton 子阶段只说"生成 2-3 套"，无结构化思考流程 |
| 5 | Phase 5 本质是路径规划问题 | ⚠️ 部分真实 | 工具层已有 `calculate_route`/`assemble_day_plan`，但 prompt 定位为"填细节"而非"优化路线" |
| 6 | Phase 3 缺乏科学的前置信息收集框架 | ✅ 真实 | 没有"锚不变量 → 综合信息 → 决策"的结构化流程 |
| 7 | Phase 1 越界——引导用户输出所有信息 | ⚠️ 部分真实 | 规则存在（第 49 行"除非区分候选地所必需"）但缺乏 Red Flags 强化 |
| 8 | Phase 3 不收敛 | ✅ 真实 | 4 个子阶段规则全量加载导致注意力稀释；缺乏收敛压力机制 |

### 1.2 调研文档评估

`docs/learning/2026-04-15-superpowers提示词架构调研.md` 整体质量高，可作为设计基础。核心洞察准确：

- ✅ 识别了 superpowers 的核心设计哲学（Gate > 建议，证据 > 声称）
- ✅ 现状分析精准（Phase 7 弱、Phase 3 全量加载、规则建议式非 Gate 式）
- ✅ 技能卡结构设计合理，与现有运行时兼容

**需补充/修正 5 点：**

1. **缺失 transport 时序问题**：用户提出的"确认时间后立即锁大交通"未被吸收
2. **优先级应调整**：P0 应为 Phase 3 子阶段拆分（最高 ROI），而非补强 Phase 7
3. **缺乏 Phase 1 回复纪律**：用户问题 1、7 的根因是 Phase 1 输出约束不足
4. **缺乏收敛机制**：Phase 3 不收敛不只是 token 稀释，还缺乏对话轮次的收敛压力
5. **Phase 5 增量策略未覆盖**：调研提到了 Completion Gate 但未覆盖"按天生成"策略

---

## 2. 设计目标

1. **Phase 3 按子阶段动态注入 prompt**——消除注意力稀释，每个子阶段只看到自己的规则
2. **所有阶段增加结构化 Red Flags + Completion Gate**——从"建议式"升级为"Gate 式"
3. **Phase 1 增加回复纪律**——控制输出长度，强制"先查后说"
4. **Phase 5 改为增量生成 + 路径优化核心定位**——从"填细节"升级为"优化路线"
5. **Phase 7 补齐完整技能卡结构**——从 4 行 prompt 升级为标准结构
6. **Phase 3 skeleton 子阶段增加思考框架**——结构化思考流程

### 不在本次范围

- 运行时拼装逻辑（`context/manager.py`）不改动
- 工具引擎（`tools/engine.py`）不改动
- 反思机制（`agent/reflection.py`）不改动
- 工具选择策略（`agent/tool_choice.py`）不改动
- transport 时序优化（需要 phase router 逻辑改动，计划在后续迭代中处理）

---

## 3. 架构设计

### 3.1 文件结构变更

**改动文件：**

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `backend/phase/prompts.py` | 主改动 | 拆分常量，重写所有阶段 prompt |
| `backend/phase/router.py` | 小改动 | 新增 `get_prompt_for_plan(plan)` 方法 |
| `backend/main.py` | 单行改动 | 调用 `get_prompt_for_plan(plan)` 替代 `get_prompt(plan.phase)` |
| `backend/agent/loop.py` | 单行改动 | 同上 |
| `backend/tests/test_phase_router.py` | 更新断言 | 适配新 prompt 内容 |
| `backend/tests/test_phase34_merge.py` | 更新断言 | 适配新 prompt 内容 |
| `backend/tests/test_prompt_architecture.py` | 新建 | 验证技能卡结构完整性 |

**不改动文件：**
- `backend/context/manager.py`（接收 `phase_prompt: str`，不关心来源）
- `backend/tools/engine.py`（`_phase3_tool_names()` 已有按子阶段过滤，不受影响）
- `backend/agent/reflection.py`（在 `phase3_lock` 和 `phase5_complete` 触发，不受影响）
- `backend/agent/tool_choice.py`（独立模块，不受影响）
- Mock routers in test files（`test_agent_loop.py`, `test_appendix_issues.py`, `test_telemetry_agent_loop.py`）—— 它们只实现 `get_prompt(phase)` 且不使用真实 prompt 内容，不受影响

### 3.2 prompts.py 新结构

```python
# backend/phase/prompts.py

# ── 全局 Red Flags（附加在所有阶段 prompt 末尾）──────────────────
GLOBAL_RED_FLAGS: str = """..."""

# ── Phase 1：目的地收敛顾问 ──────────────────────────────────
PHASE1_PROMPT: str = """..."""

# ── Phase 3：行程框架规划师（基座 + 子阶段）─────────────────────
PHASE3_BASE_PROMPT: str = """..."""  # 角色定义 + 通用规则

PHASE3_STEP_PROMPTS: dict[str, str] = {
    "brief": """...""",
    "candidate": """...""",
    "skeleton": """...""",
    "lock": """...""",
}

def build_phase3_prompt(step: str = "brief") -> str:
    """根据子阶段动态拼装 Phase 3 prompt。"""
    base = PHASE3_BASE_PROMPT
    step_prompt = PHASE3_STEP_PROMPTS.get(step, PHASE3_STEP_PROMPTS["brief"])
    return f"{base}\n\n{step_prompt}\n\n{GLOBAL_RED_FLAGS}"

# ── Phase 5：逐日行程落地与验证师 ─────────────────────────────
PHASE5_PROMPT: str = """..."""

# ── Phase 7：出发前查漏清单生成器 ─────────────────────────────
PHASE7_PROMPT: str = """..."""

# ── 向后兼容 PHASE_PROMPTS dict ─────────────────────────────
PHASE_PROMPTS: dict[int, str] = {
    1: PHASE1_PROMPT + "\n\n" + GLOBAL_RED_FLAGS,
    3: build_phase3_prompt("brief"),  # 默认 brief
    5: PHASE5_PROMPT + "\n\n" + GLOBAL_RED_FLAGS,
    7: PHASE7_PROMPT + "\n\n" + GLOBAL_RED_FLAGS,
}

# ── 控制模式（不变）──────────────────────────────────────────
PHASE_CONTROL_MODE: dict[int, str] = {
    1: "conversational",
    3: "workflow",
    5: "structured",
    7: "evaluator",
}
```

### 3.3 router.py 新增方法

```python
# 新增导入
from phase.prompts import PHASE_CONTROL_MODE, PHASE_PROMPTS, build_phase3_prompt, GLOBAL_RED_FLAGS, PHASE1_PROMPT, PHASE5_PROMPT, PHASE7_PROMPT

class PhaseRouter:
    # ... 现有方法不变 ...

    def get_prompt(self, phase: int) -> str:
        """向后兼容：不带 plan 参数的调用返回默认 prompt。"""
        return PHASE_PROMPTS.get(phase, PHASE_PROMPTS[1])

    def get_prompt_for_plan(self, plan: TravelPlanState) -> str:
        """根据 plan 状态动态返回 prompt。Phase 3 按子阶段拆分。"""
        if plan.phase == 3:
            step = plan.phase3_step or "brief"
            return build_phase3_prompt(step)
        if plan.phase == 1:
            return PHASE1_PROMPT + "\n\n" + GLOBAL_RED_FLAGS
        if plan.phase == 5:
            return PHASE5_PROMPT + "\n\n" + GLOBAL_RED_FLAGS
        if plan.phase == 7:
            return PHASE7_PROMPT + "\n\n" + GLOBAL_RED_FLAGS
        return self.get_prompt(plan.phase)
```

### 3.4 调用方改动

**`main.py` 第 1901 行：**
```python
# Before:
phase_prompt = phase_router.get_prompt(plan.phase)
# After:
phase_prompt = phase_router.get_prompt_for_plan(plan)
```

**`agent/loop.py` 第 554 行：**
```python
# Before:
phase_prompt = self.phase_router.get_prompt(to_phase)
# After:
phase_prompt = self.phase_router.get_prompt_for_plan(self.plan)
```

---

## 4. 技能卡结构规范

借鉴 superpowers 调研成果，每个阶段 prompt 采用统一的技能卡结构：

```
## 角色
一句话角色定义。

## 目标
本阶段唯一核心目标。

## 硬法则
不可违反的硬约束，Gate 式表述（"必须"/"不允许"/"禁止"）。

## 输入 Gate
进入本阶段时必须满足的前提条件。

## 流程
分步骤的工作流程。

## 状态写入契约
哪些字段该写、什么时候写、什么格式。

## 工具契约
本阶段可用工具及使用策略。

## 完成 Gate
必须满足什么条件才算完成。

## Red Flags（来自 GLOBAL_RED_FLAGS）
全局 + 阶段特有的 Red Flags。

## 压力场景（可选）
关键边缘情况的处理指南。
```

---

## 5. 各阶段 prompt 设计要点

### 5.1 GLOBAL_RED_FLAGS

适用于所有阶段的硬性红线：

- **越界禁止**：不在当前阶段职责范围内的操作必须拒绝
- **幻觉禁止**：没有工具证据支撑的事实信息不允许输出
- **状态漂移禁止**：不把 AI 推断写入用户偏好字段
- **沉默写入禁止**：结构化产物必须通过工具写入，不允许只在正文描述
- **过度搜索禁止**：拿到足够信息后停止搜索

### 5.2 Phase 1 重写要点

**问题解决：** Q1（无关内容）、Q7（越界行为）

**核心变化：**
1. 增加 **回复纪律** 段：
   - 每轮回复 ≤ 300 字（不含工具结果引用）
   - 候选比较表 ≤ 5 列，不做长篇铺垫
   - 禁止在没有搜索结果时给出具体推荐理由
2. 增加 **"先查后说"硬规则**：
   - 推荐候选前必须至少完成 1 次 `xiaohongshu_search`
   - 事实性断言（价格、签证、安全）必须有 `web_search` 验证
3. 增加 **Phase 1 特有 Red Flags**：
   - ❌ 在目的地未确认前讨论住宿选择、逐日行程
   - ❌ 主动追问日期、预算、人数（除非区分候选所必需）
   - ❌ 一次性输出超过 3 个候选
4. 保留现有的 **Examples（类型 A-E）**，结构不变

### 5.3 Phase 3 拆分要点

**问题解决：** Q2（transport 时序）、Q4（骨架思考框架）、Q6（信息收集框架）、Q8（不收敛）

**核心变化：**

#### PHASE3_BASE_PROMPT（所有子阶段共享）
- 角色定义 + 4 个子阶段概述
- 通用硬法则（状态写入纪律、工具使用纪律）
- 阶段边界（不生成逐日详细行程、不生成出发清单）

#### brief 子阶段
- 保持现有内容
- 增加 Completion Gate："当 `trip_brief` 已写入且包含 ≥3 个关键字段时，进入 candidate"

#### candidate 子阶段
- 保持现有内容
- 增加 **收敛压力**："candidate 子阶段不应超过 3 轮对话；如果 3 轮后仍无 shortlist，强制基于已有信息生成"
- 增加 Completion Gate："当 `shortlist` 已写入且 ≥ 3 项时，进入 skeleton"

#### skeleton 子阶段
- **新增思考框架**（Q4 + Q6）：
  ```
  思考流程（在生成骨架前必须完成）：
  1. 锚定不变量：出发地/返回地、总天数、必去项、硬约束
  2. 识别取舍维度：区域覆盖 vs 深度体验、体力消耗 vs 景点密度
  3. 分配日程：按区域聚合，确保同区域活动排在同一天/相邻天
  4. 检验可行性：每天移动距离 < 合理阈值，体力负荷交替
  5. 成型方案：2-3 套差异化骨架，标注关键取舍
  ```
- 增加 Completion Gate

#### lock 子阶段
- **调整 transport 时序**（Q2）：明确"日期确认后可以先查交通，不必等住宿确定"
- 保持现有完成标志
- 增加收敛压力

### 5.4 Phase 5 重写要点

**问题解决：** Q3（增量生成）、Q5（路径规划定位）

**核心变化：**
1. **路径规划核心定位**：
   - 角色从"逐日行程落地与验证师"重构为"路径规划与日程编排师"
   - 核心目标从"把骨架展开"变为"优化每天的移动路径和时间分配"
2. **增量生成策略**（Q3）：
   - 删除"优先一次性用 list[dict] 提交全部天数"
   - 改为"按 1-2 天为单位生成并写入，完成一批再做下一批"
   - 每次写入后检查进度
3. **增加输入 Gate**：必须有 `selected_skeleton_id` + `accommodation` + `dates`
4. **增加 Completion Gate**：`daily_plans` 覆盖全部天数 + 关键验证完成

### 5.5 Phase 7 重写要点

**核心变化：** 从 4 行扩展为完整技能卡结构

```
角色：出发前查漏与清单生成师
目标：基于已确认的完整行程，生成可执行的出行检查清单
输入 Gate：daily_plans 覆盖全部天数
流程：证件 → 货币 → 天气衣物 → 项目注意事项 → 紧急联系 → 实用贴士 → 生成摘要
工具契约：check_weather（必用）、generate_summary（必用）、search_travel_services（按需）
完成 Gate：generate_summary 已调用且结果已呈现给用户
Red Flags：不重新讨论行程安排、不新增景点、不修改 daily_plans
```

---

## 6. 向后兼容策略

### 6.1 `PHASE_PROMPTS` dict 保留

`PHASE_PROMPTS` dict 保留不删，内容从新常量填充：
- `PHASE_PROMPTS[1]` = `PHASE1_PROMPT + GLOBAL_RED_FLAGS`
- `PHASE_PROMPTS[3]` = `build_phase3_prompt("brief")`（默认 brief）
- `PHASE_PROMPTS[5]` = `PHASE5_PROMPT + GLOBAL_RED_FLAGS`
- `PHASE_PROMPTS[7]` = `PHASE7_PROMPT + GLOBAL_RED_FLAGS`

这确保所有直接引用 `PHASE_PROMPTS[n]` 的测试和代码不会破裂。

### 6.2 `get_prompt(phase)` 保留

旧签名 `get_prompt(phase: int)` 保留，返回 `PHASE_PROMPTS.get(phase, PHASE_PROMPTS[1])`。

新签名 `get_prompt_for_plan(plan: TravelPlanState)` 仅在两个调用方（`main.py`、`loop.py`）使用。

### 6.3 Mock Routers 不受影响

`test_agent_loop.py`、`test_appendix_issues.py`、`test_telemetry_agent_loop.py` 中的 `FakePhaseRouter` / `_PhaseRouter` 只实现 `get_prompt(phase)` 并返回占位字符串，不使用真实 prompt 内容，因此不需要改动。

---

## 7. 测试策略

### 7.1 新增测试（`test_prompt_architecture.py`）

- `GLOBAL_RED_FLAGS` 包含核心关键词（"越界"、"幻觉"、"漂移"）
- 每个阶段 prompt 包含技能卡关键段（"硬法则"/"目标"/"完成 Gate"/"Red Flags"）
- `build_phase3_prompt(step)` 对 4 个子阶段各返回不同内容
- `build_phase3_prompt(step)` 输出不包含其他子阶段的详细规则
- `PHASE_PROMPTS` dict 仍可正常使用

### 7.2 现有测试适配

- `test_phase_router.py`：更新断言字符串（如 `"目的地收敛顾问"` 保持不变，`"不要只停留在标题层判断"` 等需要确认是否保留）
- `test_phase34_merge.py`：更新 `PHASE_PROMPTS[3]` 相关断言
- Mock routers：不需要改动

### 7.3 不新增的测试

- 不新增 E2E 测试（prompt 改动不影响 API 契约）
- 不新增 LLM 集成测试（已有 golden eval 覆盖）

---

## 8. 风险评估

| 风险 | 影响 | 缓解 |
|------|------|------|
| prompt 过长导致 token 超限 | 中 | 每个阶段 prompt 控制在 2000 token 内；Phase 3 子阶段 prompt 更短 |
| 旧测试断言字符串不全覆盖 | 低 | grep 所有测试中的断言字符串，逐一确认 |
| Phase 3 动态拼装引入 bug | 低 | `build_phase3_prompt()` 逻辑极简单（字符串拼接），有专门测试覆盖 |
| `loop.py` 的 `get_prompt` 调用在 transition 时 plan 可能尚未更新 | 中 | 确认 `sync_phase_state()` 在 `get_prompt_for_plan()` 之前已被调用 |

---

## 9. 实现优先级

1. **Task 1**：GLOBAL_RED_FLAGS + Phase 1 重写
2. **Task 2**：Phase 3 拆分（base + 4 子阶段）
3. **Task 3**：Router 更新 + 调用方改动
4. **Task 4**：Phase 5 重写
5. **Task 5**：Phase 7 重写
6. **Task 6**：GLOBAL_RED_FLAGS 注入全部阶段
7. **Task 7**：测试修复
8. **Task 8**：文档更新
9. **Task 9**：最终验证
