# Agent 文档入口

本文件是 agent 文档地图，不是鸟瞰入口。默认先读 `START_HERE.md` 建立两层循环视角，再按任务读取少量相关文档。

## 读取规则

1. 默认先读 `START_HERE.md`。
2. 再读 `TASK_ROUTING.md`。
3. 只读取当前任务命中的 `slices/` 文档。
4. 只有 slice 明确指向或任务需要细节时，才读取 `deep/` 文档。
5. 需要完整文档地图时读本文件。
6. 不要默认读取根目录 `PROJECT_OVERVIEW.md`；它是全量参考，只在用户明确要求完整项目全景时使用。

## 文档树

### Slices

- `slices/architecture.md`：系统定位、主模块、Phase 1/2/3/4 主路径。
- `slices/data-flow.md`：一次 chat 请求从 API 到 AgentLoop、工具、SSE、持久化的主链路。
- `slices/memory.md`：v3 记忆、召回、提取、归档的最小上下文。
- `slices/context-compression.md`：上下文构建、压缩、runtime rebuild、context_epoch。
- `slices/llm-providers.md`：LLM Provider 抽象、按阶段切换、错误归一化。
- `slices/tools.md`：工具系统、读写分类、plan writer、Phase 2 工具门控。
- `slices/frontend.md`：React 三栏布局、SSE 消费、关键组件。
- `slices/persistence.md`：SQLite、文件系统、session/message/trace/deliverables。
- `slices/api.md`：HTTP/SSE 端点与 API 编排边界。
- `slices/testing.md`：pytest、Playwright、golden eval、trace grader、canary。
- `slices/observability.md`：OpenTelemetry、SessionStats、Trace flight recorder。

### Deep

- `deep/phase-flow.md`：Phase 1/2/3/4 细节、阶段推进、回退。
- `deep/phase3-parallel.md`：Phase 3 Orchestrator-Workers 并行路径。
- `deep/memory-recall.md`：Stage 0-4 recall / reranker / extraction 细节。
- `deep/tool-state-writes.md`：17 个状态写工具和写后处理。
- `deep/sse-events.md`：前端消费的 SSE 事件协议。
- `deep/sqlite-schema.md`：SQLite 表与消息历史语义。
- `deep/harness-architecture.md`：5 层质量守护。
- `deep/trace-flight-recorder.md`：run-scoped trace 事件与 grader。
- `deep/architecture-comparison-pi.md`：与 earendil-works/pi 的架构对比及改进空间。

## 更新规则

- 改动影响某个主题时，先更新对应 slice。
- 如果细节超过 slice 的最小上下文，放到 deep，并在 slice 里链接过去。
- slice 控制在“能让 agent 开始工作的上下文”范围内，不写历史，不堆实现流水账。
- 根目录 `PROJECT_OVERVIEW.md` 可作为人工全景参考，但不要让它重新成为默认 agent 入口。
