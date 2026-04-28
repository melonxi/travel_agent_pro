# 双轨上下文持久化设计

> **状态：已废弃 / Superseded**
>
> 这份文档是最初的单体设计草稿。经过 review 后，发现它把“历史保全、恢复语义、历史分段诊断”放在同一期实施中，复杂度过高且存在几个关键缺口：缺少 phase rebuild 前 flush、`persisted_message_count` 容易和 runtime list 缩短冲突、恢复视图依赖不足、仅靠 `phase` / `phase3_step` 无法区分重复进入同一阶段。
>
> 后续实施请不要按本文档执行。新的权威设计拆为三期：
>
> 1. `docs/superpowers/specs/2026-04-28-context-history-preservation-design.md`
> 2. `docs/superpowers/specs/2026-04-28-context-runtime-restore-design.md`
> 3. `docs/superpowers/specs/2026-04-28-context-history-segmentation-design.md`

## 背景

当前 Phase 切换后的消息处理分两步完成：

1. 运行时工作集重建：`backend/agent/execution/message_rebuild.py` 在 phase 前进或 Phase 3 子阶段切换后重建 `messages`，只保留新阶段 `system message`、handoff/backtrack note 和触发切换的原始用户消息。
2. 会话持久化覆盖写回：`backend/api/orchestration/session/persistence.py` 的 `persist_messages()` 会先删除该 session 在 `messages` 表中的全部记录，再把当前内存里的 `messages` 整体重新写回 SQLite。

这意味着当前系统不是“仅在 prompt 层做压缩”，而是把压缩后的工作集直接变成了持久化真相源。上一阶段的原始消息、工具调用和工具结果在 phase rebuild 后会一起从持久化层消失。

这一行为满足了当前在线对话的目标：

- 降低跨阶段语义污染
- 保持 prompt 短小稳定
- 让新阶段只看到当前职责相关的信息

但它也带来两个明确问题：

- session 恢复只能拿到“压缩后的当前工作集”，无法恢复完整阶段历史
- phase 回退、诊断、审计时缺少上一阶段真实交互轨迹

问题的根因不是“丢弃策略本身错误”，而是“给 LLM 看的工作上下文”和“系统需要长期保存的历史上下文”被复用了同一份数据结构和同一份持久化写法。

## 目标

把上下文管理拆成两条职责明确的轨道：

1. **运行时轨道**：继续为在线 LLM 调用提供短、小、强隔离的工作消息集。
2. **持久化轨道**：在不改变在线 prompt 行为的前提下，保留按 phase/phase3_step 切分的原始消息历史，供 session 恢复、诊断和后续扩展使用。

## 非目标

- 不改变当前 phase handoff 的 prompt 设计，不恢复“前序阶段摘要”主路径
- 不把历史 phase segment 重新注入 LLM prompt
- 不新增独立 `phase_segments` 表
- 不在本期引入新的前端 phase 历史浏览 UI
- 不改变 `PhaseRouter.infer_phase()`、`check_and_apply_transition()`、backtrack 的业务语义

## 现状判断

### 1. 当前 runtime 丢弃策略是正确的

`2026-04-17-phase-handoff-redesign-design.md` 已明确把 phase handoff 设计成“结构化职责交接”，其核心是：

- 前进切换时不保留上一阶段流水账
- 不把历史 user/assistant/tool 交互继续交给下一阶段模型
- 只保留新阶段 `system message` + handoff note + 必要 user anchor

这套设计的目标是防止新阶段模型被旧阶段任务重新拉偏。该目标仍然成立，因此本设计**不推翻 runtime 丢弃策略**。

### 2. 当前 persistence 覆盖写回是问题所在

`persist_messages()` 目前会清空整段历史后再写入当前工作集，导致 SQLite 里的 `messages` 不再表示“会话全历史”，而只表示“最近一次 rebuild 后的工作集快照”。

这让 SQLite 同时承担了两件互相冲突的事：

- 给当前 LLM 调用提供最短工作集
- 给恢复、回退、诊断提供完整历史

