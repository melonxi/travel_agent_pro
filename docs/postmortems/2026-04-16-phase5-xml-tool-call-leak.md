# Phase 5 XML 工具调用泄漏事故复盘

- 事故日期：2026-04-16
- 事故范围：`backend/agent/loop.py`、`backend/context/manager.py`、`backend/llm/anthropic_provider.py`、`backend/tools/engine.py`、`backend/tools/plan_tools/phase3_tools.py`
- 事故类型：阶段切换语义错位 + 第三方模型兼容性差异 + provider 兜底缺失
- 事故等级：中
- 事故状态：已定位根因，待修复

---

## 1. 事故摘要

在一次 Phase 3 -> Phase 5 的正常转阶段过程中，前端直接展示了模型输出的原始 XML 工具调用文本：

```xml
<minimax:tool_call>
  <invoke name="select_transport">...</invoke>
</minimax:tool_call>
```

按系统设计，这类内容本应被识别为结构化工具调用，再交给 ToolEngine 执行；但本次实际被当成普通文本透传给前端，形成用户可见故障。

本次事故不是单点 bug，而是三层问题串联：

1. Phase 转换后的消息重建，把上一阶段的工作语义带进了下一阶段。
2. MiniMax 经讯飞 Anthropic 代理接入时，在目标工具不在当前工具列表里时，会回退输出原生 XML 工具调用文本，而不是 Anthropic 标准 `tool_use` block。
3. `anthropic_provider` 只识别 `tool_use` block，不识别 XML 回退格式，最终把它当普通文本输出。

---

## 2. 用户可见现象

问题 session：`sess_5b4a037df06b`

用户在 Phase 3 锁定阶段说：

```text
航班 ok 的，住宿就朵兰达+维也纳
```

随后系统已经自动进入 Phase 5，但 assistant 在 seq 3 和 seq 7 输出了原始 XML：

```xml
<invoke name="select_transport">
  <parameter name="choice">...</parameter>
</invoke>
```

这段 XML 没有被系统解析执行，直接显示在聊天气泡里。

---

## 3. 影响评估

### 3.1 用户影响

- 用户直接看到底层工具协议文本，体验断裂。
- 用户会误以为系统崩溃或进入开发态。
- assistant 在 Phase 5 仍表现为 Phase 3 的行为模式，损害对“多阶段规划”能力的信任。

### 3.2 系统影响

- 阶段边界失真：Phase 5 被上一阶段的确认动作污染。
- provider 抽象失效：同为“Anthropic”接入，真实模型兼容性差异未被隔离。
- 下游无兜底：异常协议穿透到 UI，说明 provider 层缺少协议清洗。

---

## 4. 发现过程

### 4.1 初始线索

- 前端显示 `<minimax:tool_call>` 文本。
- 数据库中问题消息 `tool_calls` 列为空，但 `content` 列包含 XML。
- 同模型、同代理的另一个 session 在 Phase 3 中工具调用正常，说明问题不是“模型完全不支持工具调用”，而是特定上下文下触发。

### 4.2 关键证据

1. `backend/tools/plan_tools/phase3_tools.py:496`

```python
phases=[3]
```

`select_transport` 只在 Phase 3 可用。

2. `backend/tools/engine.py:41-50`

```python
return [
    t.to_schema()
    for t in self._tools.values()
    if phase in t.phases
]
```

Phase 5 的工具列表确实不包含 `select_transport`。

3. `backend/agent/loop.py:574-594`

阶段转换后，会把上一阶段摘要写成一条 assistant 消息，并把原始用户消息再次重放到新阶段：

```python
rebuilt.append(Message(role=Role.ASSISTANT, content=summary_message))
rebuilt.append(self._copy_message(original_user_message))
```

4. `backend/context/manager.py:333-355`

阶段摘要是规则化流水账，保留全部用户原文、助手摘要和工具决策行。

5. `backend/llm/anthropic_provider.py:250-254`

provider 只识别：
- `text`
- `tool_use`

对于 XML 文本没有任何特殊处理，因此直接作为 `TEXT_DELTA` 透传。

---

## 5. 事故时间线

### 5.1 业务时间线

1. 用户在 Phase 3 已完成目的地、日期、骨架、交通候选、住宿候选等确认。
2. 用户说：“航班 ok 的，住宿就朵兰达+维也纳”。
3. Phase 3 正常调用 `select_transport`、`set_accommodation`，状态满足 Phase 5 进入条件。
4. `PhaseRouter.infer_phase(plan)` 返回 5，系统触发 3 -> 5 转场。
5. 新阶段消息被重建为：
   - Phase 5 system prompt
   - 一条超长的“阶段 3 回顾” assistant 消息
   - 原始用户消息再次出现
6. 模型在新一轮中再次尝试执行 `select_transport`。
7. 由于当前 tool schema 不含该工具，MiniMax 输出原生 XML 调用文本。
8. provider 未识别 XML，UI 直接显示。

### 5.2 代码路径时间线

```text
Phase 3 工具写入成功
  -> AgentLoop.check_and_apply_transition()
  -> AgentLoop._rebuild_messages_for_phase_change()
  -> ContextManager.compress_for_transition()
  -> AgentLoop.run() 下一次 llm.chat()
  -> AnthropicProvider._emit_nonstream_response()
  -> TEXT_DELTA 透传
  -> Message(role=ASSISTANT, content=xml_text)
  -> 前端显示
```

---

## 6. 根因分析

### 6.1 直接根因

MiniMax 在当前 tool schema 不包含目标工具时，没有返回 Anthropic 标准 `tool_use` block，而是输出了自己的 XML 风格工具调用文本；provider 未做解析，导致 XML 外泄。

### 6.2 上游诱因

