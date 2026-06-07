# Runtime Notice 与 App Event 的 Message List 位置说明

> 本文用简化 message list 解释上下文工程如何管理 `<runtime_notice>` 和 `<app_event>`。重点关注：它们何时注入、在 message list 中放在哪里、哪些会持久化，以及对 prefix cache 的影响。

## 1. 共同基线

每轮主聊天开始时，LLM runtime message list 先被组装成：

```text
[0] SYSTEM static_system
    transient=True
    作用：稳定规则、阶段规则、tag 解释

[1..n] persisted_history
    transient=False
    作用：真实用户消息、assistant 回复、tool 相关消息、持久 app_event

[n+1] USER current_user
      transient=False
      作用：本轮真实用户输入

[n+2] USER <turn_context>
      transient=True
      作用：当前时间、当前阶段、当前可用工具、TravelPlanState、相关记忆
```

正常主聊天路径会把这份列表注册为：

```text
session["_active_runtime_messages"] = llm_messages
```

之后 Hook 如果需要注入 `runtime_notice` 或 `app_event`，会优先追加到这份 active runtime list，而不是直接改静止状态下的 `session["messages"]`。

后续在 AgentLoop 运行中，assistant tool call、tool result、runtime notice、app event 会继续追加到 active runtime list，或在阶段重建时插入到合适位置。

生命周期规则：

```text
transient=True：
  本轮 runtime prompt 可见，结束后不进 session["messages"]，不写 SQLite。

transient=False：
  可以进入 persisted_history，下一轮继续作为历史上下文出现。
```

本轮结束或安全持久化前会执行清理：

```text
session["messages"] = clean_persisted_session_messages(llm_messages)
```

因此：

```text
保留：transient=False 且 role != SYSTEM 的消息
移除：static_system、turn_context、runtime_notice
```

## 2. Runtime Notice 总览

`<runtime_notice>` 都是：

```text
role=USER
transient=True
持久化：否
语义：Harness 给 Agent 的临场提醒，不是用户请求
```

### 2.1 validation：实时约束检查

场景：模型调用状态写入工具后，Harness 发现预算、住宿、交通等约束问题。

真实流程不是把 validation 插进 assistant tool call 和 tool result 中间，而是：

```text
第一次 LLM 调用前：

[0] SYSTEM static_system                         transient=True
[1..n] persisted_history                         transient=False
[n+1] USER current_user                          transient=False
[n+2] USER <turn_context>                        transient=True

LLM 输出工具调用后，AgentLoop 追加：

[n+3] ASSISTANT tool_calls                       transient=False
[n+4] TOOL tool_result                           transient=False

after_tool_call hook 发现约束错误：

session["_pending_system_notes"] += "[实时约束检查]..."

下一次 LLM 调用前，on_before_llm flush：

[n+5] USER <runtime_notice kind="validation">    transient=True
      [实时约束检查]
      - 预算和住宿要求冲突
```

代码顺序上，validation flush 发生在 `before_llm_call` hook 内，且早于 prompt token 预算检查和 history summary 压缩。它不是直接插进 OpenAI 要求的 `assistant.tool_calls -> tool` 协议序列中间，这是为了避免破坏工具调用协议。

结束后保留：

```text
保留：current_user、ASSISTANT tool_calls、TOOL tool_result、后续 assistant 回复
移除：static_system、turn_context、runtime_notice validation
```

对 prefix cache 的影响：

```text
本轮内：tool/result/validation 追加在尾部，前面相同 prefix 可复用。
跨轮：tool/result 进入 persisted_history，是 append-only 增长，不改写旧 prefix。
validation 自身 transient=True，本轮结束后不进入下一轮 prefix。
```

### 2.2 reflection：阶段自检

场景：进入关键阶段边界时，Harness 提醒模型自检。例如 Phase 2 进入 lock 前，或 Phase 3 所有天数行程填完后。

注入位置：

```text
某次 LLM 调用前：

[0] SYSTEM static_system                         transient=True
[1..n] persisted_history                         transient=False
[n+1] USER current_user                          transient=False
[n+2] USER <turn_context>                        transient=True

run_llm_turn 顶部检查 reflection trigger 后追加：

[n+3] USER <runtime_notice kind="reflection">    transient=True
      [自检]
      用户偏好是否都体现？
      用户约束有没有违反？
      必去点是否安排？
```

