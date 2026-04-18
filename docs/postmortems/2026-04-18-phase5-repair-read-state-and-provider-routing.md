# Phase 5 修复型任务裸工具调用泄漏根因复盘

- 事故日期：2026-04-18
- 问题 session：`sess_633cc86ff6c9`
- 事故范围：`backend/context/manager.py`、`backend/main.py`、`backend/llm/anthropic_provider.py`、`backend/agent/loop.py`、`backend/phase/prompts.py`
- 事故类型：Phase 5 已完成态修复任务状态不可读 + provider 路由配置未生效 + 第三方工具协议泄漏
- 事故等级：中
- 事故状态：已定位根因，待修复

---

## 1. 事故摘要

在第三轮 Phase 5 真实前端复测中，用户要求修复当前逐日行程里的所有时间冲突，并明确要求使用 `replace_daily_plans` 传入完整 `days` 数组整体替换。

实际结果是 assistant 没有产生任何结构化 tool call，而是在前端和数据库中输出了原始工具调用协议文本：

```xml
<invoke name="get_trip_info">
</invoke>
</minimax:tool_call>
```

Trace 显示本轮：

- `llm_call_count = 1`
- `tool_call_count = 0`
- `tool_calls = []`
- `state_changes = []`
- `errors = []`

因此本轮没有实际修复 `daily_plans`，也没有触发工具错误或 repair 重试。系统把一次无效的裸工具协议输出当成了正常完成。

---

## 2. 用户可见现象

用户在 Phase 5 session 中发送：

```text
请修复当前逐日行程里的所有时间冲突，必须保留 2026-05-24 到 2026-05-30 共 7 天；不要重新选择交通和住宿。需要更新状态时，请用 replace_daily_plans 传入完整 days 数组整体替换。修复完成后再简短总结。
```

前端随后展示：

```xml
<invoke name="get_trip_info">
</invoke>
</minimax:tool_call>
```

该内容也以普通 assistant 消息形式落库，`tool_calls` 为空。

---

## 3. 当前已确认正常的部分

本轮复测同时确认前两次 Phase 5 状态契约修复已有成效：

1. 没有再次出现 `replace_daily_plans({})` 空参数 TypeError。
2. 没有再次出现 Day 7 超出 6 天行程长度的问题。
3. API 当前状态为 Phase 5，日期为 `2026-05-24` 至 `2026-05-30`，`daily_plans_count = 7`。
4. SQLite `sessions` 元数据已经同步到 Phase 5，标题为 `九寨沟 + 成都 · 7天6晚`，不再停留在旧的 Phase 3 stale 状态。

这说明本次事故不是旧的天数状态契约问题，而是新的 Phase 5 已完成态修复任务问题。

---

## 4. 关键证据

### 4.1 数据库证据

`messages` 表中最新 assistant 消息：

```text
seq=24
role=assistant
content=
<invoke name="get_trip_info">
</invoke>
</minimax:tool_call>
tool_calls=[]
```

全库检索裸 XML 工具协议，共 3 条：

```text
sess_633cc86ff6c9 | seq 24 | get_trip_info
sess_5b4a037df06b | seq 3  | select_transport
sess_5b4a037df06b | seq 7  | select_transport
```

说明问题不是前端渲染误判，而是后端已经把裸协议文本作为 assistant content 持久化。

### 4.2 prompt / runtime context 证据

本轮 Phase 5 system prompt 中：

- 不包含 `get_trip_info`
- 不包含 `minimax`
- 不包含 `<invoke`
- 包含 `replace_daily_plans`

Phase 5 当前规划状态中虽然注入了 `daily_plans` 进度，但只注入每天前几个活动名称：

```text
- 已规划 7/7 天
  - 第1天（2026-05-24）：上海飞抵成都、午餐、入住酒店、逛春熙路太古里...
```

没有注入每个活动的 `start_time`、`end_time`、`transport_duration_min`。模型看不到真实时间表，无法判断或修复时间冲突。

### 4.3 工具注册证据

全代码库没有 `get_trip_info` 工具定义。当前 Phase 5 可用工具包括：

- `append_day_plan`
- `replace_daily_plans`
- `assemble_day_plan`
- `calculate_route`
- `check_availability`
- `check_weather`
- `get_poi_info`
- `web_search`
- `xiaohongshu_search`
- `request_backtrack`

缺少“读取当前完整 plan / daily_plans”的只读工具。

### 4.4 provider 路由证据

`config.yaml` 声明：

```yaml
llm_overrides:
  phase_5:
    provider: "openai"
    model: "gpt-4o"
```

但实际 `_build_agent()` 始终使用默认 LLM：

```python
llm = create_llm_provider(config.llm)
```

当前环境实际创建出的 provider 是：

```text
AnthropicProvider anthropic astron-code-latest
phase5 override configured: openai/gpt-4o
```

也就是说 Phase 5 配置期望走 OpenAI/gpt-4o，但运行期没有使用 `llm_overrides`，实际走的是 Anthropic-compatible 的 `astron-code-latest`。

