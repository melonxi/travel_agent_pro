# Context-Aware Compaction Implementation

## 背景

这次改动的目标是把原来的三层压缩收敛为两层：

1. `before_llm_call` 的总压缩
2. phase 切换时的阶段压缩

原先独立存在的“工具结果写时截断”被移除，其职责并入 `before_llm_call` 的总压缩器中。

## 本次改动概览

本次实现做了三件核心事：

1. 新增一个共享 compaction 模块，统一处理 prompt budget 计算、token 估算和 rich tool payload 压缩。
2. 重写 `before_llm_call` 的压缩流程，让它先做工具结果渐进式压缩，再决定是否需要做历史摘要压缩。
3. 移除 `AgentLoop` 里写入 `messages` 时的即时 tool 裁剪，避免压缩逻辑分散在两处。

phase 切换压缩逻辑没有改动，仍然沿用 `ContextManager.compress_for_transition()` 的规则摘要。

## 具体改动

### 1. 新增共享模块 `backend/agent/compaction.py`

新增文件：`backend/agent/compaction.py`

模块里实现了以下能力：

- `compute_prompt_budget(context_window, max_output_tokens, safety_margin=2000)`
  - 统一用 `context_window - max_tokens - safety_margin` 计算 prompt budget。
- `estimate_messages_tokens(messages, tools=...)`
  - 不再只估 `message.content`。
  - 现在会把以下内容都算进估算值：
    - `message.content`
    - `assistant.tool_calls`
    - `tool_result.data`
    - 当前轮传给模型的 `tools schema`
- `compact_messages_for_prompt(...)`
  - 这是总压缩器里的“工具压缩子步骤”。
  - 会根据预算占用比决定是否压缩，以及使用 `moderate` 或 `aggressive` 两档策略。
  - 除了看整体 `usage_ratio`，还会检查单条 TOOL 消息是否异常大，避免“总量没超、单条已经爆炸”的情况。

### 2. 把 rich tool 压缩策略并入总压缩器

`backend/agent/compaction.py` 中对两个富结果工具做了结构化压缩：

- `web_search`
  - 保留 `title`、`url`、`score`
  - 截断 `answer`
  - 截断每条 `results[].content`
  - 对结果列表做 top N 截断
  - 记录 `results_omitted_count`
- `xiaohongshu_search`
  - `search_notes`
    - 保留 `note_id`、`title`、`liked_count`、`note_type`、`url`
    - 对列表做 top N 截断
    - 记录 `items_omitted_count`
  - `read_note`
    - 保留 `note_id`、`title`、`desc`、计数字段、`tags`、`note_type`、`url`
    - 截断 `desc`
  - `get_comments`
    - 保留 `nickname`、`content`、`like_count`
    - 截断评论正文
    - 对评论列表做 top N 截断
    - 记录 `comments_omitted_count`

这里保留了 `url` 和关键句柄，没有再走“只保留 220 字正文片段”的旧策略。

### 3. 重写 `backend/main.py` 中的 `on_before_llm`

修改文件：`backend/main.py`

`on_before_llm` 现在的执行顺序是：

1. 从运行时 `context_window` 和 `config.llm.max_tokens` 计算 `prompt_budget`
2. 用共享 estimator 估算当前 prompt 大小
3. 先调用 `compact_messages_for_prompt(...)`
   - 如果工具结果压缩后已经回到预算内，就直接结束
4. 如果仍超预算，再调用 `context_mgr.should_compress(..., tools=tools)`
5. 如果仍需要压缩，再走历史摘要压缩：
   - 优先压缩较旧的 `compressible` 消息
   - 摘要内容复用 `compress_for_transition(...)`
   - 保留：
     - 原 system message
     - `must_keep` 用户偏好消息
     - 一条 `[对话摘要]`
     - 最近 4 条消息

同时，压缩事件里新增了更明确的字段：

- `estimated_tokens_after`
- `mode`
  - `tool_compaction`
  - `history_summary`

### 4. 修改 `backend/context/manager.py`

修改文件：`backend/context/manager.py`

`should_compress()` 做了两点变更：

