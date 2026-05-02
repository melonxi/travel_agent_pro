# TODO

## 0. 分阶段状态机硬约束与 prompt 自律解耦

### 背景

当前分阶段机制里，prompt 负责引导模型按阶段行动，状态机负责确定性推进、冻结和回退。风险点在于：部分不可逆或高副作用动作仍主要依赖 prompt 自律。一旦模型某轮写错状态，状态机会把错误固化，后续只能通过有副作用的回退修复。

这类问题优先级高于单纯的提示词措辞优化。

### P0 / P1 待办项

#### 1. Phase 1 destination 粒度硬校验

问题：`update_trip_basics(destination=...)` 当前只写字符串，不校验目的地粒度。`PhaseRouter.infer_phase()` 只要看到 `destination` 非空就离开 Phase 1。如果模型把“日本 / 美国 / 东南亚”等多城市国家或大区域写入 destination，就会跳过城市级收敛。

待办：

- 在 `update_trip_basics` 写入边界增加 destination 粒度校验。
- Phase 1 下拒收明确多城市国家 / 大区域 destination，并返回可操作 suggestion。
- 保留紧凑旅行单元例外，如冰岛、马尔代夫、不丹等可整体规划目的地。
- 评估是否在 `PhaseRouter` 增加二次兜底，防止旧状态或绕过工具写入的异常 destination 推进阶段。

#### 2. request_backtrack 副作用显式化与确认机制

问题：`request_backtrack(to_phase=...)` 会调用 `clear_downstream(from_phase=to_phase)`，清掉目标阶段及之后产物。Phase 4 回退到 Phase 2 会清除骨架、交通、住宿、`daily_plans` 和 `deliverables`。当前 prompt / red flag 只告诉模型“该回退”，没有告诉模型回退会删除哪些成果。

待办：

- 在 `G-BACKTRACK-BOUNDARY`、`request_backtrack` 工具描述和相关 phase prompt 中明确回退会清除的下游产物。
- 要求模型在调用回退前先告知用户影响范围，尤其是会丢失 `daily_plans` / `deliverables` 的场景。
- 评估将 `request_backtrack` 改成两步式 preflight：第一次返回 `needs_confirmation` 与 `will_clear`，用户确认后才真正执行。
- 评估保留可恢复快照，避免用户感知为“系统把已确认成果删了”。

#### 3. Phase 4 工具失败降级交付协议

问题：Phase 4 prompt 要求先查天气 / 服务，再通过 `generate_summary` 同时提交 `travel_plan_markdown` 和 `checklist_markdown`。如果 `check_weather` 或 `search_travel_services` 失败、超时或返回空，模型可能卡在“必须交付双 markdown”和“不能编造事实”之间。

待办：

- 在 `PHASE4_PROMPT` 中加入显式降级规则。
- 工具失败、超时或返回空时，最多重试 1 次。
- 仍然正常生成双 markdown，但在清单里标注“未获取到 X，出发前请自行确认”。
- 禁止编造天气、政策、价格、链接、订单号。
- 确认 `generate_summary` 接收带未知项标注的 markdown，并保持冻结语义。

#### 4. skeleton days 长度与 dates.total_days 一致性

问题：`_skeleton_days_match()` 会在 router 层阻止 Phase 2 进入 Phase 3，但 `set_skeleton_plans` 写入工具和 skeleton prompt 未把 “days 长度必须等于 `dates.total_days`” 作为前置硬约束。

待办：

- 在 skeleton step 字段契约中加入 `len(days) == dates.total_days` 规则。
- 在 `set_skeleton_plans` 工具层校验：若 `plan.dates` 已存在，所有 skeleton 的 `days` 长度必须等于 `plan.dates.total_days`。
- 工具错误中返回明确 suggestion，例如“当前行程为 5 天，请生成 5 天骨架”。

#### 5. candidate 到 skeleton 的两轮节奏硬化

问题：candidate step 当前开放 `set_skeleton_plans`，因为现有状态机依赖写入 `skeleton_plans` 触发进入 skeleton。但 prompt 又禁止在写入 `shortlist` 的同一轮工具批次里继续写 skeleton。这是用 prompt 软协议表达“两轮节奏”，模型可能跳过攻略经验采集直接写骨架。

待办：