后者在现状下无法满足。

## 决策

### 决策 1：采用双轨策略，但只在职责层双轨，不在 prompt 层双轨

系统拆分为两条轨道：

- **Runtime View**：继续使用当前重建后的 `messages`，供在线 LLM 调用
- **History View**：持久化保留原始消息历史，并给每条消息打上所属 `phase` / `phase3_step`

两条轨道共享同一套基础消息记录，但使用方式不同。

### 决策 2：不新增 `phase_segments` 表，segment 作为派生视图存在

本期不引入新的 segment 存储表。原因：

- 当前已经有 `messages` 表和 `plan_snapshots` 表，新增第三套独立 segment 存储会增加一致性成本
- phase segment 本质是按消息元数据聚合得到的查询视图，不需要单独落表
- 后续若前端或 trace 需要按 phase 展示历史，可通过查询 `messages` 表按 `phase` / `phase3_step` 派生得到

因此，segment 的事实源仍然是 `messages` 表。

### 决策 3：给 `messages` 表增加 phase 标签字段

为 `messages` 表新增以下列：

- `phase INTEGER`
- `phase3_step TEXT`

写入语义：

- 每条消息写入时记录它生成当下所属的 `plan.phase`
- 若处于 Phase 3，则同时记录当下 `plan.phase3_step`
- 其余 phase 的 `phase3_step` 置空

这两个字段只承担“历史归属标记”职责，不改变 runtime rebuild 行为。

### 决策 4：持久化从“覆盖写回”改为“追加写入 + 增量同步元数据”

`persist_messages()` 不再执行 `DELETE FROM messages WHERE session_id = ?` 这种整段抹平式写法。

改为：

1. 把本轮新增消息按顺序 append 到 `messages`
2. 为每条新增消息写入所属 `phase` / `phase3_step`
3. session 恢复时，按需要从完整历史中派生 runtime view

本期目标是让 `messages` 真正重新成为“完整历史消息流”的事实源，而不是“当前工作消息集快照”。

### 决策 5：恢复时显式区分 runtime view 与 history view

恢复 session 时，不再简单地把 `messages` 表全量加载结果直接当作继续对话用的 runtime messages。

恢复逻辑改为产出两个视图：

- `history_view`：该 session 的完整消息历史
- `runtime_view`：根据当前 `plan.phase` 和最新历史记录，重新构造出与在线运行一致的工作集

恢复后的下一轮 LLM 调用必须看到和“未中断情况下”一致的 runtime view，而不是完整历史堆叠。

### 决策 6：phase 回退时，segment 只读，不 replay 回 LLM

回退场景最容易出现设计倒退：为了“找回上下文”，把目标阶段的旧消息重新塞回 prompt。这会直接破坏当前已经建立的阶段隔离。

本设计明确禁止：

- 不把历史 phase segment 直接 replay 到回退后的 LLM prompt
- 不把旧阶段工具调用、工具结果重新作为工作上下文输入给模型

回退后 LLM 看到的仍然是：

- 新阶段 `system message`
- backtrack notice
- 触发回退的原始用户消息

而历史 segment 仅用于：

- session 恢复时构造 history view
- trace / 调试 / 审计
- 后续可能的前端历史展示

### 决策 7：Phase 3 子阶段历史也一并保留，但不引入子阶段专用独立表

由于 Phase 3 的 `brief -> candidate -> skeleton -> lock` 切换同样会触发工作集重建，本设计同样要求为消息打上 `phase3_step` 标签。

这样后续可以按以下粒度派生历史：

- Phase 1
- Phase 3 / brief
- Phase 3 / candidate
- Phase 3 / skeleton
- Phase 3 / lock
- Phase 5
- Phase 7

仍然不新增独立子阶段归档表。

## 模块设计

### A. `backend/storage/database.py`

#### schema 迁移

在 `messages` 表新增两列：

- `phase INTEGER`
- `phase3_step TEXT`

迁移原则：

- 对新库，直接在 `_SCHEMA` 中创建这两列
- 对旧库，在 `_migrate_messages_table()` 中补列
- 历史旧数据允许这两列为空，不做一次性回填

