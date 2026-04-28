# LLM 基础设施面试问答集

> 目标：围绕 Travel Agent Pro 的 LLM provider 抽象、多供应商、流式工具协议、错误恢复、token/cost、model routing 与新一代 Agent 平台迁移，组织可用于面试表达的中文问答。回答应基于当前代码事实，不把规划能力包装成已完整上线能力。

## 0. 总体定位

### Q：Travel Agent Pro 的 LLM 基础设施解决的核心问题是什么？

推荐回答：

它解决的不是“怎么调一次模型 API”，而是把模型调用放进可观察、可恢复、可替换的 Agent runtime 里。项目把 LLM 基础设施拆成几层：

- `LLMProvider` Protocol 定义统一入口：`chat()`、`count_tokens()`、`get_context_window()`。
- OpenAI / Anthropic provider 把不同供应商的 message、tool、stream、usage 格式归一成内部 `LLMChunk`。
- `AgentLoop` 和 `run_llm_turn()` 只消费内部 chunk，不关心底层是 Chat Completions 还是 Anthropic Messages。
- `run_agent_stream()` 再把 chunk 转成 SSE，处理 usage 统计、错误展示、continue 和保底持久化。

所以 LLM 基础设施的职责是：隔离供应商差异、保护工具调用协议、归一化错误和 usage、为上层 Phase 状态机和 trace/eval 提供稳定接口。

代码主线：

- `backend/llm/base.py`
- `backend/llm/types.py`
- `backend/llm/openai_provider.py`
- `backend/llm/anthropic_provider.py`
- `backend/agent/execution/llm_turn.py`
- `backend/api/orchestration/chat/stream.py`

### Q：你会如何向面试官解释“LLM infra”和“Agent Loop”的边界？

推荐回答：

LLM infra 是模型 transport 和协议适配层，回答“怎样稳定地调用不同模型，并把返回统一成系统能理解的事件”。Agent Loop 是业务运行时，回答“拿到模型事件后怎样执行工具、推进阶段、写状态、处理质量门控和上下文重建”。

例如 OpenAI streaming 会分片返回文本和 tool call arguments，Anthropic tool use 是 content block；这些差异在 provider 层处理。到了 `AgentLoop`，它只看到 `TEXT_DELTA`、`TOOL_CALL_START`、`USAGE`、`DONE`，然后按 think-act-observe 执行工具和判断阶段变化。这个边界让后续接入 Responses API 或新 provider 时，尽量只动 provider 层，不动旅行状态机。

## 1. Provider Abstraction

### Q：`LLMProvider` 抽象为什么只保留三个方法？

推荐回答：

这是刻意做薄的抽象。当前项目不希望 provider 层知道 Phase、工具权限、memory、state writer 或 SSE 细节，它只需要提供三件事：

1. `chat()`：给定内部 `Message[]` 和工具 schema，输出统一的 `LLMChunk` 异步流。
2. `count_tokens()`：给 context compaction 一个估算能力。
3. `get_context_window()`：让系统在启动时探测真实上下文窗口，探测失败时回退到配置值。

薄抽象的好处是供应商扩展成本低。新增 DeepSeek 或 Gemini 时，核心工作是把它们的 message/tool/usage/error 转成内部 chunk，而不是让整个 Agent runtime 分叉。

代码：`backend/llm/base.py::LLMProvider`。

### Q：内部为什么要定义 `LLMChunk`，而不是直接把 provider 的 stream 往上传？

推荐回答：

因为上层 Agent 关心的是语义事件，不是某个 SDK 的事件名。项目内部的 `LLMChunk` 包含：

- `TEXT_DELTA`：文本增量；
- `TOOL_CALL_START`：完整工具调用；
- `PROVIDER_STATE_DELTA`：provider 私有状态，如 DeepSeek reasoning content；
- `USAGE`：token usage；
- `DONE`：本轮模型输出结束；
- 以及上下文压缩、phase transition、internal task 等 runtime 事件。

这样 `run_llm_turn()` 可以用同一套逻辑处理 OpenAI、Anthropic 和未来 provider；SSE 层也可以稳定渲染工具卡、文本、usage 和错误。

代码：`backend/llm/types.py::ChunkType`、`backend/llm/types.py::LLMChunk`。

### Q：如果新增一个 provider，需要实现哪些能力？

推荐回答：

最小改动路径是：

1. 新增 `backend/llm/<provider>_provider.py`，实现 `LLMProvider` Protocol。
2. 在 `backend/llm/factory.py::create_llm_provider()` 注册 provider 分支。
3. 把该 provider 的消息格式、工具 schema、tool choice、usage、错误对象转成项目内部格式。
4. 增加 provider 单测，至少覆盖 message 转换、tool result 转换、stream chunk、usage、错误分类和 retry 行为。
5. 在 `config.yaml` / 环境变量中增加配置入口。

