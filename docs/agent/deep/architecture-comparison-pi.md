# 架构对比：与 earendil-works/pi 的实践

## 什么时候读

当任务涉及控制回路可扩展性、状态隔离、运行中引导（steering）、会话压缩、Harness 分层等架构演进决策时读取。本文是对比分析，不是实现规范；落地某项前应再读对应 slice / deep。

## 对比对象

- **本项目**：`backend/agent/loop.py::AgentLoop.run()` 为核心控制回路，外围服务经 `build_agent`（`backend/api/orchestration/agent/builder.py`）依赖注入接入。
- **earendil-works/pi**：monorepo，核心包 `@earendil-works/pi-agent-core`。回路 `runLoop`（`packages/agent/src/agent-loop.ts`），上层有 `AgentHarness` 编排层与 `AgentSession` 会话层，外围能力经 Extension System 统一接入。

两者回路骨架相似（有界迭代 → LLM turn → tool batch → 状态反馈 → 下一轮），差异主要在**扩展点模型、状态隔离、运行中引导、会话压缩、分层抽象**五处。

## 问题 1：没有统一扩展点，外围能力靠硬编码 hook 散点接入

### pi 的做法

独立 Extension System。所有外围能力（工具、命令、事件订阅）通过 `pi.on() / pi.registerTool() / pi.registerCommand()` 统一接入，由 `ExtensionRunner` 桥接到 `AgentSession`。生命周期事件是全集：`session_start/shutdown`、`agent_start/end`、`turn_start/end`、`tool_call/result`、`message_end`。pi 明确说明这是“从旧 hooks 系统重构进化而来，把 hooks 和 custom tools 统一成 extension”。

### 本项目现状

`backend/api/orchestration/agent/hooks.py` 手工注册 4 个固定 hook 点：

```
before_llm_call, after_tool_call, after_tool_result, before_phase_transition(gate)
```

这些 hook 的实现写死在 builder 装配里（soft_judge、quality_gate、validate、压缩、reflection 注入），不是可插拔扩展。新增能力（审计日志、外部审批）只能改 `hooks.py` 和 `builder.py`。

### 问题本质

外围服务接入点是**封闭枚举**，pi 是**开放注册**。本项目 hooks 还混着两类东西——纯通知型（`before_llm_call`）和门控型（`before_phase_transition` 的 gate）——但都挤在同一个 HookManager 里，没有区分。

### 改进方向

- 把 hook 点扩展成覆盖完整生命周期的枚举（补 `session_start/end`、`turn_start/end`、`tool_call`、`message_end`）。
- 区分 **event hook**（通知型，可多订阅、不阻断）和 **gate hook**（门控型，可阻断，如 `before_phase_transition`）。已有 `register_gate`，但只有 1 处用到，未形成范式。
- 引入轻量 extension 概念：一个扩展 = {事件订阅 + 注册的工具 + 注册的命令} 的 bundle，而不是散在 builder 里的函数。

## 问题 2：缺少 Turn Snapshot，运行中配置变更会污染当前 turn

### pi 的做法

`AgentHarness` 把状态显式分四层，其中 **Turn Snapshot** 是关键。`createTurnState()` 为单次 LLM turn 生成不可变快照（含 model、thinking level、tools、resources、system prompt）。运行时配置变更只影响 Harness Config，“不影响当前正在运行的 provider request”，只影响下一个 turn。四层：

1. **Harness Config**：最新运行时配置（model、thinking、tools、resources）。
2. **Turn Snapshot**：单次 turn 的不可变状态，turn 内所有逻辑只用这份。
3. **Session**：仅持久化条目，不含 pending writes。
4. **Pending Session Writes**：运行中产生的写，在 save point 顺序 flush。

### 本项目现状

全项目搜 `turn_snapshot / TurnSnapshot / save_point` 零命中。`AgentLoop.run()` 直接读 `self.plan`、`self.tool_engine`、`self.llm` 等实例属性，在整个 run 期间可变。例如 `tools = transition_outcome.tools` 就地覆盖循环变量——phase transition 中途换工具集是即时生效的，没有“当前 turn 用快照、变更延迟到下轮”的隔离。

### 问题本质

