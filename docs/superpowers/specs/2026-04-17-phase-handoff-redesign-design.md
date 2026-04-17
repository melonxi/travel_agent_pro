# Phase Handoff 重构设计

## 背景

当前 `AgentLoop._rebuild_messages_for_phase_change()` 在阶段切换时，会构造如下消息序列：

1. 新阶段的 `system message`
2. 一条 assistant 角色的“上一阶段对话与工具调用回顾”
3. 原始用户消息重放

其中第 2 步依赖 `ContextManager.compress_for_transition()`，把上一阶段的用户消息、assistant 文本、工具调用与工具结果压缩成一段规则化流水账。

这套机制的原始目标是：

- 避免在切换阶段后完全丢失上下文
- 在 provider 只接受非空 `messages` 时保留一个 user anchor
- 让新阶段模型看到上一阶段做了什么

但在当前项目架构下，这个设计已经开始和系统能力发生冲突。

### 已确认的现状

1. `ContextManager.build_system_message()` 已经为每一轮 LLM 调用注入：
   - 当前阶段 prompt
   - 当前规划状态（destination、dates、trip_brief、骨架、住宿、偏好、约束、daily_plans 进度等）
   - 当前可用工具列表
2. 每个 phase prompt 的职责边界已经较强：
   - Phase 1 不做交通/住宿/逐日行程
   - Phase 3 负责 brief/candidate/skeleton/lock
   - Phase 5 不重新选目的地、不重做骨架、不重新锁住宿
   - Phase 7 不重做行程、不重选交通住宿
3. 因此，阶段切换时真正缺少的不是“历史聊天回放”，而是一份更薄、更明确的职责交接语义。

### 触发本次设计的事故

`sess_5b4a037df06b` 在 Phase 3 -> 5 切换后，MiniMax 模型输出原始 XML 工具调用文本，直接暴露到前端。调查后确认：

- 阶段切换摘要和原始用户消息重放，把模型再次拉回了 Phase 3 的锁定任务语境
- 模型尝试调用当前阶段不可用的工具
- provider 没有识别 XML 回退格式，最终泄漏给用户

这次事故说明：**phase handoff 不应继续以“历史压缩摘要”为中心，而应升级为“结构化职责交接”。**

---

## 目标

将 phase 切换机制从“历史摘要 + 原始用户消息重放”重构为“结构化状态 + 职责交接指引”，降低跨阶段语义污染，并让新阶段模型只看到与当前职责直接相关的信息。

## 非目标

- 不修改 `PhaseRouter.infer_phase()` 的阶段判断规则
- 不修改各 phase 的核心业务目标
- 不在本次设计中处理 provider 层 XML 解析兜底（那是独立修复项）
- 不重构 `build_system_message()` 的整体拼装方式

---

## 设计原则

### 1. handoff 交接“职责”，不交接“流水账”

phase 切换后的模型不需要知道上一阶段所有对话细节，只需要知道：

- 当前处于哪个阶段
- 已完成哪些关键产物
- 当前唯一目标是什么
- 哪些事禁止重复做
- 如果前置条件不足，应该如何升级处理

### 2. 结构化状态是主数据源

`当前规划状态` 已经是新阶段的主事实来源。handoff note 只补充“任务语义”，不重复承载完整业务数据。

### 3. 阶段交接内容必须短、确定性、可断言

handoff note 不能是自由文本摘要，而应是固定模板拼装，便于：

- 降低模型误读
- 测试稳定断言
- 避免后续演化为新的长摘要系统

### 4. 默认不重放原始用户消息

原始用户消息在上一阶段往往已经被消费、解释并沉淀到状态里。phase 切换时再次原样重放，容易重新触发上一阶段动作模式。

---

## 决策

### 决策 1：彻底移除 phase transition summary

`AgentLoop._rebuild_messages_for_phase_change()` 不再调用 `ContextManager.compress_for_transition()`。

这意味着：

- 不再把用户/助手/工具流水账拼接成 assistant 摘要
- `compress_for_transition()` 不再承担 phase handoff 职责

### 决策 2：引入 handoff note

阶段切换后，新增一条确定性的 assistant 角色交接说明，固定包含以下四段：