我会优先保证内部协议不变：上层仍只看到 `LLMChunk` 和 `LLMError`，不让供应商特性泄漏到 PhaseRouter 或 writer 层。

### Q：为什么当前 factory 很简单，没有复杂的 `LLMFactory` 类？

推荐回答：

当前需求还不需要复杂工厂。`create_llm_provider(config: LLMConfig)` 只基于 `provider` 创建 OpenAI 或 Anthropic provider，符合现阶段“少做抽象、先把主路径稳定”的原则。

但这里也暴露出下一步演进点：`config.yaml` 已经解析了 `llm_overrides`，而主 chat 构建路径当前仍主要用 `config.llm` 创建 provider。也就是说，配置层已经为按阶段/任务路由预留了结构，执行层还需要一个明确的 model router，把 `phase`、任务类型、成本预算和 provider health 统一纳入选择逻辑。面试中我会把它描述为“已预留配置与 provider 抽象，主路径 routing 仍应继续收敛”，而不是说已经完整上线动态 routing。

代码：

- `backend/config.py::AppConfig.llm_overrides`
- `backend/llm/factory.py::create_llm_provider`
- `backend/api/orchestration/agent/builder.py::build_agent`

## 2. OpenAI / Anthropic 多供应商

### Q：OpenAI provider 和 Anthropic provider 最大的协议差异是什么？

推荐回答：

有三个关键差异：

1. **system message**：OpenAI 直接把 system 放进 messages；Anthropic 要拆成顶层 `system` 字段，所以 provider 里有 `_split_system_and_convert()`。
2. **tool 格式**：OpenAI 是 `{"type":"function","function":...}`；Anthropic 是 `{"name","description","input_schema"}`。
3. **tool result 回传**：OpenAI 使用 `role="tool"` 和 `tool_call_id`；Anthropic 把 tool result 包成 user message 的 `tool_result` content block。

这些差异在 provider 层消化，上层 `AgentLoop` 不需要知道。

代码：

- `backend/llm/openai_provider.py::_convert_messages`
- `backend/llm/openai_provider.py::_convert_tools`
- `backend/llm/anthropic_provider.py::_split_system_and_convert`
- `backend/llm/anthropic_provider.py::_convert_tools`

### Q：Anthropic provider 为什么在有 tools 时走 non-stream fallback？

推荐回答：

代码里有一个非常实际的工程取舍：Anthropic streaming tool-use 在当前 runtime 下遇到 SDK event accumulation 问题，所以当 tools 可用时，provider 会调用非流式 `messages.create()`，再把完整响应重新发射成内部 `TEXT_DELTA` / `TOOL_CALL_START` / `USAGE` / `DONE` chunk。

这不是最理想的用户体验，因为工具场景下文字不会真正 token-by-token 流出；但它换来工具协议稳定性和实现可控。面试时可以强调：生产 Agent 里 tool-use 正确性优先于表面 streaming，尤其是写状态工具存在副作用时，不能为了流式体验牺牲协议稳定。

测试：`backend/tests/test_anthropic_provider.py::test_streaming_with_tools_falls_back_to_nonstream_create`。

### Q：OpenAI provider 如何处理 streaming tool calls？

推荐回答：

OpenAI streaming 会把 tool call 的 id、name、arguments 分散在多个 delta 里返回。项目用 `current_tool_calls` 按 index 聚合分片，直到 `finish_reason` 出现后再一次性发出完整 `TOOL_CALL_START`。这样 `AgentLoop` 拿到的永远是 arguments 已经 JSON 解析后的完整工具调用，不需要处理半截 JSON。

同时 provider 会在 stream option 中请求 usage：`stream_options={"include_usage": True}`，最终把 prompt/completion tokens 转成内部 `USAGE` chunk。

测试：

- `backend/tests/test_openai_provider.py::test_streaming_chat_flushes_tool_calls_when_finish_reason_chunk_has_no_delta`
- `backend/tests/test_openai_provider.py::test_streaming_chat_emits_done_when_finish_reason_arrives_without_delta`

### Q：项目如何支持 provider 私有的 reasoning state？

推荐回答：

内部协议没有把 reasoning 当成普通用户可见文本，而是通过 `PROVIDER_STATE_DELTA` 传递 provider 私有状态。OpenAI-compatible provider 如果返回 `reasoning_content` 或 `reasoning_details`，`OpenAIProvider` 会发出 provider state delta；`run_llm_turn()` 会把这些 delta 合并到 assistant message 的 `provider_state` 里。后续如果模型要求 continuation 或 provider 要求把 reasoning state 带回，也可以通过 message conversion 附回去。