回路在迭代过程中共享可变状态。目前因单线程 async 且 transition 在 turn 结束后才换 tools，恰好没出问题，但这是“靠执行顺序侥幸正确”，不是“靠设计保证正确”。一旦加入并行 turn、中途 steering 改配置、或 hot-reload 工具，就会出竞态。

### 改进方向

引入 turn-scoped 快照：每次 `run_llm_turn` 前 freeze 一份 `{messages, tools, model, phase}` 的不可变视图传入，transition 产生的变更写回 harness config，下一轮 turn 重新 snapshot。这也让 context rebuild 逻辑更清晰——rebuild 即生成新 snapshot。

## 问题 3：没有 Steering 机制，continue 是粗粒度重启

### pi 的做法

显式 `steeringQueue` + `getSteeringMessages` 回调。用户在 agent 运行中插入的消息（“换个方向”、“停用某工具”）进入队列，在下一次 LLM 调用前注入，不中断当前 turn。`steeringMode` 可选 `all` 或 `one-at-a-time`。

### 本项目现状

`continue_chat`（`backend/api/routes/chat_routes.py:355`）是独立 endpoint，本质是从中断点重新启动一个 run，不是向运行中的 run 注入消息。`cancel_event` 只能停，不能“边跑边引导”。

### 问题本质

Human-Agent Loop 是停-走式的（用户必须等 agent done 才能输入），pi 是流式可引导式的。旅行规划是长 run（Phase 3 逐日排程可能跑很久），用户无法中途纠偏，只能等跑完或取消重来。

### 改进方向

给 `AgentLoop` 加 `steering_queue: asyncio.Queue`，在 `run_llm_turn` 开头 drain 队列、把 steering 消息拼到 messages 前。前端加运行中输入框，走单独 `POST /api/chat/{session_id}/steer`。改动可控，体验提升明显。

## 问题 4：Compaction 是 prompt 级裁剪，没有 session 级摘要持久化

### pi 的做法

Compaction 是会话级的：找到 cut point → 用 LLM 对旧消息生成摘要 → 存 `CompactionEntry`（summary + firstKeptEntryId）→ reload session 时用摘要替代旧消息。摘要可迭代（用 previous summary 做下一轮）。处理了“单 turn 超预算”的 split-turn 情况。

### 本项目现状

`backend/agent/compaction.py::compact_messages_for_prompt` 是每次构造 prompt 时临时裁剪——估算 token、丢掉/压缩旧 tool result（`compact_tool_message`、`_compact_web_search_data`）。这是 prompt 构造时的有损过滤，不是会话级摘要。没有 `CompactionEntry`、没有持久化摘要、没有 cut point 概念。

### 问题本质

Compaction 是无状态的、每轮重算的。旧信息要么全留要么按规则裁掉，没有“压缩成摘要保留语义”的能力。长会话里，Phase 1/2 早期的重要决策（如“用户最初想去北海道但预算不够改成了关西”）会在裁剪中丢失语义。

### 改进方向

已有 reflection 和 memory extraction 可复用。在 session 级引入 `CompactionEntry`：当 messages token 超阈值时，对 cut point 之前的消息调一次 LLM 摘要（可复用 memory extraction 的 LLM），存到 SQLite，prompt 构造时用“摘要 + recent messages”。与现有 `context_epoch` rebuild 机制天然契合。

## 问题 5：外围服务分层没有显式化

### pi 的做法

三层显式抽象，职责清晰，每层只调下层：

- `Agent`：核心 LLM 接口与回路。
- `AgentHarness`：编排层，管配置/资源/持久化/锁。
- `AgentSession`：会话层，管交互/扩展/分支。

### 本项目现状

外围服务运行时其实不平级（见 `slices/architecture.md` 的分层注记）：soft_judge / quality_gate / memory_gate 在运行时新建 `judge_llm` / `gate_llm`，是 LLM 的二级消费者；hooks 和 phase router 会反向重写回路输入。但这套分层只存在于代码隐式行为里，没有显式抽象。`build_agent` 把所有东西平铺喂给 `AgentLoop`，回路同时直接持有 LLM、tool_engine、memory_mgr、hooks、phase_router、context_manager——回路知道太多。

### 问题本质

`AgentLoop` 承担了 pi 里 `Agent` + `AgentHarness` 两层职责。既跑核心 loop，又管 phase transition、context rebuild、工具切换、memory 注入。导致 `loop.py` 的 `run` 方法约 258 行、全文 841 行，单方法偏重，且无法单独测试“纯 loop”。