更精确的代码顺序是：

```text
before_llm_call hook
  ├── flush validation
  ├── tool payload compaction
  └── history_summary compaction

reflection.check_and_inject(...)
strip_non_initial_system_messages(...)
llm.chat(...)
```

所以 reflection 是在本次 `llm.chat(...)` 之前追加，但在 `before_llm_call` hook 之后追加。

如果这是工具调用后的下一次 iteration，也可能出现在已有 `ASSISTANT tool_calls` / `TOOL tool_result` 后面：

```text
... persisted / current / turn_context
ASSISTANT tool_calls                             transient=False
TOOL tool_result                                 transient=False
USER <runtime_notice kind="reflection">          transient=True
```

结束后保留：

```text
reflection 不持久化。
它只是提醒下一次 LLM 行动，不成为长期历史事实。
```

对 prefix cache 的影响：

```text
只追加在 runtime tail，不改写 static_system 或 persisted_history 旧内容。
```

### 2.3 repair：状态同步修复

场景：模型已经用自然语言说了方案，但没有调用对应工具写结构化状态。

例子：模型说了旅行画像，但 `trip_brief` 为空；模型说了骨架方案，但 `skeleton_plans` 为空；模型写了每日行程文本，但 `daily_plans` 没保存。

注入位置：

```text
某次 LLM 返回没有 tool_calls，只有文本：

[0] SYSTEM static_system                         transient=True
[1..n] persisted_history                         transient=False
[n+1] USER current_user                          transient=False
[n+2] USER <turn_context>                        transient=True

AgentLoop 先记录 assistant 文本：

[n+3] ASSISTANT "我为你设计了三套骨架方案..."     transient=False

repair_hints 检测到结构化状态缺失，于是追加：

[n+4] USER <runtime_notice kind="repair">        transient=True
      [状态同步提醒]
      你刚刚已经给出了骨架方案，但 skeleton_plans 仍为空。
      请先调用 set_skeleton_plans(...)
```

然后 AgentLoop `continue`，让下一次 LLM 调用看到这条 repair notice。

结束后保留：

```text
保留：assistant 文本
移除：repair notice
```

注意：

```text
repair notice 不代表用户说了这句话。
它是 Harness 对模型行为的纠偏指令，只服务下一次模型行动。
```

### 2.4 continue：中断恢复

场景：上一轮流式输出中断，或者工具已经完成但总结回复中断，需要继续。

continue route 不使用本轮 `current_user`，而是构造 continuation runtime messages：

```text
[0] SYSTEM static_system                         transient=True
[1..n] persisted_history                         transient=False

[n+1] USER <runtime_notice kind="continue">      transient=True
      上一轮回复因网络中断未完成，请从断点继续，不要重复已说内容。

[n+2] USER <turn_context>                        transient=True
```

如果中断类型是工具后总结：

```text
<runtime_notice kind="continue">
你已经调用了工具并获得结果，但总结被中断了。请根据已有的工具结果继续回复。
</runtime_notice>
```

结束后保留：

```text
continue notice 不持久化。
继续生成出来的 assistant 回复会持久化。
```

## 3. App Event 总览

`<app_event>` 都是：

```text
role=USER
transient=False
持久化：是
语义：应用生成的历史事件，不是用户请求，也不是系统规则
```

但 `app_event` 不等于“永远放在最后”。它放在语义上应该承接的位置。

当前代码里的大多数 `app_event` 是通过：

```text
active_runtime_messages(session).append(app_event_message(...))
```

追加到当前 active runtime list。因为 `app_event` 是 `transient=False`，本轮结束后会被清理逻辑保留下来，并进入后续 persisted history。

### 3.1 history_summary：历史压缩摘要

场景：runtime prompt 超过预算，需要把旧历史压缩成摘要。

压缩前：