### 4.5 provider 解析证据

`AnthropicProvider._emit_nonstream_response()` 只识别：

- `block.type == "text"` -> 直接输出 `TEXT_DELTA`
- `block.type == "tool_use"` -> 转成内部 `ToolCall`

当上游模型返回 `<invoke ...></invoke></minimax:tool_call>` 作为 text block 时，provider 没有任何拦截、解析或错误化处理，最终直接透传到 SSE、前端和数据库。

### 4.6 repair 机制证据

`AgentLoop._build_phase5_state_repair_message()` 只处理“模型输出了逐日行程文本但 daily_plans 尚未写满”的情况。

当前 session 已经是 `7/7` 天，因此该函数在检测到 `planned_count >= total_days` 后直接返回，不会注入 repair 提醒。

用户本轮明确要求“修复时间冲突”，但系统没有“修复型任务必须产生写工具调用”的完成门控。

---

## 5. 根因分析

### 5.1 直接根因

第三方模型通过 Anthropic-compatible provider 返回了 MiniMax 风格 XML 工具调用文本，而 provider 没有解析或拦截该文本，导致裸工具协议被当作普通 assistant 文本输出。

### 5.2 上游诱因一：Phase 5 修复任务看不到完整现有状态

用户要求修复已有 `daily_plans` 的时间冲突，本质需要读取完整已有行程：

- day
- date
- activity name
- start_time
- end_time
- transport_duration_min
- location
- category
- cost

但当前 runtime context 只展示每天的活动名摘要。模型没有足够信息构造完整 `replace_daily_plans(days=[...])` 参数。

于是模型尝试调用一个不存在的读取工具 `get_trip_info`。这不是 prompt 中显式出现的工具，而是模型在“需要读取当前状态但工具集不给读状态能力”的压力下自造的工具名。

### 5.3 上游诱因二：工具设计缺少只读状态查询工具

Phase 5 prompt 要求：

- 生成/修改行程必须写 `daily_plans`
- 修改已有天数时用 `replace_daily_plans(days=[...])`
- 修复冲突时整体替换

但工具集只提供写入工具，没有提供读取完整当前 plan 的工具。只要上下文摘要不足，模型就无法安全完成“基于当前状态修改”的任务。

这是一种工具契约不完整：系统要求模型“整体替换完整状态”，但没有稳定地给模型“完整旧状态”。

### 5.4 上游诱因三：`llm_overrides` 配置未生效

配置层已经声明 Phase 5 应走 `openai/gpt-4o`，但 agent 构建层没有根据 phase 使用 override。

这导致团队对复测环境的认知和实际运行环境不一致：

- 以为测试的是 OpenAI/gpt-4o 的 function calling 行为
- 实际测试的是 Anthropic-compatible `astron-code-latest` 的工具协议行为

裸 XML 中出现 `minimax:tool_call`，与该类第三方兼容模型/网关的历史泄漏行为一致。

### 5.5 下游放大因素：provider 抽象缺少协议防火墙

provider 层假设上游只会返回标准 Anthropic block：

- text
- tool_use

但真实接入里，模型可能把工具调用降级为文本协议。provider 没有把这种协议文本识别为异常，也没有转换成内部 tool call，更没有阻止它进入 UI。

这使一个模型协议问题变成用户可见事故。

### 5.6 下游放大因素：Phase 5 repair 只面向“未写满”

Phase 5 repair 当前核心判断是 `daily_plans` 是否写满。它不能处理：

- 用户要求修改已写满的行程
- 用户要求修复时间冲突
- 模型输出裸工具协议但没有结构化 tool call
- 模型零 tool call 却声称完成

因此在 `7/7` 已写满状态下，repair 机制完全失效。

---

## 6. 为什么这次不同于 2026-04-16 XML 泄漏事故

2026-04-16 的事故主链路是：

1. Phase 3 -> 5 handoff 携带了上一阶段确认语义。
2. 模型在 Phase 5 重复尝试调用 Phase 3 的 `select_transport`。
3. 当前工具列表不含 `select_transport`。
4. MiniMax XML 泄漏。

本次事故主链路是：

1. 用户在 Phase 5 已完成态要求修复已有 `daily_plans`。
2. 上下文只提供 daily plan 摘要，不提供完整时间字段。
3. 工具集没有读取完整 plan 的只读工具。
4. 模型自造 `get_trip_info`。
5. Anthropic-compatible provider 把 XML 工具协议当 text 透传。
6. Phase 5 repair 因 7/7 已写满而不触发。

两次事故共享“provider 未拦截 XML 协议”的下游问题，但上游诱因不同。本次不是 phase handoff 污染，而是 Phase 5 修复型任务的状态可见性和工具契约缺口。

---

## 7. 影响评估

### 7.1 用户影响