当前实现里对 DeepSeek 类模型做了兼容：只有 model name 包含 `deepseek` 时，才把 `reasoning_content` / `reasoning_details` 带回 OpenAI-compatible request，避免普通 OpenAI 模型收到不认识的字段。

代码：

- `backend/llm/openai_provider.py::_attach_reasoning_state`
- `backend/agent/execution/llm_turn.py::_merge_provider_state_delta`

## 3. Streaming / Tool-use 协议

### Q：为什么工具调用协议是 LLM infra 的核心风险点？

推荐回答：

因为一旦 assistant message 带 tool calls，后面必须紧跟所有对应 tool results。如果中间插入 system message、普通 assistant message，或漏掉某个 tool result，后续模型调用就可能被 provider 拒绝，或者模型误解上下文。

项目的处理是：

- provider 层只负责把供应商 tool-use 归一成完整 `ToolCall`；
- `AgentLoop` 在执行工具前先 append assistant tool_calls message；
- `_execute_tool_batch()` 执行后 append 对应 `Role.TOOL` message；
- 工具期间产生的实时校验、soft judge 等系统提示先进入 pending notes，下一次 LLM 调用前再 flush。

这保证了 `assistant.tool_calls -> tool results` 的原子序列。

代码：

- `backend/agent/loop.py::AgentLoop.run`
- `backend/api/orchestration/session/pending_notes.py`
- `backend/api/orchestration/agent/hooks.py::on_before_llm`

### Q：tool result 为什么要包含 `error_code` 和 `suggestion`？

推荐回答：

LLM 修复工具错误时，纯自然语言错误不够稳定。项目在 `ToolResult` 里把 `error`、`error_code`、`suggestion` 一起回传给 provider。OpenAI 和 Anthropic provider 都会把这些字段序列化进 tool result payload。

这样模型不仅知道“失败了”，还知道错误类别和下一步怎么修。例如 Phase 3 skeleton 写入失败时，工具可以返回 `INVALID_VALUE` 和“某 POI 只能保留在一天”的 suggestion，模型下一轮更容易调用正确的修复工具，而不是继续自然语言解释。

测试：

- `backend/tests/test_openai_provider.py::test_convert_tool_error_message_includes_repair_fields`
- `backend/tests/test_anthropic_provider.py::test_convert_tool_error_includes_repair_fields`

### Q：当前内部 chunk 里为什么没有真正使用 `TOOL_CALL_DELTA`？

推荐回答：

这是一个有意识的简化。虽然底层 provider 可能有 tool call delta，但项目上层更需要“完整、可执行”的工具调用，而不是半截 arguments。尤其写状态工具有副作用，不能让 runtime 看到未完成 JSON 就做任何动作。

所以 OpenAI provider 在内部聚合 delta，Anthropic provider 也在 content block 结束后发完整 tool call。`TOOL_CALL_DELTA` 保留在枚举里，是为未来前端展示“模型正在构造工具调用”或更细粒度 trace 预留，不是当前执行语义的一部分。

## 4. 错误归一化与 Retry / Continue

### Q：项目如何统一不同 provider 的错误？

推荐回答：

项目定义了统一 `LLMError` 和 `LLMErrorCode`：

- `LLM_RATE_LIMITED`
- `LLM_TRANSIENT_ERROR`
- `LLM_BAD_REQUEST`
- `LLM_STREAM_INTERRUPTED`
- `LLM_PROTOCOL_ERROR`

OpenAI / Anthropic provider 捕获 SDK 的强类型异常，例如 `APIConnectionError`、`APIStatusError`，再映射到统一错误。对于裸 `APIError` 或代理网关返回的不标准错误，`classify_opaque_api_error()` 会用 status code、body 和关键词做启发式分类。

这让 SSE 层可以用统一字段返回 `error_code`、`retryable`、`provider`、`model`、`failure_phase` 和用户友好文案。

代码：

- `backend/llm/errors.py`
- `backend/api/orchestration/common/llm_errors.py`
- `backend/api/orchestration/chat/stream.py::run_agent_stream`

### Q：`classify_opaque_api_error()` 对未知错误为什么返回 transient 但 `retryable=False`？

推荐回答：

这是一个保守取舍，但**既不算优雅也不算正确**。未知错误可能确实是临时网关问题，所以错误码保留 `LLM_TRANSIENT_ERROR` 语义；但系统没有足够证据自动 retry，因此 `retryable=False`（`backend/llm/errors.py:152-223`）。这样既不会把 opaque error 误归成用户请求错误，也不会盲目重试造成重复成本或副作用。

更严格的做法是按 status code 分桶（5xx → transient+retry、4xx → bad_request、其他 → unknown 单独 code），而不是统一塞到 transient。当前实现的好处是简单、不会误伤；坏处是观测和告警分组会被这一类 "瞬态但不重试" 的错误污染，**面试里要主动承认这是软实现**。

