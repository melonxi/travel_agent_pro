# Observability Slice

## 什么时候读

当任务涉及 OpenTelemetry、Jaeger、SessionStats、trace viewer、run-scoped trace、trace grader、成本/延迟或 failure analysis 时读取。

## OpenTelemetry spans

```text
agent_loop.run
agent_loop.iteration
tool.execute
llm.chat
phase.transition
context.should_compress
orchestrator.run
day_worker.run.day_N
```

## 应用内可观测性

- `SessionStats` 记录 token、成本、延迟、工具调用、状态变化、validation、judge、memory hit、recall telemetry。
- legacy `/api/sessions/{session_id}/trace` 继续从 `SessionStats` 构建 TraceViewer 视图。
- run-scoped flight recorder 写入 `trace_runs` / `trace_events` / `trace_artifacts` / `trace_grades`。
- `/api/traces/{run_id}` 提供 run 级 trace。
- `/api/traces/{run_id}/grade` 运行 deterministic trace grader。

## Trace 用途

- 前端 TraceViewer 展示阶段、工具、LLM、memory、quality gate 等行为。
- canary 从持久化 trace 审计工具调用，覆盖 SSE 看不到的 Phase 3 子 agent。
- failure-analysis 用 trace evidence 做失败归因。

## 深入阅读

- Trace flight recorder：`../deep/trace-flight-recorder.md`
- Testing/eval：`testing.md`