- 用户看到底层工具协议文本，体验断裂。
- 用户要求修复时间冲突，但系统没有实际修改状态。
- UI 和 trace 都显示 completed，容易误导调试者以为任务成功。

### 7.2 系统影响

- Phase 5 “已写满后的修改/修复”路径缺少完成门控。
- `llm_overrides` 配置与运行行为不一致，后续评估可能误判模型能力。
- provider 层没有隔离第三方模型协议差异。
- trace 对“裸工具协议泄漏”没有错误标记，观测性不足。

---

## 8. 修复建议

### 8.1 P0：让 `llm_overrides` 真正生效

在 agent 构建或 LLM 调用前按当前 phase 选择 LLM config，例如：

- Phase 1/2 使用 `llm_overrides.phase_1_2`
- Phase 5 使用 `llm_overrides.phase_5`
- 未命中 override 时回退 `config.llm`

同时 trace/stats 应记录实际 provider/model，而不是默认 `config.llm`。

### 8.2 P0：拦截裸工具协议文本

provider 或 AgentLoop 应识别以下模式：

- `<invoke name=`
- `</minimax:tool_call>`
- `<minimax:tool_call>`

处理策略：

1. 能解析且工具名在当前工具列表中：转换为内部 `ToolCall`。
2. 工具名未知：不要展示给用户，生成可诊断错误或注入 repair message。
3. 无法解析：作为 protocol error 记录，并返回用户友好错误。

最低要求：裸 XML 不允许作为普通 assistant content 落库和展示。

### 8.3 P0：Phase 5 修复任务必须有写工具闭环

当用户本轮包含以下意图时：

- 修复
- 调整
- 替换
- 解决冲突
- 修改第 N 天
- 优化时间

如果本轮没有产生任何结构化 tool call，不能直接 completed。应注入 repair：

```text
你正在处理已存在 daily_plans 的修改请求，但本轮没有调用写入工具。
请使用 replace_daily_plans(days=[...]) 提交完整修复后的 daily_plans。
```

### 8.4 P1：Phase 5 已有行程上下文注入完整关键字段

当前 `build_runtime_context()` 对 daily_plans 只注入活动名摘要。Phase 5 已存在 daily_plans 时，应至少注入：

- `day`
- `date`
- `name`
- `start_time`
- `end_time`
- `transport_duration_min`
- `location.name`
- `category`
- `cost`

可以控制体积，但不能省略修复时间冲突必需的字段。

### 8.5 P1：增加只读状态工具

新增 Phase 5 可用只读工具，例如：

- `get_current_plan`
- `get_daily_plans`
- `get_day_plan(day=...)`

用途：

- 修复已有行程前读取完整状态
- 避免 runtime context 过大时模型仍能按需取数
- 减少模型自造 `get_trip_info` 这类工具名的概率

### 8.6 P1：trace 标记协议泄漏

如果 assistant content 出现疑似工具协议但 `tool_calls=[]`，trace 中应记录：

- `protocol_leak_detected: true`
- `raw_tool_name`
- `known_tool: true/false`
- `phase`
- `available_tools`

这样调试时不会只看到 `tool_call_count=0` 和 `errors=[]`。

---

## 9. 建议回归测试

### 9.1 provider override 生效测试

构造 Phase 5 plan，断言 `_build_agent()` 或等价工厂使用 `config.llm_overrides["phase_5"]`。

### 9.2 裸 XML 拦截测试

Fake provider 返回：

```xml
<invoke name="get_trip_info">
</invoke>
</minimax:tool_call>
```

断言：

- 不会 yield 普通 `TEXT_DELTA` 给前端
- 不会把 XML 作为 assistant content 落库
- trace 或 run status 能记录协议错误/repair

### 9.3 Phase 5 已完成态修复测试

构造 `daily_plans = 7/7` 且含时间冲突，用户说“修复所有时间冲突”。

断言：

- 如果模型没有 tool call，AgentLoop 注入 repair，不直接 completed。
- 如果模型调用 `replace_daily_plans`，trace 中有 state_changes。

### 9.4 runtime context 完整字段测试

构造 Phase 5 plan，已有 daily_plans，断言 system prompt 中包含每个活动的 `start_time`、`end_time` 和 `transport_duration_min`。

---

## 10. 事故定性

本次事故的本质不是“模型偶发乱输出”，而是系统在 Phase 5 已完成态修复任务上缺少闭环：

- prompt 要求整体替换完整 `daily_plans`
- runtime context 没给完整旧 `daily_plans`
- 工具集没有读取完整旧状态的能力
- 实际 provider 与配置预期不一致
- provider 没有协议防火墙
- repair 机制只覆盖未写满，不覆盖已写满后的修改

最终模型自造 `get_trip_info`，第三方兼容层把它降级成 XML 文本，系统又把这段文本当成正常回答完成。

修复重点不应只补一个 XML parser。真正需要补的是 Phase 5 “读当前状态 -> 修改 -> 写回 -> 验证”的闭环，以及 provider 路由和协议边界的可观测性。
