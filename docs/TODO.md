# TODO

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

## 4. session_id 与 trip_id 并存边界梳理

### 背景

当前系统中 `session_id` 表示一段聊天会话，`trip_id` 表示这段会话中正在规划的具体旅行。新建 session 时默认生成一对一关系：

```text
session_id = sess_xxxxxx
trip_id    = trip_xxxxxx
```

但当用户在同一个聊天里触发"重新开始 / 换目的地 / 新行程"等 reset 型回退时，系统会保留同一个 `session_id`，并轮转新的 `trip_id`，以隔离旧行程下的 trip-scope memory / working memory / episode 语义。

### 待办项

- 明确产品语义：一个聊天会话是否允许承载多个连续旅行规划
- 如果允许，确认 `session_id -> multiple trip_id` 是正式设计，而不是历史实现残留
- 梳理 `TravelPlanState.trip_id`、v2 trip memory、v3 working memory、TripEpisode / EpisodeSlice 的生命周期边界
- 评估 Working Memory 存储路径是否应从 `memory/sessions/{session_id}/working_memory.json` 调整为更显式的 `memory/sessions/{session_id}/trips/{trip_id}/working_memory.json`
- 检查 trip 轮转后旧 working memory 是否只是不再召回，还是也需要显式归档 / 标记 obsolete
- 补充测试覆盖：同一 session 中换目的地后，旧 trip 的临时信号不会污染新 trip

### 目标

让 `session_id` 和 `trip_id` 的职责边界成为清晰的架构约定：聊天连续性归 session，旅行语义隔离归 trip，避免后续 memory / archive / frontend 状态治理出现隐性耦合。

## 5. recall-first 后前端 `profile_fixed` 展示语义收敛

### 背景

本轮已将记忆召回主链路调整为 recall-first：长期 profile 不再每轮固定注入 system prompt，而是只在 recall 命中后作为 candidate 进入上下文。

同时，为了降低改动风险，后端暂时保留了 `MemoryRecallTelemetry.sources.profile_fixed` 这个字段结构，但其运行时值现在应稳定为 `0`。这意味着前端当前仍沿用旧展示语义时，会出现两类问题：

- UI 仍把 `profile_fixed` 当成一个有业务意义的来源维度展示
- 用户或开发者在看 Trace / ChatPanel 时，容易误以为系统还存在“固定长期画像常驻注入”这条路径

前因后果是：

- 之前系统同时存在两条长期画像进入 prompt 的路径：
  - 固定注入 `fixed_profile_items`
  - query recall 命中的 `query_profile`
- 现在已经移除了第一条路径，只保留 recall candidate 路径
- 但前端展示模型尚未随之收敛，存在“后端行为已改、前端解释仍旧”的语义滞后

### 待办项

- 检查 `frontend/src/components/ChatPanel.tsx` 中记忆召回摘要、memory chip、internal task 文案是否仍隐含 `profile_fixed` 语义
- 检查 `frontend/src/components/TraceViewer.tsx` 中 recall 来源分解是否仍把 `profile_fixed` 作为一等来源展示
- 决定前端策略：
  - 彻底隐藏 `profile_fixed`
  - 或仅在 debug/trace 模式保留，但明确标注“当前链路未使用”
- 调整相关文案，避免继续把“长期画像常驻注入”描述成当前行为
- 补充前端测试或至少补充后端-前端联动测试，确保 recall-first 语义在 UI 层表达一致

### 目标

让前端显示的记忆来源与当前后端真实行为一致：长期 profile 只通过 recall 命中进入上下文，避免用户和开发者被历史字段名误导。

## 6. recall-first 后 trace / stats / API 旧语义清理

### 背景

本轮改动已把后端运行时语义切到 recall-first，并把“未应用任何 recall 结果”的 `final_recall_decision` 从旧的 `fixed_only` 改成了 `no_recall_applied`。

这次只做了最小闭环修正：

- `backend/main.py`
- `backend/memory/manager.py`
- 相关 manager / integration 测试
- `PROJECT_OVERVIEW.md`

但代码库里仍可能残留基于旧机制的观测与接口语义，例如：

- trace/stats 测试仍手工构造 `profile_fixed=1` 或 `final_recall_decision="fixed_only"`
- API 返回结构虽然兼容，但字段解释已经变了
- 文档、统计看板、排障习惯仍可能默认认为“未 recall = fixed_only”

前因后果是：

- 旧模型里，未触发 query recall 时，prompt 里仍可能只有固定 profile，因此 `fixed_only` 有意义
- 新模型里，fixed profile 常驻注入已不存在，所以“未 recall”不再等于“只用了 fixed profile”
- 如果 trace / stats / API 解释层不跟进，就会继续传播错误心智模型，影响后续排障和数据分析

### 待办项

- 全局扫描 `fixed_only`、`profile_fixed` 在 trace / stats / API / 测试中的残留使用
- 区分哪些是“字段结构兼容保留”，哪些是“业务语义仍在使用”
- 将 `final_recall_decision` 的合法值和解释统一到新语义，补充枚举说明
- 评估 `sources.profile_fixed` 是否应继续保留：
  - 若保留，明确为兼容字段，值预期为 0
  - 若不保留，设计一次可控的字段清理计划
- 更新相关 trace/stats 测试，使其表达当前 recall-first 语义，而不是复用旧 fixture
- 必要时补充 API 文档或 telemetry 字段说明，避免下游消费者误解

### 目标

让 trace、stats、API、测试和文档对记忆召回的解释保持一致，彻底消除 `fixed_only` 时代遗留的观测语义偏差。
