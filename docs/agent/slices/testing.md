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

Phase 3 黑板 / Steering 回归还必须覆盖真实边界：候选 artifact 被拒后不得进入 handoff、
POI 名称别名与泛化活动名、交通/住宿全口径预算、两日 MOVE 成对重派，以及并发中已启动
worker 的 steering redispatch；还要覆盖 artifact 写入顺序与 attempt 数字倒挂、最后版本 rejected
或重派未提交时不得被动复活旧 accepted、step7 期间 steering 必须触发 attempt=5，以及 run 尾部
残留 steering 必须收到终结 ack。仅用不写 artifact 的 fake worker 不能证明 artifact 不变量。
重派失败路径必须显式注入（现有理想数据默认“重派必成功”）：steering(attempt=2)、
late-steering(attempt=5)、再协商(attempt=4)、8b 修复(attempt=3) 四段各有“重派失败 →
该天以回滚版本交付、不静默丢天”的触发性回归；乱序 activities 与跨午夜 end_time 不得被
边界校验误拒；泛化 location.name 必须回退 activity.name 参与跨天去重。

测试中的行程日期禁止写死：`past_date` guardrail 会拦截过去日期的
`save_day_plan`/`check_weather` 等调用，写死的“未来”日期一旦过期，整条依赖写入成功的
断言链（实时校验反馈、soft judge、quality gate、天气拒绝）都会静默失效，形成日期定时
炸弹。统一用 `date.today() + timedelta(days=N)` 生成未来日期（见各测试文件的
`_future_date` / `_trip_date` 辅助函数）。

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