不做历史回填的原因：

- 旧数据已经在历史上被覆盖写平，回填也无法恢复真实 phase 边界
- 本次设计目标是从新写入的数据开始保留完整轨迹，而不是伪造旧轨迹

### B. `backend/storage/message_store.py`

扩展 `append()` / `append_batch()` 入参，支持写入：

- `phase`
- `phase3_step`

并新增按 session 读取完整历史的方法保持不变。

本期不新增复杂查询 API。按 phase 派生 segment 的逻辑放在更靠近恢复编排的一层，避免 `MessageStore` 过早承担业务语义。

### C. `backend/api/orchestration/session/persistence.py`

#### `persist_messages()`

当前函数是“整段覆盖写回”。本设计将其重构为“增量持久化协调器”。

核心变化：

1. 不再删除旧消息
2. 不再把当前内存 `messages` 视为“完整历史”
3. 改为只写入本轮新增消息，并带上 `phase` / `phase3_step`

为避免重复写入，需要引入“已落盘消息边界”概念。边界的具体实现不在本 spec 里规定为某一种唯一方案，但必须满足：

- 同一条消息不会因 phase rebuild 被重复持久化多次
- phase rebuild 只影响 runtime view，不抹掉已落盘历史

推荐实现方向：在 session 运行态维护 `persisted_message_count` 或等价游标，仅 append 新增尾部消息。

#### `restore_session()`

恢复时流程调整为：

1. 加载完整 `history_view`（messages 全量历史）
2. 加载当前 `plan`
3. 调用独立的 runtime 重建逻辑，根据当前 phase 派生 `runtime_view`
4. 返回给 session runtime 的 `messages` 使用 `runtime_view`
5. `history_view` 作为附加数据挂回恢复结果，供未来调试或接口扩展使用

本期允许 `history_view` 先只在后端内部存在，不强制暴露到 API 响应。

### D. `backend/agent/execution/message_rebuild.py`

本文件的职责保持不变：

- 继续负责为在线 LLM 调用重建短工作集
- 不关心历史 segment 存储

需要明确的约束是：

- `rebuild_messages_for_phase_change()` 继续返回 runtime messages
- `rebuild_messages_for_phase3_step_change()` 继续返回 runtime messages
- 任何与“保留历史全量轨迹”相关的逻辑都不放进这里

这样可以确保 runtime rebuild 仍然是纯 prompt 行为，不被持久化需求污染。

### E. 新增运行时视图派生器

新增一个面向恢复流程的 helper，职责是：

- 输入：`history_view` + 当前 `plan`
- 输出：与当前在线运行一致的 `runtime_view`

其规则应与在线 rebuild 语义对齐：

- 当前 phase 前进后的运行态，不需要重放更早 phase 的全量历史
- 当前 phase3_step 切换后的运行态，不需要重放更早子阶段的全量历史
- 若当前 session 没经历过 phase rebuild，允许降级为最近一段可直接继续的消息尾部

本 helper 的目标不是“完美还原历史现场”，而是“恢复后下一轮行为与未中断时保持一致”。

## 数据流设计

### 在线运行

1. AgentLoop 在内存里维护 runtime messages
2. phase 前进或子阶段切换时，仍按现有逻辑 rebuild runtime messages
3. persistence 层只 append 本轮新增消息到历史流，不删除旧消息
4. SQLite 中保留全历史，内存里保留短工作集

### session 恢复

1. 从 SQLite 加载全量历史消息
2. 从 state snapshot/plan 文件加载当前 plan
3. 用 runtime 视图派生器构造当前工作集
4. 用该工作集重建 AgentLoop

### phase 回退

1. backtrack 仍走现有 plan 清理和 runtime rebuild 逻辑
2. 历史消息不删除
3. 回退后 prompt 不拼接旧阶段 segment
4. 若要诊断回退原因，消费方从 history view 中读取，不从 prompt 中读取

## 恢复视图规则