测试：

- `backend/tests/test_openai_provider.py::test_classify_error_unknown_exception`
- `backend/tests/test_anthropic_provider.py::test_classify_error_unknown_exception`

### Q：provider 层什么时候可以自动 retry？

推荐回答：

当前 provider 只在连接阶段、错误可 retry、尚未向上 yield 任何内容时自动 retry，最多重试 2 次，延迟为 1 秒和 3 秒。这个条件很关键：一旦已经 yield 文本或工具调用，上层可能已经展示给用户或准备执行工具，再自动重发同一轮模型调用就有重复副作用风险。

代码里用 `_has_yielded` 做保护：

- OpenAI streaming 文本 delta 出现后设置 `_has_yielded=True`；
- Anthropic streaming 文本 delta 出现后也设置；
- 只有 `failure_phase == "connection"`、`retryable=True`、`not _has_yielded` 时才 retry。

### Q：流式输出中断后为什么用 continue，而不是直接重发上一条用户消息？

推荐回答：

直接重发用户消息会让 Agent 从头规划，可能重复工具调用或重复写状态。项目用 `RunRecord` 和 `IterationProgress` 记录中断位置：

- `PARTIAL_TEXT`：已经输出部分文本，可以保存 incomplete assistant message，然后注入“从断点继续，不要重复已说内容”的 system note。
- `TOOLS_READ_ONLY`：只完成了只读工具，可以根据已有工具结果继续总结。
- `TOOLS_WITH_WRITES` / `PARTIAL_TOOL_CALL`：不安全，不默认 continue，因为可能涉及副作用或半截工具调用。

`run_agent_stream()` 依据 `agent.progress` 判断 `can_continue`，`continue_chat` 不新增用户消息，只追加恢复提示并重新进入 agent stream。

代码：

- `backend/run.py::IterationProgress`
- `backend/run.py::RunRecord`
- `backend/api/routes/chat_routes.py::continue_chat`
- `backend/api/orchestration/chat/stream.py::run_agent_stream`

### Q：cancel 和 LLM error 在运行时如何区分？

推荐回答：

cancel 是用户主动停止，`AgentLoop` 在 LLM 调用前、streaming chunk 处理时、工具执行前检查 `cancel_event`。如果被取消，会抛出 `LLMError(failure_phase="cancelled")`，SSE 层把 run 标记为 `cancelled` 并发送 done。

普通 LLM error 则把 run 标记为 `failed`，输出统一 error event，并根据 progress 决定是否可 continue。finally 块会调用 `persist_run_safely()` 做保底持久化，防止已经写入的 plan 或 message 因连接断开丢失。

## 5. Token / Context / Cost

### Q：项目如何估算 token 和上下文预算？

推荐回答：

启动时 `main.py` 会调用 provider 的 `get_context_window()` 探测模型上下文窗口；失败时用 `config.llm.context_window`。每轮 LLM 前，hook 用：

`prompt_budget = context_window - max_output_tokens - safety_margin`

然后用 `estimate_messages_tokens()` 估算当前 messages + tools 是否超预算。优先压缩长工具结果，尤其是 `web_search` 和小红书工具；如果工具压缩后仍超预算，再做历史摘要，保留 system、偏好/约束相关消息和最近消息。

代码：

- `backend/main.py::_probe_context_window`
- `backend/agent/compaction.py::compute_prompt_budget`
- `backend/agent/compaction.py::compact_messages_for_prompt`
- `backend/api/orchestration/agent/hooks.py::on_before_llm`

### Q：当前 token 估算准确吗？生产化怎么改？

推荐回答：

当前是“够用但不精确”的工程实现：

- OpenAI provider 使用 `tiktoken.encoding_for_model()`，但只粗略加 message overhead，工具 schema 和多模态场景不完整。
- Anthropic provider 用 `len(content)//3` 估算（`backend/llm/anthropic_provider.py:447-452`），比官方 tokenizer 粗很多。
- compaction 同样用 `max(1, len(text)//3)`（`backend/agent/compaction.py:356`），适合本地 demo 和防止明显爆窗，不适合做严格账单或 SLA。

生产化我会做三步：

1. 接入 provider 官方 token counting API 或 tokenizer，分别计算 messages、tools、tool results。
2. 把实际 usage 回写到 trace，与估算值对比，校准 compaction 阈值。
3. 引入 per-session / per-user token budget，超过预算时触发 model downgrade、减少候选数量或要求用户确认。

### Q：成本统计现在怎么做？有什么边界？

推荐回答：

provider 返回 `USAGE` chunk 后，`run_agent_stream()` 调用 `_record_llm_usage_stats()` 写入 `SessionStats`。`SessionStats` 按模型聚合 input/output tokens、调用次数、duration，并用本地 `_PRICING` 表估算美元成本。