> 注：复杂度论点成立（258 行单方法偏重），但拆分动机应基于可观测成本（测试困难、回归面大），而非“回路知道太多”这一设计品味判断。

### 改进方向

拆出 Harness 层，把“配置管理 + 持久化 + context rebuild + phase transition 决策”上移，让 `AgentLoop` 回归“纯 LLM turn + tool batch”的瘦回路。收益：纯 loop 可独立测试；transition 逻辑可复用；问题 2 的 turn snapshot 自然落地在 Harness 层。

## 问题 6（次要）：工具结果直接进 transcript，缺 message_end 替换能力

### pi 的做法

`message_end` 事件允许扩展替换处理完的消息——比如把冗长 tool result 替换成精简版再存 transcript。

### 本项目现状

tool result 执行完直接 append 到 messages，replace 能力分散在 `message_filters.py` 和 compaction 里，没有统一钩子。长行程（大量 search 结果）时 transcript 膨胀。

### 改进方向

小改动：给 `after_tool_result` 加“返回替换内容”协议，统一收口。

## 改进优先级

优先级同时考虑收益与成本/风险。`P0-if-3` 表示该问题的紧迫性依赖问题 3 落地后才成立，孤立看今天不痛。

| 优先级 | 问题 | 收益 | 成本 / 风险 |
|---|---|---|---|
| P0-用户可感 | 3 Steering | Phase 3 长 run 中途纠偏，用户能直接感知的体验刚需。 | 中：一个 `asyncio.Queue` + 一个 `/steer` endpoint，主回路只在 `run_llm_turn` 开头 drain，侵入面可控。 |
| P0-if-3 | 2 Turn Snapshot | 消除“靠执行顺序侥幸正确”的隐藏竞态；是问题 3/5 的前置。 | 中：需冻结 `{messages, tools, model, phase}` 视图，transition 变更改写 harness config 而非就地覆盖。**孤立看今天不痛**——单线程 async + turn 结束才换 tools，目前不出竞态；紧迫性来自问题 3 引入运行中注入后才会触发。 |
| P1 | 4 Session 级 Compaction | 长会话早期决策语义保留，复用已有 memory extraction LLM。 | 中低：新增 `CompactionEntry` 表 + 摘要 LLM 调用 + prompt 构造改造，但与现有 `context_epoch` rebuild 机制契合，回归面有限。 |
| P2 | 5 拆 Harness 层 | 降 `loop.py` 复杂度，纯 loop 可独立测试，是 2/3 的承载层。 | **高**：动 `loop.py` 这个全项目最核心文件，涉及 phase transition / context rebuild / 工具切换所有主路径的回归。“回路知道太多”是设计品味论证，不是可观测成本；此条是否做应取决于 2/3 落地后是否确实需要承载层，不宜为品味单独驱动。 |
| P2 | 1 统一扩展点 | 长期可扩展性，区分 event hook / gate hook。 | 中：需重排 hook 生命周期并引入 extension bundle 概念；当前 4 个 hook 点尚够用，非燃眉。 |
| P3 | 6 message_end 替换 | 长 transcript 体积控制。 | 低：给 `after_tool_result` 加返回替换协议。 |

## 反向条目：本项目做得更好或有意更简的地方

以上六条都是“本项目不如 pi”的逆向差。一份可信的对比必须也列出正向差——本项目作为垂直旅行 Agent，有几条 pi 作为通用框架没有对应物的设计。

### 1. Phase 1/2/3/4 是业务领域状态机，不是运行时操作状态

本项目 `PhaseRouter`（`backend/phase/router.py`）的 phase 是旅行规划业务状态机：Phase 1 目的地收敛 → Phase 2 画像/候选/骨架/锁定 → Phase 3 逐日详排 → Phase 4 查漏冻结。带 `phase2_step` 子状态、`red_flags` 风险标记、`backtrack` 回退。`infer_phase` 从 `TravelPlanState` 推断当前阶段。

pi 的 phase 是**运行时操作状态**（idle/turn/compaction/branch_summary/retry），用于防止并发结构性操作，不含任何业务语义。这是通用框架与垂直应用的根本差异——本项目把领域进度建模成状态机，pi 把执行状态建模成状态机。

### 2. `TravelPlanState` 是领域结构化对象，不是消息流