```text
[0] SYSTEM static_system                         transient=True
[1] USER old history A                           transient=False
[2] ASSISTANT old history B                      transient=False
[3] USER old history C                           transient=False
[4] ASSISTANT recent history D                   transient=False
[5] USER current_user                            transient=False
[6] USER <turn_context>                          transient=True
```

压缩触发后，summary source 来自较旧的 compressible messages。重建顺序是：

```text
[0] SYSTEM static_system                         transient=True
[1..k] must_keep                                 transient=False
[k+1] USER <app_event kind="history_summary">    transient=False
      [对话摘要]
      用户想去日本，偏好安静酒店，预算一万...

[k+2..] recent messages                          mixed
```

所以 `history_summary` 的位置是：

```text
static_system + must_keep + history_summary + recent
```

它不是动态 tail，而是旧 history 的压缩替代物。

当前实现的几个细节：

```text
static_system：
  如果 index 0 是 SYSTEM，会被显式保留，不进入 summary_source。

recent：
  使用当前 message list 的最后 4 条，通常包含 current_user / turn_context，
  以及本轮刚追加的 tool/result/notice/event。

summary_source：
  优先选择 recent 之外的 older_compressible；
  如果 older_compressible 太少，才退回 compressible。
```

因此 `<turn_context>` / `<runtime_notice>` 正常情况下因为位于 recent tail，不会进入 history summary；旧的 `<app_event>` 如果变成较旧 compressible，则可能被后续 summary 再次摘要化。

结束后保留：

```text
history_summary 会作为 persisted history 保留。
被压缩的旧消息从 active runtime context 中移除；底层存储是否保留原文是另一层审计问题。
```

对 prefix cache 的影响：

```text
压缩会替换历史前缀，因此会开启新的 cache epoch。
这是改写/替换，不是 append-only 追加。
```

### 3.2 soft_judge：软质量评估建议

场景：`save_day_plan`、`replace_all_day_plans` 或 `generate_summary` 的工具结果之后，内部质量评估发现改进建议，但不阻塞当前工具结果返回。

注入位置：

```text
LLM 调用工具后：

[0] SYSTEM static_system                         transient=True
[1..n] persisted_history                         transient=False
[n+1] USER current_user                          transient=False
[n+2] USER <turn_context>                        transient=True
[n+3] ASSISTANT tool_calls: save_day_plan        transient=False
[n+4] TOOL save_day_plan result                  transient=False

after_tool_result soft judge 追加：

[n+5] USER <app_event kind="soft_judge">         transient=False
      行程质量评估（3.6/5）：
      - 第 2 天路线绕路
      - 午餐时间偏紧
```

结束后保留：

```text
soft_judge 只有在存在 suggestions 时才注入 app_event；没有建议时只产生 internal_task 结果。
有 suggestions 的 soft_judge 当前实现会持久化。
下一轮它会出现在 persisted_history 中，而不是 runtime_notice tail 中。
```

设计备注：

```text
如果 soft_judge 只是指导下一步修正，它也可以被重新设计为 runtime_notice。
但当前实现把它作为 app_event，即“应用生成的历史反馈”保留。
```

### 3.3 feasibility：可行性检查未通过

场景：Phase 1 -> Phase 2 阶段推进前，Harness 检查当前旅行计划不可行。

注入位置：

```text
工具调用导致可能发生阶段推进：

[0] SYSTEM static_system                         transient=True
[1..n] persisted_history                         transient=False
[n+1] USER current_user                          transient=False
[n+2] USER <turn_context>                        transient=True
[n+3] ASSISTANT tool_calls                       transient=False
[n+4] TOOL tool_result                           transient=False

phase transition gate 检查失败：

[n+5] USER <app_event kind="feasibility">        transient=False
      [可行性检查]
      当前旅行计划存在以下问题：
      - ...
      请调整后再继续。
```

结束后保留：

```text
feasibility 当前实现会持久化。
它记录了“这次阶段推进为什么没通过”。
```

设计备注：

```text
如果只想让模型立刻修正，这类 gate feedback 也可以考虑改为 runtime_notice。
当前实现选择 app_event，是把它当作阶段推进失败的历史事件。
```

### 3.4 hard_constraint：硬约束冲突