边界是：这个成本适合 demo、debug 和面试展示，不应视为实时准确账单。价格表可能过期，provider 可能有 cached input 折扣、batch 价格、代理价格或企业折扣。生产化需要把价格版本化，定期同步 provider billing，按 user/session/project 设预算和报警。

代码：

- `backend/api/orchestration/common/telemetry_helpers.py::_record_llm_usage_stats`
- `backend/telemetry/stats.py::SessionStats`
- `backend/telemetry/stats.py::_PRICING`

### Q：Phase 5 并行 worker 对成本和延迟的影响是什么？

推荐回答：

Phase 5 并行主要降低 wall-clock latency，不保证降低 token 成本。多个 Day Worker 并发规划每天行程，用户等待时间下降；但每个 worker 都有自己的 LLM loop、工具调用和 retry，通常总 token 会比串行更高。

shared prefix / KV-cache 的价值主要是降低 provider 侧重复计算和延迟，不等于 prompt token 免费。多数 provider 仍会按完整输入计费，最多对 cached input 给折扣。因此面试中要讲清楚：这是“用更多 token 换更低等待时间”的架构取舍，适合天数多、用户等待成本高的阶段，不应该无条件启用。

## 5.5 Prefix Cache 与 Cached Input Tokens

### Q：什么是 prefix cache，为什么对 Agent 系统是关键优化？

推荐回答：

OpenAI 和 Anthropic 在 2024-2025 都把 **prompt prefix caching** 推为一等公民优化（OpenAI 自动启用，Anthropic 通过 `cache_control` 显式标注）。它的本质是：相同前缀的 prompt 在 provider 侧缓存 KV，命中时按 cached token 折扣计费（OpenAI 约 50% off，Anthropic 约 90% off）并显著降低 TTFT。

对 Agent 系统这是关键优化，因为 Agent 的输入有结构化的 cache 友好特征：

- **system prompt 长且稳定**（角色、安全条款、phase rule、工具使用准则）。
- **tool schema 长且稳定**（17 个写工具的 JSON schema 占比可观）。
- **历史 messages 是 append-only**，前缀完全不变。
- **Phase 5 并行 worker** 共享 system + skeleton 前缀，N 个 worker 并发时 prefix cache 价值最大化。

只要把 "稳定→不稳定" 的内容按顺序排（system → tools → static context → history → 当前轮），cache 命中率可以非常高。

### Q：本项目对 prefix cache 做了什么、还差什么？

推荐回答：

**做对了的部分**：

- system / tool schema 在每轮基本稳定，不会随 phase 频繁重排。
- Phase 5 worker 共享 skeleton plan 作为前缀，并发时 prefix 复用率高（这是设计目标之一）。
- compaction 优先压缩长工具结果，保留 system + 偏好 + 最近消息，前缀结构稳定。

**客观短板**（面试要主动承认）：

1. **`build_time_context` 用秒精度时间戳**：`backend/context/manager.py:124` 用 `%H:%M:%S` 写入 system 段的"当前时间"。每秒变一次的字段会让 system prefix 每轮都变，**直接破坏 OpenAI 自动 prefix cache 命中**。正确做法是按对话轮、按分钟或按"业务 epoch" 离散化，否则没必要把秒级精度暴露给模型。
2. **`cached_input_tokens` 没接入 `SessionStats`**：provider 返回的 cached input token 字段没有在 telemetry 层聚合，所以**测不出真实命中率，也没法做 cache hit ratio dashboard**。这意味着我们对 "prefix cache 有没有真起作用" 是盲测的。
3. **Anthropic 没有显式 `cache_control` 标注**：Anthropic provider 走的是默认行为，没有按 system / tools / 长工具结果显式标 ephemeral/persistent，长尾收益没拿满。
4. **成本聚合不区分 cached / uncached**：`SessionStats._PRICING` 当前只按 model 单价乘 input tokens，没有按 cached input 折扣分摊，**估算成本会系统性偏高**。

生产化路线很清晰：把 cached_input_tokens 写进 SessionStats、分单价计费、加 hit ratio 监控；把 build_time_context 时间精度降到分钟或事件级；Anthropic 显式打 cache_control。

## 6. Model Routing 与任务分层

### Q：当前项目里的模型配置是什么样？

推荐回答：

`config.yaml` 主配置默认是 OpenAI `gpt-4o`，`llm_overrides` 里预留了按阶段覆写，例如 Phase 1/2 使用 Anthropic Claude Sonnet 4、Phase 5 使用 OpenAI GPT-4o。配置会被 `load_config()` 解析成 `AppConfig.llm` 和 `AppConfig.llm_overrides`。