本项目的权威状态是 `TravelPlanState`（`backend/state/models.py`），含 `destination`、`daily_plans`、`skeleton_plans`、`phase2_step`、`preferences` 等领域字段。pi 的权威状态是 Session 的 persisted entries（JSONL 消息树），没有领域结构化对象——它的“状态”是消息流，领域语义由上层应用自行解释。

> 修正：先前版本曾把“单一写通道”和“权威状态 vs 记忆”也列为 pi 没有的对应物。核实后不成立——pi 通过 `Session` 对象 + `pendingSessionWrites` 在 save point flush 实现了等价的单一写通道，四层状态分离也是“权威状态”的工程化。这两条是双方共有，不构成反向差异。

### 3. plan_writer 把“写状态”暴露为 LLM 工具

本项目通过 `make_all_plan_tools`（`backend/tools/plan_tools.py`）把 `write_skeleton_plans`、`write_candidate_pool`、`write_transport_options` 等 plan writer 注册为 LLM 可调用工具（`backend/api/orchestration/agent/tools.py:38`）。LLM 通过工具调用来写状态，写通道、参数校验、phase 门控统一收口在工具层。

pi 的写是内部 session append，不暴露给 LLM 作为工具——LLM 只能产出消息，不能直接结构化写状态。这是本项目“让 LLM 显式驱动领域状态变更”的设计选择，pi 没有对应物。

## 该学 vs 无需付费：通用性税的切分

pi 是多租户 / 扩展生态 / IDE 式通用框架，它的部分抽象层是**通用性税**——为让任意第三方写扩展、任意模型热切换、会话可分支才被迫付的复杂度。本项目是单领域垂直应用，没有第三方扩展、没有会话分支、模型基本固定。照搬抽象层有 over-engineering 风险。

### 该学（垂直场景也受益）

- **Steering（问题 3）**：与通用性无关，长 run 中途纠偏是任何长任务 agent 的刚需。
- **Turn Snapshot（问题 2）**：与通用性无关，状态隔离是正确性保证，不是扩展性需求。
- **Session 级 Compaction（问题 4）**：长会话语义保留，垂直场景同样需要。

### pi 因通用性被迫承担、本项目无需付费

- **Extension System 完整生态（问题 1）**：pi 需要支持任意第三方 `pi.registerTool/registerCommand/on`，必须有 `ExtensionRunner` + 生命周期事件全集 + 动态加载。本项目没有第三方扩展，4 个固定 hook 点是**合理简化而非缺陷**，完整 Extension System 是过度设计。
- **会话分支 / JSONL 树（问题 5 的部分）**：pi 的 Session 是 `id/parentId` 树结构支持 branching，用于 IDE 式“探索不同方案”。本项目一个 session 一条主线，无需分支。
- **多模型热切换 + cross-provider handoff**：pi 的 `model registry` + `credential resolution` + provider 动态注册服务于“用户随时切模型”。本项目模型基本固定，`llm_factory` 已够用。

### 看情况（取决于问题 2/3 是否落地）

- **拆 Harness 层（问题 5）**：如果只是品味上“回路知道太多”，不值得动核心文件；如果 Turn Snapshot 和 Steering 落地后确实需要承载层，再拆。**不要为品味单独驱动大重构**。

## 共性根因

问题 1/2/5 共享一个根因：**控制回路与编排职责未分离**。pi 用 `Agent` / `AgentHarness` / `AgentSession` 三层把“纯回路”“配置与状态编排”“会话与扩展”切开；本项目把它们压进一个 `AgentLoop`。

但需注意：pi 的三层里，`AgentSession`（扩展/分支）层对本项目是通用性税，不该照搬；真正该学的是 `AgentHarness` 那一层对**状态隔离**（问题 2）和**配置/持久化编排**的分离。先落地问题 3（Steering）和问题 2（Turn Snapshot），再根据承载需求决定是否拆 Harness——不要反过来先拆层再找需求。

## 深入阅读

- 系统边界与模块职责：`../slices/architecture.md`
- 主链路与 AgentLoop 顺序：`../slices/data-flow.md`
- 上下文构建与压缩：`../slices/context-compression.md`
- 质量守护分层：`harness-architecture.md`
- 对比对象 pi 仓库：`https://github.com/earendil-works/pi`