- 不要直接从 candidate 工具集中移除 `set_skeleton_plans`，否则现有 candidate 无法推进到 skeleton。
- 评估新增硬状态，如 `route_research_done` / `skeleton_research_notes`，让攻略经验采集成为可验证状态。
- 或将 Phase 2 子阶段拆成 `candidate -> route_research -> skeleton`，由状态机而不是 prompt 自律保证节奏。
- 在未引入新状态前，保留 prompt 禁令，但把风险记录为质量降级而非流程阻断。

### 目标

把“会冻结、会推进、会清下游”的确定性状态机动作，从 prompt 自律迁移到工具层和 router 层硬约束。prompt 继续负责解释和节奏，工具 / 状态机负责防止单次模型失误造成不可逆损害。

## Phase 1 prompt / soul 职责边界整理

### 背景

Phase 1 的身份、目标和节奏正在从 `PHASE1_PROMPT` 与 `soul.md` 之间重新分层。当前 `PHASE1_PROMPT` 不再自包含“目的地收敛顾问”等身份信息，而 `soul.md` 的 Phase 1 标题也从旧的“交互基调”改成了“身份 / 目标 / 不做 / 节奏”结构。

这会影响两类测试/调用边界：

- `PhaseRouter.get_prompt(1)` / `get_prompt_for_plan(phase=1)` 单独读取时缺少 Phase 1 身份和目标。
- `ContextManager` 的 soul 注入测试仍断言旧标题 `## Phase 1 交互基调`。

### 待办项

- 明确 Phase prompt 是否必须保持自包含任务合同。
- 明确 `soul.md` 是否只承载语气/节奏，还是也承载阶段身份。
- 根据最终边界同步更新 `PHASE1_PROMPT`、`soul.md` 和相关单测。
- 避免在 prompt 中使用“见 Phase 1 身份”这类跨文件隐式引用，除非所有调用路径都保证注入 soul。

### 目标

把 Phase 1 prompt 合同和 soul 人格片段的职责边界整理清楚，避免在 1/2/3/4 阶段迁移之外混入未定稿的 prompt 架构改动。

## 1. tool-self-repair

### 背景

当前 agent 系统已经具备将工具报错作为 `tool_result` 返回给模型的能力，但对"模型传了不受支持的工具参数"这类错误，仍然缺少足够强的自纠错支持。

已出现的实际案例：

- 工具：`xiaohongshu_search`
- 场景：模型调用 `search_notes`
- 输入包含：`max_results`
- 结果：由于工具函数签名未接收该参数，触发 Python `TypeError`
- 当前落到 agent 的错误类型：`INTERNAL_ERROR`

这类错误虽然会被回传给模型，但错误语义不够明确，模型未必能稳定完成下一轮自我修正。

### 待办项

- 在 `ToolEngine` 中识别类似 `unexpected keyword argument` 的异常
- 不要统一归类成 `INTERNAL_ERROR`
- 改为更明确的可恢复错误，例如：`INVALID_INPUT` / `UNSUPPORTED_PARAMETER`
- 在错误结果里附带不被支持的参数名
- 在 `suggestion` 中返回该工具允许的参数列表
- 最好明确到 operation 级别，例如 `xiaohongshu_search.search_notes` 支持哪些字段
- 评估是否需要在真正调用 Python 工具函数前，先根据工具 schema 做一次参数白名单校验
- 扫描其他工具的 schema 与 Python 函数签名是否一致，重点关注搜索类工具

### 目标

让 agent 在工具调用失败时，不只是"把错误返回给模型"，而是能够以更高概率驱动模型完成自我纠错并继续执行。

## 2. [DONE] openai_provider 错误分类：从 APIError 中恢复真实 HTTP 状态码

### 目标

让裸 `APIError`（讯飞等兼容网关常见）被准确归类为 TRANSIENT/RATE_LIMITED/BAD_REQUEST，而不是误报为 PROTOCOL_ERROR。

### 完成记录

- 完成日期：2026-04-15
- 分支：`fix/llm-error-classify`
- 改动：`llm/errors.py` 新增 `classify_opaque_api_error()`，两个 provider fallthrough 改调该函数
- 测试：`test_classify_opaque_api_error.py`（28+ 用例）、`test_anthropic_provider_classify.py`（6 用例）、`test_openai_provider.py` 已有用例更新

## 3. TraceViewer 迭代行折叠优化

### 背景