但需要客观说明：主 chat agent 当前由 `build_agent()` 直接用 `config.llm` 创建 provider；`llm_overrides` 还没有形成完整的运行时 routing 控制面。项目现在更像“provider abstraction + override config 已存在，动态 routing 是下一步收口项”。

代码：

- `config.yaml`
- `backend/config.py::load_config`
- `backend/api/orchestration/agent/builder.py::build_agent`

### Q：如果要把 model routing 做生产化，你会怎么设计？

推荐回答：

我会加一个显式 `ModelRouter`，输入包括：

- phase / phase3 step；
- 任务类型：主规划、memory gate、query plan、quality judge、Phase 5 worker；
- 复杂度信号：天数、候选数量、工具数量、用户是否要求高精度；
- 成本预算和延迟 SLA；
- provider health、rate limit、近期错误率。

输出是完整 `LLMConfig`，而不是只替换 model。这样可以从 OpenAI 切 Anthropic，也可以在同 provider 内大小模型切换。router 决策要写入 trace，便于解释“为什么这轮用了这个模型”。

一个合理策略是：轻量 gate/extraction 用便宜小模型；Phase 3 skeleton、Phase 5 daily planning 和质量评估用强模型；外部 provider rate limited 时走 fallback provider；高风险写状态前不因省钱降级到不稳定模型。

### Q：为什么 memory extraction / recall gate 不一定要用主模型？

推荐回答：

这些任务更像结构化分类和信息抽取，成本敏感且调用频繁。理想情况下：

- recall gate 用小模型或规则优先，减少每轮固定成本；
- retrieval plan 用中等模型，要求稳定 JSON/tool 输出；
- 主规划和 Phase 5 worker 用强模型，因为它们承担开放推理、信息整合和路线权衡。

当前代码里 memory orchestration 多处仍复用 `config.llm`，部分只通过 `replace(config.llm, model=recall_gate_model)` 替换模型。这能跑通，但不是完整 provider-level routing。生产化时应让 memory pipeline 也通过 `ModelRouter` 获取完整 provider/model/timeout 配置。

代码：

- `backend/api/orchestration/memory/orchestration.py`
- `backend/api/orchestration/memory/extraction.py`

## 7. Responses API / Agents SDK 迁移判断

### Q：如果现在迁移到 OpenAI Responses API，你会怎么做？

推荐回答：

我不会重写 AgentLoop，而是先做 provider 层迁移：

1. 新增 `ResponsesProvider`，实现现有 `LLMProvider` Protocol。
2. 把 Responses API 的 output items、tool calls、usage、reasoning/metadata 转成内部 `LLMChunk`。
3. 保持 `ToolCall`、`ToolResult`、SSE、PhaseRouter、writer contract 不变。
4. 用 golden cases 对比迁移前后的 phase、tool call、state diff、deliverables 和 trace。

这样迁移收益是拿到更 Agent-native 的 API 原语，而不是把业务控制面交出去。Travel Agent Pro 最核心的资产是 `TravelPlanState`、Phase gate、writer 工具、memory policy 和 eval，这些不应该因为 API 迁移被弱化。

### Q：Agents SDK / AgentKit 对这个项目有什么价值？

推荐回答：

它们的价值在底层能力标准化：tool schema、handoff、trace、guardrails、eval、built-in tools 和 hosted UI/workflow。对 Travel Agent Pro 来说，最可能收益是：

- 更标准的 tool tracing 和 eval integration；
- built-in web/file/MCP 工具减少自研接入成本；
- Agents SDK 的 handoff/tracing 原语可替代部分自研 glue；
- AgentKit / ChatKit 可参考前端任务可见性和操作审批。

但我不会把 PhaseRouter、`TravelPlanState`、写工具合同、memory 归档和旅行领域 eval 迁出去。原因是这些是业务状态权威和失败恢复边界，平台可以提供 infra，但不应该替代 domain control plane。

### Q：如何判断“迁移新平台”是收益还是追新？

推荐回答：

我会设三个门槛：

1. **行为等价或更好**：同一 golden case 的 phase、tool call、state、deliverable 不回退。
2. **可观测性不倒退**：必须能定位 model call、tool call、guardrail、handoff、error 和 usage。
3. **控制面不外包**：当前旅行事实、写状态权限、backtrack、memory policy、PII 删除和审批策略仍由项目定义。

如果新平台只让代码更“潮”，但 trace 变少、状态写入不可控或 eval 难做，那就不迁。Agent 平台是加速器，不是替代状态建模和故障恢复的理由。

### Q：项目和当前 Agent 发展趋势如何对齐？

推荐回答：

2025-2026 年 Agent 工程的趋势是：从“final answer”转向“trajectory”，从单次 chat API 转向 tools / state / trace / eval / guardrail 的完整运行时。Travel Agent Pro 虽然是自研 loop，但内部原语基本和新平台一致：