恢复阶段需要明确一条优先级：**行为一致性优先于历史完整性**。

因此 runtime view 的构造遵循以下规则：

1. 如果当前 phase 已有明确的重建语义，则恢复时按该语义重建，而不是全量回放历史。
2. 如果当前处于 Phase 3，则 runtime view 应对应当前 `phase3_step`，不混入更早 Phase 3 子阶段的原始消息。
3. 如果当前处于回退后的阶段，runtime view 仍以 backtrack notice + 当前用户锚点为准，不拼接目标阶段旧消息。
4. 只有在系统无法判定明确的 rebuild 边界时，才允许退化到“最近一段可继续的消息尾部”。

## 为什么不新增 `phase_segments` 表

本设计明确拒绝独立表，原因如下：

1. `messages` 已经是原始消息事实源，再复制一份 segment 会制造双写问题。
2. segment 边界并不是独立业务对象，而是消息查询视图。
3. 当前项目已经有 `plan_snapshots` 作为状态时间点快照，再增加独立 segment 表只会让恢复逻辑在三个真相源之间来回对齐。
4. 目前没有足够明确的 segment 专属读写需求来支撑额外表设计。

因此本期坚持“消息真相源单一化”：

- 原始消息真相源：`messages`
- 状态真相源：`plan` / `plan_snapshots`
- segment：查询派生视图，不独立落库

## 测试策略

### 1. 数据库迁移测试

覆盖旧版 `messages` 表只有 `provider_state`、没有 `phase` / `phase3_step` 时的迁移补列行为。

### 2. MessageStore 测试

验证 `append()` / `append_batch()` 能正确写入并读出 `phase` / `phase3_step`。

### 3. SessionPersistence 测试

覆盖以下场景：

1. phase rebuild 后再次持久化，不会删除旧阶段消息
2. 同一条已落盘消息不会被重复 append
3. 不同 phase 的消息带有正确标签
4. 恢复时返回的 runtime view 不等于 history 全量回放

### 4. 集成测试

至少覆盖三个主路径：

1. Phase 1 -> 3 -> 5 后恢复 session，runtime view 仍为短工作集
2. Phase 3 `brief -> candidate -> skeleton` 后恢复 session，runtime view 只对应当前子阶段
3. Phase 5 -> 3 回退后恢复 session，history 保留 Phase 5 原始轨迹，但 runtime view 不 replay Phase 5 段

## 风险与约束

### 风险 1：重复持久化

如果仅按“当前 messages 全量写入”改造为 append，而没有边界游标，phase rebuild 后会把 handoff/runtime 消息重复写入，导致历史流膨胀且失真。

因此增量边界是必做项，不可省略。

### 风险 2：恢复行为与在线行为不一致

如果恢复时直接把全量历史灌回 AgentLoop，恢复后的下一轮行为会和未中断 session 明显不同。这会抵消本设计的核心目标。

因此 runtime 视图派生器也是必做项。

### 风险 3：开发者误把历史 segment 再注入 prompt

未来实现中最危险的捷径是：“既然历史保住了，那回退时顺手把目标 phase 历史也喂给模型。”

本 spec 明确禁止该做法。任何需要历史诊断的能力，必须走 history view，不走 prompt 注入。

## 实施边界

本期必须完成：

1. `messages` 表增加 `phase` / `phase3_step`
2. 持久化从覆盖写回改为增量追加
3. session 恢复显式区分 history view 与 runtime view
4. 回退不 replay 历史 segment 的约束以代码与测试固化

本期不做：

1. 独立 segment 表
2. 前端 phase 历史时间线
3. 人工诊断 API
4. 历史旧数据回填

## 结论

本设计不否定当前的 phase 丢弃策略，而是把它限制在正确的职责边界内：

- **对 LLM**：继续丢弃，保持强隔离
- **对系统持久化**：不再丢弃，改为保留完整历史并附带 phase 标签

也就是说，系统不是从“丢弃策略”切到“隔离策略”，而是从“单轨混用”切到“runtime 丢弃、storage 隔离”的双轨架构。