1. 当前阶段
2. 已完成事项
3. 当前唯一目标
4. 禁止重复事项与升级路径

示例（Phase 3 -> 5）：

```text
[阶段交接]
当前阶段：Phase 5（逐日行程落地）。
已完成事项：目的地、日期、旅行画像、已选骨架、交通、住宿均已确认。
当前唯一目标：基于已选骨架与住宿，生成覆盖全部出行日期的 daily_plans。
禁止重复：不要重新锁交通、不要重新锁住宿、不要重选骨架；若前置状态不足或骨架不可执行，调用 request_backtrack(to_phase=3, reason="...")。
```

### 决策 3：默认不重放 `original_user_message`

对于 `to_phase > from_phase` 的正常前进切换：

- 默认不再追加 `self._copy_message(original_user_message)`

保留原始用户消息重放只用于 `to_phase < from_phase` 的 backtrack 场景，因为回退后通常需要重新处理同一条用户请求。

### 决策 4：保留单一 system message + 单一 assistant handoff note

切换后消息结构调整为：

```text
[system(new phase prompt + runtime context)]
[assistant(handoff note)]
```

backtrack 场景保持：

```text
[system(new phase prompt + runtime context)]
[system(backtrack notice)]
[user(original user message)]
```

即：正常前进切换和回退切换的 rebuild 策略分开处理。

---

## 模块设计

### A. `backend/agent/loop.py`

#### `_rebuild_messages_for_phase_change()`

当前逻辑：

- normal forward transition：system + assistant summary + original user message

新逻辑：

- normal forward transition：system + assistant handoff note
- backtrack transition：system + backtrack notice + original user message

#### 变更要点

1. 删除对 `context_manager.compress_for_transition()` 的调用
2. 改为调用新的 handoff builder
3. 删除 forward transition 中 `original_user_message` 的重放

### B. `backend/context/manager.py`

新增方法：

```python
def build_phase_handoff_note(
    self,
    *,
    plan: TravelPlanState,
    from_phase: int,
    to_phase: int,
) -> str:
```

该方法负责基于当前 `plan` 生成固定模板的 handoff 文本。

#### 组成规则

##### 1. 当前阶段
- 从 `to_phase` 映射出人类可读名称

##### 2. 已完成事项
- 根据 plan 中已存在字段，拼装高层完成项：
  - 目的地
  - 日期
  - 旅行画像
  - shortlist / 候选筛选
  - 已选骨架
  - 交通
  - 住宿
  - daily_plans 进度

注意：这里只写“完成项名称”，不写具体大段数据。

##### 3. 当前唯一目标
- 根据 `to_phase` 返回一条固定职责描述

##### 4. 禁止重复事项与升级路径
- 根据 `to_phase` 返回一条固定禁止/回退说明

### C. `ContextManager.compress_for_transition()`

本次不直接删除方法，以降低影响面，但它将退出 phase handoff 主路径。

处理策略：

- 保留函数实现，避免一次性打断过多测试和潜在调用点
- 在代码注释和文档中标明：**该函数不再用于 phase handoff**

后续如果确认没有其他消费方，可另起一轮清理。

---

## Handoff Note 规则细化

### Phase 名称映射

| Phase | 展示名称 |
|------|---------|
| 1 | 目的地收敛 |
| 3 | 行程框架规划 |
| 5 | 逐日行程落地 |
| 7 | 出发前查漏 |

### 当前唯一目标模板

| to_phase | 模板 |
|---------|------|
| 1 | 当前唯一目标：帮助用户确认目的地，不进入交通、住宿或逐日行程。 |
| 3 | 当前唯一目标：围绕已确认目的地完成旅行画像、候选筛选、骨架方案与锁定项。 |
| 5 | 当前唯一目标：基于已选骨架与住宿，生成覆盖全部出行日期的 `daily_plans`。 |
| 7 | 当前唯一目标：基于已确认行程做出发前查漏与准备清单，不重做规划。 |

### 禁止重复事项模板