- `RunRecord` 对应 run 生命周期；
- `ToolCall` / `ToolResult` 对应 tool-use trajectory；
- Phase 5 handoff 对应 controlled handoff；
- ToolGuardrail、Quality Gate、Soft Judge 对应 guardrail/eval；
- OpenTelemetry + TraceViewer 对应 trace；
- golden eval 和 reranker-only eval 对应 agent eval。

这个项目的关键表达是：我不是停留在 prompt engineering，而是在构建一个可调试、可评估、可恢复的 Agent control plane。

## 8. 生产化改造

### Q：如果要把当前 LLM infra 推向生产，你优先改哪几件事？

推荐回答：

我会按风险优先级改：

1. **ModelRouter 落地**：让 phase、任务类型、预算、provider health 共同决定完整 `LLMConfig`。
2. **严格 token accounting**：接入 provider tokenizer/count API，估算值和实际 usage 对齐。
3. **供应商熔断和 fallback**：按 provider/model 记录错误率、429、超时和 p95 latency，触发降级或切 provider。
4. **幂等与副作用保护**：写工具引入 operation id / idempotency key，防止网络恢复或 continue 造成重复写。
5. **成本治理**：per-user/session budget、告警、超预算降级策略。
6. **trace 持久化**：当前 session stats 更偏运行态，生产需要可查询的历史 trace、错误聚合和 replay。

### Q：当前 retry / continue 还缺什么生产能力？

推荐回答：

当前设计已经避免了最危险的“有输出后自动重试”。生产化还需要补：

- write tool 幂等 key，保证重复请求不会重复写状态；
- tool-call-level checkpoint，把已完成只读工具、已完成写工具和未完成工具明确分段；
- stream interruption 的错误码更精细，例如真正区分 provider stream 断开、客户端断开、server cancel；
- continue 时将 partial assistant text 和已完成工具结果纳入更结构化的恢复上下文，而不只是 system note。

也就是说，当前实现适合 demo 和开发调试，生产上还要把恢复从“提示模型继续”升级成“可审计 checkpoint + 幂等执行”。

### Q：如何做多 provider fallback 才不会破坏协议？

推荐回答：

fallback 不能简单 catch exception 后换模型重发。要先判断失败发生在什么阶段：

- 连接前失败、无输出、无工具调用：可以切 provider 重试。
- 已输出文本：只能 continuation，不能从头重发。
- 已生成 tool call 但未执行：需要检查 tool call 是否完整，通常不自动 fallback。
- 已执行写工具：不能重跑同一轮，只能基于状态继续。

因此 provider fallback 应该和 `IterationProgress`、tool batch checkpoint、幂等写工具绑定。Fallback 策略也要记录到 trace：原 provider、错误码、切换目标、是否有 partial output。

### Q：如何防止供应商切换导致工具 schema 不兼容？

推荐回答：

项目现在通过内部工具 schema 作为单一事实源，再由 provider 转换成 OpenAI / Anthropic 格式。生产化要继续保持这个原则：

- 工具定义只在 `ToolDef` / `ToolEngine` 中维护；
- provider adapter 只做格式转换；
- 对每个 provider 跑 contract test：required、enum、additionalProperties、tool choice、tool result error fields；
- 高风险写工具在 provider 切换后必须跑回归 eval，不只看最终答案，还看 tool arguments 是否等价。

这也是为什么不应该在 prompt 里写 provider-specific 工具语法；工具协议必须在 adapter 层统一。

## 9. STAR 深挖题

### Q：讲一个你在 LLM infra 上做工程取舍的例子。（STAR）

推荐回答：

- **Situation**：项目需要同时支持 OpenAI 和 Anthropic，但两者在 system message、tool schema、tool result、streaming tool-use 上协议不同。尤其 Anthropic 工具流在当前 runtime 下不稳定，如果强行逐 token streaming，容易破坏工具调用可靠性。
- **Task**：我的目标是让 AgentLoop 上层只依赖统一语义事件，同时保证写状态工具不会因为 provider 差异出现协议错误。
- **Action**：我定义了 `LLMProvider` 和 `LLMChunk`，把 provider 差异收敛在 adapter；OpenAI streaming 聚合 tool call delta 后再发完整工具调用；Anthropic 有 tools 时走 non-stream fallback，再重放成内部 chunk；tool result 统一携带 `error_code` 和 `suggestion`；测试覆盖两边的转换和错误分类。
- **Result**：上层 `AgentLoop` 可以用同一套 think-act-observe 处理多 provider；即使某个供应商的 streaming tool-use 不完美，也不会影响旅行状态写入协议和 SSE 事件结构。

