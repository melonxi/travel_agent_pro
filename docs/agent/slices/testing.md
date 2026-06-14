# Testing Slice

## 什么时候读

当任务涉及测试、回归、eval、canary、trace grader、Playwright 或验证策略时读取。

## 测试层级

- 后端单元测试：`backend/tests/`，覆盖 AgentLoop、LLM、状态、阶段路由、工具、存储、压缩、验证、遥测、guardrail、eval。
- plan tools 专项：`backend/tests/test_plan_tools/`。
- Memory 集成测试：`backend/tests/test_memory_integration.py`。
- E2E：根目录 `e2e-*.spec.ts`，Playwright 覆盖主流程、重试体验、等待体验、Phase 1 不跑题等。
- Golden eval：`backend/evals/golden_cases/`。
- Reranker eval：固定 Stage 0/1/2 输出与候选，只验证 Stage 4 reranker。
- Canary：`scripts/run-adaptive-canary.py` 和 `scripts/run-full-phase-canary.py`。
- Stability：`scripts/eval-stability.py`。
- Failure analysis：`scripts/failure-analysis/`。

## 关键断言

Golden cases 支持多类断言，包括：
- `phase_reached`
- `state_field_set`
- `tool_called`
- `tool_not_called`
- `contains_text`
- `not_contains_text`
- `budget_within`
- `memory_recall_field`
- `daily_plans_count`
- `daily_plans_have_activities`
- `deliverable_field_set`
- `trace_grade_status`

## 运行命令

```bash
cd backend && pytest
npx playwright test e2e-test.spec.ts
```

## 深入阅读

- Harness：`../deep/harness-architecture.md`
- Trace grader：`../deep/trace-flight-recorder.md`