Phase 转换时的上下文重建方式，让模型误以为自己仍在继续上一阶段的“锁交通/锁住宿”任务：

- 阶段摘要过长，且保留了大量上一阶段流水账。
- 摘要末尾展示了历史决策：`决策: select_transport ...`、`决策: set_accommodation ...`。
- 原始用户消息被再次重放，形成“同一确认消息再次出现”的强信号。
- 摘要没有明确标记“这些动作已完成，下一阶段无需重复执行”。

### 6.3 深层根因

当前 phase handoff 设计把“历史对话回放”当成了“阶段交接”，但对多阶段状态机 agent 来说，真正应该交接的是：

- 已沉淀的状态事实
- 已完成的决策结论
- 下一阶段的唯一目标
- 边界内允许做什么 / 不允许做什么

而不是把上一阶段的大量过程流水账继续喂给下一阶段模型。

换句话说，系统完成了“消息迁移”，但没有完成“任务语义切换”。

---

## 7. 为什么同模型在 Phase 3 正常

对比 session：`sess_920cc1e88e53`

Phase 3 正常的原因很简单：`select_transport` 在 Phase 3 的工具列表里，模型发起该调用时可以走标准工具协议，因此数据库里有结构化 `tool_calls`，前端也不会看到 XML。

所以这次事故不是“MiniMax 完全不会工具调用”，而是：

- 当目标工具存在时，行为正常。
- 当目标工具不存在时，回退路径与 Anthropic 标准不兼容。

---

## 8. 放大因素

### 8.1 转场摘要过长且像流水账

`compress_for_transition()` 当前保留：

- 全部用户消息原文
- 助手摘要
- 工具决策行

这对“保真”有利，但对“阶段切换”有害。模型会把它理解成“当前仍需继续处理的上下文”。

### 8.2 原始用户消息再次重放

如果上一阶段最后一条用户消息已经在摘要中出现，再原样追加一次，会形成“重复指令”错觉。

### 8.3 provider 抽象对第三方兼容性假设过强

代码默认认为 Anthropic 兼容网关返回的工具协议一定是 `tool_use`，但本次接入的真实模型并不满足这个假设。

### 8.4 Phase 5 repair 无法覆盖该场景

`_build_phase5_state_repair_message()` 只识别“行程文本未写状态”的情况，对 XML 工具调用文本没有检测能力，因此没能二次自救。

---

## 9. 事故定性

这是一次典型的“跨层协作失配”事故：

- 业务层以为 phase 已切换成功。
- 模型层没有真正完成任务切换。
- 协议层没有识别第三方模型的回退格式。
- 展示层缺少异常协议屏蔽。

本质不是单个函数写错，而是系统把“阶段切换”设计成了“上下文拼接”，但真实需求是“角色切换 + 任务交接 + 边界重置”。

---

## 10. 矫正与预防动作

### 10.1 修复优先级 A：provider 兜底

在 `anthropic_provider.py` 中识别 MiniMax XML 工具调用格式：

- 解析 `<invoke name="...">` 和参数
- 尽量转成内部 `TOOL_CALL_START`
- 不能转也至少不要把原始 XML 直接透传给前端

目标：即使上游再次出错，也不要把底层协议暴露给用户。

### 10.2 修复优先级 B：phase handoff 重构

把“阶段交接内容”从流水账改成结构化 handoff 包，至少包含：

1. 当前已确认事实
2. 上一阶段已完成决策
3. 当前阶段目标
4. 当前阶段禁止重复做的事
5. 若需回退，应如何回退

目标：让新阶段模型读到的是“接力棒”，不是“聊天记录回放”。

### 10.3 修复优先级 C：减少重复用户消息

如果阶段摘要已经包含最后一条用户消息，默认不要再次原样重放；或把重放消息改写成更明确的 handoff 用户锚点，例如：

```text
用户刚刚已确认交通与住宿。请在此基础上继续 Phase 5 逐日行程规划，不要重复锁定交通或住宿。
```

### 10.4 修复优先级 D：异常协议监控

增加日志和告警：

- assistant 文本中出现 `<invoke name=`、`<minimax:tool_call>`
- 工具可用列表与模型请求的工具名不匹配
- phase 转换后首轮回复仍包含上一阶段典型动作

---

## 11. 对当前架构的设计启示

### 11.1 Phase 转换不是“压缩历史”，而是“交接责任”

多 phase 旅行 agent 的每个阶段都在做不同类型的工作：

- Phase 1：收敛目的地
- Phase 3：设计框架并锁定关键选择
- Phase 5：展开逐日行程
- Phase 7：出发前查漏

因此 phase 转换时，新的模型实例最需要知道的不是“上一阶段说过什么”，而是：

- 哪些决策已经板上钉钉
- 哪些决策还没完成
- 我现在负责的唯一任务是什么
- 我绝对不能再碰什么

### 11.2 正确的 handoff 应该像工单交接，而不是会议纪要

好的 handoff 像这样：

```text
你现在处于 Phase 5。
已完成：目的地、日期、骨架、交通、住宿均已锁定。
待完成：按 2026-06-06 至 2026-06-10 生成 daily_plans。
禁止：不要再次搜索或锁定交通、住宿；若骨架不可执行，使用 request_backtrack(to_phase=3, ...)。
```

而不是再把十几轮历史对话和工具流水账贴一遍。

---

## 12. 结论

本次事故暴露的不是“某个工具 phases 写错了”，而是当前状态机 agent 的 phase handoff 语义设计还不够强：

- 系统完成了 phase 切换
- 但模型没有完成任务切换

下一步优化重点不应只是“补一个 XML parser”，而应把 phase handoff 从“历史压缩”升级为“结构化交接协议”。

只有这样，多阶段状态机才是真的在“分工协作”，而不是在“换 prompt 后继续聊上一轮的话题”。
