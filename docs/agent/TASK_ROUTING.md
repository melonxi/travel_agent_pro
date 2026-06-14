# Task Routing

按任务读取文档。命中多个任务时，读取对应 slice 的并集；deep 文档只在需要细节时读。

## 改 Agent 主循环 / 阶段行为

读取：
- `slices/architecture.md`
- `slices/data-flow.md`
- `slices/tools.md`
- `slices/context-compression.md`

必要时：
- `deep/phase-flow.md`
- `deep/tool-state-writes.md`
- `deep/harness-architecture.md`

## 改 Phase 3 日程详排 / 并行 worker

读取：
- `slices/architecture.md`
- `slices/data-flow.md`
- `slices/tools.md`

必要时：
- `deep/phase-flow.md`
- `deep/phase3-parallel.md`
- `deep/tool-state-writes.md`

## 改工具 / plan writer / 状态写入

读取：
- `slices/tools.md`
- `slices/data-flow.md`
- `slices/persistence.md`

必要时：
- `deep/tool-state-writes.md`
- `deep/harness-architecture.md`

## 改记忆召回 / 记忆提取 / profile / working memory

读取：
- `slices/memory.md`
- `slices/data-flow.md`
- `slices/persistence.md`
- `slices/observability.md`

必要时：
- `deep/memory-recall.md`
- `deep/sqlite-schema.md`
- `deep/trace-flight-recorder.md`

## 改上下文压缩 / runtime rebuild / 消息历史

读取：
- `slices/context-compression.md`
- `slices/data-flow.md`
- `slices/persistence.md`

必要时：
- `deep/sqlite-schema.md`

## 改 LLM provider / 错误恢复 / continue

读取：
- `slices/llm-providers.md`
- `slices/data-flow.md`
- `slices/api.md`

必要时：
- `deep/sse-events.md`

## 改前端 UI / SSE / Trace / Memory 面板

读取：
- `slices/frontend.md`
- `slices/api.md`
- `slices/observability.md`

必要时：
- `deep/sse-events.md`
- `deep/trace-flight-recorder.md`

## 改数据库 / session 恢复 / deliverables

读取：
- `slices/persistence.md`
- `slices/api.md`
- `slices/context-compression.md`

必要时：
- `deep/sqlite-schema.md`

## 改 API 路由 / 编排层

读取：
- `slices/api.md`
- `slices/data-flow.md`
- `slices/persistence.md`

必要时：
- `deep/sse-events.md`

## 改测试 / eval / canary / trace grader

读取：
- `slices/testing.md`
- `slices/observability.md`

必要时：
- `deep/harness-architecture.md`
- `deep/trace-flight-recorder.md`

## 只问项目整体是什么

读取：
- `START_HERE.md`
- `slices/architecture.md`

如果用户明确要求完整全景，再读：
- `PROJECT_OVERVIEW.md`