| to_phase | 模板 |
|---------|------|
| 3 | 禁止重复：不要回到目的地发散；若用户要求推翻前序决策，使用 `request_backtrack(...)`。 |
| 5 | 禁止重复：不要重新锁交通、不要重新锁住宿、不要重选骨架；若前置状态不足或骨架不可执行，调用 `request_backtrack(to_phase=3, reason="...")`。 |
| 7 | 禁止重复：不要修改 `daily_plans`、不要重新选择交通或住宿；若发现严重问题，使用 `request_backtrack(...)`。 |

### 已完成事项拼装规则

建议输出为逗号分隔的高层标签，不输出值：

```text
已完成事项：目的地、日期、旅行画像、已选骨架、交通、住宿均已确认。
```

如果完成项为空，则降级为：

```text
已完成事项：系统已按当前规划状态切换到新阶段。
```

---

## 兼容性与风险

### 风险 1：某些 provider 可能要求 `messages` 非空且最好有 user 消息

当前 forward transition 后将只保留 system + assistant handoff note。理论上这是合法的，因为系统随后会继续在同一次 agent loop 中发起下一轮 LLM 调用，messages 并不为空。

如果某个 provider 强依赖 user turn 才稳定响应，需要单独在 provider 兼容层处理，不应继续依赖“重放原始用户消息”这一高污染策略。

### 风险 2：handoff note 与 runtime context 重复

这是可接受的有限重复。两者职责不同：

- runtime context 负责事实
- handoff note 负责任务语义

设计上应确保 handoff note 只写“已完成项标签”，不展开值，避免重复过重。

### 风险 3：保留 `compress_for_transition()` 可能让未来开发者误用

需要通过：

- 代码注释
- spec / plan
- 测试改名或新增断言

明确说明 phase handoff 已不依赖它。

---

## 测试策略

本次只执行相关测试，不跑全量测试。

### 需要更新 / 新增的测试

#### `backend/tests/test_agent_loop.py`
- 更新 `_rebuild_messages_for_phase_change()` 相关断言：
  - forward transition 不再出现 summary 文案
  - forward transition 不再重放原始 user message
  - forward transition 产物为 system + assistant handoff note
  - backtrack 行为保持原样

#### `backend/tests/test_context_manager.py`
- 新增 `build_phase_handoff_note()` 单测：
  - Phase 3 -> 5 handoff note 内容正确
  - 已完成事项按 plan 字段拼装
  - fallback 文案生效
- `compress_for_transition()` 旧测试保留，但不再把它视为 phase handoff 主路径

#### `backend/tests/test_phase_transition_event.py`
- 如果有 rebuild 顺序或消息形状断言，需要同步更新

#### `backend/tests/test_phase_integration.py`
- 若存在依赖旧 transition summary 文案的断言，改为断言新 handoff 模式

### 推荐执行的测试命令范围

```bash
cd backend && pytest tests/test_context_manager.py -q
cd backend && pytest tests/test_agent_loop.py -q
cd backend && pytest tests/test_phase_transition_event.py -q
cd backend && pytest tests/test_phase_integration.py -q
```

---

## 验收标准

1. Phase 前进切换时，不再调用 `compress_for_transition()`
2. Phase 前进切换后的消息不再包含上一阶段流水账摘要
3. Phase 前进切换后默认不再重放原始用户消息
4. 新阶段仍能获得：
   - 当前 phase prompt
   - 当前规划状态
   - 当前可用工具
   - 一条确定性的职责交接 note
5. backtrack 场景行为不回归
6. 相关测试通过，无需跑全量测试

---

## 不改动的部分

| 模块 | 理由 |
|------|------|
| `PhaseRouter` | 阶段判断逻辑本身没有问题，本次不改 |
| `build_system_message()` 主体结构 | 已经承担结构化状态注入职责 |
| 各 phase prompt 主文案 | 已具备较清晰职责边界 |
| provider 层 XML 解析 | 属于另一条修复链路，不和本次 handoff 重构绑死 |

---

## 结论

当前项目已经具备“用结构化状态驱动 phase handoff”的基础能力，因此继续保留基于历史流水账的阶段切换压缩机制，收益已经低于风险。

本次设计选择彻底移除 phase transition summary，让 phase handoff 回归为：

- 结构化事实
- 当前职责
- 明确边界
- 升级路径

这会让多阶段状态机 agent 的阶段切换更像“交接班”，而不是“继续翻聊天记录”。