### Q：讲一个你如何处理 LLM 错误恢复的例子。（STAR）

推荐回答：

- **Situation**：流式 LLM 调用可能在任意时刻断开。如果直接自动 retry，可能重复输出；如果已经执行工具，甚至可能重复写状态。
- **Task**：要在用户体验和副作用安全之间取平衡：能继续的尽量继续，不能继续的要明确失败，不做危险重试。
- **Action**：provider 层只在连接阶段、未 yield 内容时自动 retry；AgentLoop 用 `IterationProgress` 记录进度；SSE 层捕获 `LLMError` 后判断 `PARTIAL_TEXT` 和 `TOOLS_READ_ONLY` 是否可 continue，并保存 `continuation_context`；cancel 作为独立 failure phase 处理；finally 做保底持久化。
- **Result**：系统避免了有副作用场景下的盲目重放，同时给部分文本中断提供“继续生成”能力，用户不必重新发起完整规划。

### Q：如果面试官质疑“你们的 model routing 没做完”，怎么回答？

推荐回答：

我会承认边界，但强调架构顺序是合理的：现在已经有 `LLMConfig`、provider factory、`llm_overrides` 配置解析、usage/cost 统计和统一 error 事件，这些是 routing 的前置条件。主路径目前还没有完整 `ModelRouter`，所以我不会声称已经做到动态多模型调度。

下一步我会把 routing 做成独立决策层，而不是把 if/else 散在 chat、memory、judge、worker 各处。这样后续才能根据 phase、任务类型、预算和 provider health 做一致决策，并且把路由原因写入 trace。这个回答既不回避 gap，也能说明我知道生产化 routing 应该长什么样。

## 10. 快速追问清单

### Q：`get_context_window()` 探测失败怎么办？

推荐回答：

启动时探测失败会静默回退到 `config.llm.context_window`，保证服务可用性优先。OpenAI provider 还内置了常见模型 context window 的 prefix registry，Anthropic provider 也有已知 Claude 模型窗口表。

### Q：为什么 `USAGE` chunk 在 SSE 层不直接发给前端？

推荐回答：

当前 `run_agent_stream()` 捕获 `USAGE` 后用于记录 `SessionStats`，不作为普通聊天事件透出。这样前端聊天体验不被 token 统计打断；需要看成本和 trace 时走 stats/trace 视图。生产化可以增加 debug 开关，把 usage 作为开发者事件展示。

### Q：provider 错误用户可见文案在哪里做？

推荐回答：

`LLMError` 保留 provider、model、raw_error 和错误码；用户可见文案由 `backend/api/orchestration/common/llm_errors.py::user_friendly_message` 生成。这样日志和前端展示分离，既能 debug，也避免把底层堆栈直接暴露给用户。

### Q：为什么 OpenAI provider 里有 DeepSeek 逻辑？

推荐回答：

因为很多网关或 OpenAI-compatible endpoint 会复用 OpenAI SDK，但返回 DeepSeek reasoning 字段。项目把它作为 OpenAI-compatible provider 的兼容分支处理，并且只在 model name 包含 `deepseek` 时附回 reasoning state，避免污染普通 OpenAI 请求。更长期的做法是拆独立 DeepSeek provider，让兼容逻辑更清晰。

### Q：当前 LLM infra 最值得展示的测试有哪些？

推荐回答：

我会展示：

- OpenAI streaming tool call 聚合测试；
- Anthropic tool-use non-stream fallback 测试；
- tool error payload 包含 `error_code` / `suggestion` 的双 provider 测试；
- `LLMError` 分类测试；
- `run_llm_turn()` progress 和 provider state 合并测试；
- telemetry span / usage 记录测试。

这些测试能证明项目不是只测最终文本，而是在测 Agent trajectory 的关键协议点。

### Q：`max_llm_errors` 这个配置项现在起作用了吗？

推荐回答：

`backend/agent/execution/limits.py` 里有 `max_llm_errors` 字段（默认 3），意图是限制单个 AgentLoop 内连续 LLM 失败次数，避免无限重试浪费成本。**但当前 `loop.py` 没有累计 enforcement 路径**：连续 LLM 失败超过该阈值并不会触发硬停。这个值更像"为下一步埋的预留维度"。

我之所以记得这个细节，是因为读代码时确认过：当前 LLMError 已经在 provider 层做了精细分类、在 stream 层决定 retryable / continue，**但没有 loop 层的"累计连续失败"熔断**。要补的话语义也清晰：连续 N 次 LLMError 且 progress 没推进 → 硬停并 emit 一条 cost-bound failure，避免少数 opaque error 在 long horizon agent 里堆积。

面试讲这个的目的不是炫细节，是表明**对 limits 配置的语义和 enforcement 链路都做了核对**，不是把存在的字段当已实现的功能。