场景：阶段推进前发现硬约束冲突，例如预算、用户明确约束等不可忽略的问题。

注入位置：

```text
[0] SYSTEM static_system                         transient=True
[1..n] persisted_history                         transient=False
[n+1] USER current_user                          transient=False
[n+2] USER <turn_context>                        transient=True
[n+3] ASSISTANT tool_calls                       transient=False
[n+4] TOOL tool_result                           transient=False
[n+5] USER <app_event kind="hard_constraint">    transient=False
      [质量门控]
      硬约束冲突，必须修正：
      - ...
```

结束后保留：

```text
hard_constraint 当前实现会持久化。
下一轮模型能看到之前阶段推进被硬约束挡住过。
```

### 3.5 quality_gate：质量门控未通过

场景：Phase 2 -> Phase 3 或 Phase 3 -> Phase 4 阶段推进前，质量评分低于阈值，且还没有达到最大重试次数。

注入位置：

```text
[0] SYSTEM static_system                         transient=True
[1..n] persisted_history                         transient=False
[n+1] USER current_user                          transient=False
[n+2] USER <turn_context>                        transient=True
[n+3] ASSISTANT tool_calls                       transient=False
[n+4] TOOL tool_result                           transient=False
[n+5] USER <app_event kind="quality_gate">       transient=False
      [质量门控]
      当前方案评分 3.2/5，低于阈值 4.0。
      请修正后再进入 Phase 3：
      - ...
```

结束后保留：

```text
quality_gate 当前实现会持久化。
它是阶段推进失败的应用事件。
如果已经达到最大重试次数，当前实现会允许继续，不再注入 quality_gate app_event。
```

对 prefix cache 的影响：

```text
作为 app_event，它在当前轮是追加；
跨轮进入 persisted_history，属于 append-only 增长。
只要不压缩/重建旧历史，就不破坏已有 prefix。
```

### 3.6 backtrack：阶段回退

场景：用户或系统触发从后续阶段回退到前置阶段。

这类事件不是简单追加到最后，而是在阶段回退重建 messages 时放到回退语境里。

重建后：

```text
[0] SYSTEM static_system                         transient=True
[1] USER original_user_message                   transient=False
[2] USER <app_event kind="backtrack">            transient=False
    [阶段回退]
    用户从 phase 3 回退到 phase 1，原因：想换目的地

[3] USER <turn_context>                          transient=True
    回退后的阶段、工具、规划状态、记忆
```

这个结构来自 phase change rebuild，不是普通 tail append。回退前的当前 epoch 会先被 flush，随后进入新的 context epoch，并用回退后的 phase prompt / tool list / plan state 重建 runtime messages。

结束后保留：

```text
backtrack 会持久化。
它记录了阶段回退这个流程事实。
```

对 prefix cache 的影响：

```text
阶段回退通常会重建 static_system / runtime context，并进入新的 context epoch。
因此它比普通 append-only app_event 更容易重置 cache。
```

## 4. 总结心智模型

不要只按“在列表最后还是中间”记忆，而要同时看两个维度：

```text
1. 运行时位置：
   这条消息是在本轮 prompt 的哪里被模型看到？

2. 生命周期：
   本轮结束后它是否进入 persisted_history？
```

最终分类：

| 类型 | 生命周期 | 典型位置 | 例子 |
|---|---|---|---|
| `runtime_notice` | transient，一次性 | runtime tail，通常在下一次 LLM 调用前追加 | validation / reflection / repair / continue |
| `app_event` | persistent，跨轮 | 事件发生的历史位置 | history_summary / soft_judge / feasibility / hard_constraint / quality_gate / backtrack |
| 普通 history message | persistent，跨轮 | persisted_history | 用户消息 / assistant 回复 / tool call / tool result |
| `turn_context` | transient，一次性 | 当前用户消息之后的 runtime tail | 时间 / 阶段 / 工具 / TravelPlanState / 记忆 |

最重要的区别：

```text
runtime_notice = Harness 给 Agent 的临场控制提醒。
app_event = Harness 写入上下文历史的应用事件。
assistant tool call / tool result = 普通持久历史消息，不是 XML-like 动态内容。
```