- 增加 `tools` 参数
- 估算逻辑改为复用 `estimate_messages_tokens(...)`

这意味着压缩判定现在会考虑：

- tool results
- tool call arguments
- tool schema

而不再只是 `len(m.content or "") // 3`。

### 5. 修改 `backend/agent/loop.py`

修改文件：`backend/agent/loop.py`

这里做了两个关键调整：

- `before_llm_call` hook 现在会收到 `tools=tools`
- TOOL 消息写回 `messages` 时不再调用旧的 `compact_tool_result_for_messages(...)`

旧的以下逻辑已移除：

- `_truncate_text`
- `_compact_web_search_data`
- `_compact_xiaohongshu_data`
- `compact_tool_result_for_messages`

现在 `AgentLoop` 负责写入原始 `ToolResult`，压缩职责完全交给 LLM 调用前的总压缩器。

## 行为变化

### 1. 工具结果不再“写入时立刻截断”

现在的行为是：

- frontend SSE 仍然拿到完整 tool result
- `messages` 里先写入完整 `ToolResult`
- 只有在下一次真正调用 LLM 前，才由总压缩器按预算决定是否压缩

这让压缩行为和真实 prompt budget 更一致，也避免了过早、无条件裁剪。

### 2. 总压缩不再忽略 TOOL 消息

旧实现里：

- `should_compress()` 基本看不到 TOOL payload
- `on_before_llm()` 摘要阶段主要只处理 `USER/ASSISTANT` 文本

新实现里：

- TOOL payload 会参与预算估算
- TOOL payload 会先做专门的结构化压缩
- 如果还超预算，才进入历史摘要压缩

### 3. phase 压缩保持不变

`compress_for_transition()` 的职责没有变化：

- 仍然是 phase 前进或回退时使用
- 仍然走规则驱动摘要
- 仍然保留用户消息、助手摘要和工具决策指纹

## 测试改动

修改文件：

- `backend/tests/test_loop_payload_compaction.py`
- `backend/tests/test_context_manager.py`

### 新增和重写的测试覆盖点

`test_loop_payload_compaction.py` 现在覆盖：

- prompt budget 计算
- token estimator 是否覆盖 tool call / tool result / tools schema
- `web_search` 压缩后的正文截断与列表截断
- `xiaohongshu_search`
  - `search_notes`
  - `read_note`
  - `get_comments`
  的压缩行为

`test_context_manager.py` 新增了一条回归测试，确保：

- `should_compress()` 会把 tool payload 和 tool schema 算进去

## 验证结果

本次实际执行并通过了以下验证：

### 单元与集成测试

```bash
cd backend && .venv/bin/pytest tests/test_loop_payload_compaction.py tests/test_context_manager.py tests/test_agent_loop.py tests/test_telemetry_phase_context.py -q
```

结果：

- `54 passed`

```bash
cd backend && .venv/bin/pytest tests/test_api.py tests/test_phase_integration.py -q
```

结果：

- `21 passed`

### E2E

```bash
npx playwright test e2e-test.spec.ts
```

结果：

- `1 passed`

## 本次涉及的文件

新增：

- `backend/agent/compaction.py`
- `docs/context-aware-compaction-implementation.md`

修改：

- `backend/main.py`
- `backend/context/manager.py`
- `backend/agent/loop.py`
- `backend/tests/test_context_manager.py`
- `backend/tests/test_loop_payload_compaction.py`

## 未改动的部分

这次没有改动以下内容：

- `ContextManager.compress_for_transition()` 的核心 phase 摘要逻辑
- 前端 tool result 展示协议
- 各工具本身的返回结构定义
- phase 路由与状态推进逻辑

## 总结

这次改动的本质不是“把压缩做少”，而是“把压缩职责收口到真正该发生的位置”：

- 不再在工具执行后立刻盲目截断
- 改为在 LLM 调用前，根据真实 prompt 预算做渐进式压缩
- phase 切换压缩继续作为第二层独立机制保留

最终得到的是一个更干净的两层压缩架构：

1. pre-LLM context-aware 总压缩
2. phase transition 摘要压缩