当前 `build_trace()`（`backend/api/trace.py:98-153`）为每个 LLM 调用创建一条独立的迭代行。在长对话中（如 289 次 LLM 调用），TraceViewer 右面板会产生大量冗余信息：

- 连续多行属于同一 agent phase，无工具调用，优先级/token/cost 完全相同
- 用户需要反复滚动才能找到有实际意义的迭代（带工具调用或阶段切换的行）
- 模型 `astron-code-latest` 不在 `_PRICING` 表（`backend/telemetry/stats.py:10-25`）中，导致所有行显示 0 tokens / <$0.001

### 待办项

#### 后端（`backend/api/trace.py`）
- 在 `build_trace()` 中识别**连续同 phase、无工具调用**的 LLM 调用序列
- 将这些序列合并为一个"折叠组"（`collapsed_group`），包含：组内调用数量、首尾时间戳、汇总 token/cost
- 保留每条原始记录作为 `children`，供前端展开时使用

#### 前端（`frontend/src/components/TraceViewer.tsx`）
- `IterationRow` 支持渲染折叠组：默认显示汇总行（如 "Phase: plan × 47 calls"），点击展开详情
- 折叠/展开动画与 Solstice 设计系统一致（glass morph + smooth transition）

#### 补充
- 将缺失的模型添加到 `_PRICING` 表，或在前端对 0 tokens 的行显示 "N/A" 而非 "0"
- 考虑对折叠组内无差异的列（priority, tools, cost）只在汇总行显示一次

### 目标

将 TraceViewer 的信噪比从"每个 LLM 调用一行"提升到"每个有意义阶段/工具调用一行"，大幅减少滚动和视觉噪声。

## 4. [DONE] session_id 与 trip_id 并存边界梳理

### 背景

当前系统中 `session_id` 表示一段聊天会话，`trip_id` 表示这段会话中正在规划的具体旅行。新建 session 时默认生成一对一关系：

```text
session_id = sess_xxxxxx
trip_id    = trip_xxxxxx
```

但当用户在同一个聊天里触发"重新开始 / 换目的地 / 新行程"等 reset 型回退时，系统会保留同一个 `session_id`，并轮转新的 `trip_id`，以隔离旧行程下的 trip-scope memory / working memory / episode 语义。

### 完成结果

- `session_id -> multiple trip_id` 是正式设计：聊天连续性归 session，旅行语义隔离归 trip。
- `TravelPlanState.trip_id` 是当前旅行事实的隔离键；working memory 与 episode 归档都按当前 trip 语义读写。
- Working Memory 已从 session 级路径收口到 `memory/sessions/{session_id}/trips/{trip_id}/working_memory.json`。
- trip 轮转后旧 working memory 不会被当前 trip 召回。
- 已补充测试覆盖同一 session 换目的地后旧 trip 临时信号不污染新 trip。

### 目标

让 `session_id` 和 `trip_id` 的职责边界成为清晰的架构约定：聊天连续性归 session，旅行语义隔离归 trip，避免后续 memory / archive / frontend 状态治理出现隐性耦合。

## 5. recall-first 后前端旧来源展示语义收敛

### 状态

- 已完成

### 完成结果

- `frontend/src/components/TraceViewer.tsx` 不再展示已移除的旧来源维度
- `frontend/src/types/trace.ts` 已删除对应的旧来源类型定义
- `ChatPanel` 继续保留 `profile_ids` 聚合逻辑，但其语义已收敛为“命中的 profile recall ids”，不再暗示 fixed profile 常驻注入

### 结论

前端展示已与 recall-first 主链路对齐：长期 profile 只通过 recall 命中进入上下文，不再保留旧的固定画像来源语义。

## 6. recall-first 后 trace / stats / API 旧语义清理

### 状态

- 已完成

### 完成结果

- `MemoryRecallTelemetry.sources` 已删除旧的固定画像来源字段
- `MemoryHitRecord` / trace API / stats / memory v3 API 测试样例已收口到 `query_profile`、`working_memory`、`episode_slice`
- `final_recall_decision` 的旧语义已从相关测试中移除，统一收口到当前真实语义 `no_recall_applied`
- `PROJECT_OVERVIEW.md` 已同步说明新的 recall payload 和来源定义

### 结论

trace、stats、API、测试和文档现在都以 recall-first 的真实行为为准，不再传播历史遗留的固定画像 / 固定注入观测语义。
