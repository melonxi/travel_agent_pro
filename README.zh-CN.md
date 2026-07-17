[English](README.md) | 简体中文

# Travel Agent Pro — 面向长任务的高可靠规划 Agent

一个旅行规划 Agent，但真正接受检验的是 **Agent Runtime**：受控自治、带版本化候选的并行
Worker、运行中 steering、局部重规划、基于 trace 的评测。旅行规划只是验证场景。

> **Portfolio 原型** — 独立开发，无生产用户。可靠性结论来自自动化测试、可执行的 golden
> eval、故障注入和确定性 trace 评分，而非线上流量。

[![ci](https://github.com/melonxi/travel_agent_pro/actions/workflows/ci.yml/badge.svg)](https://github.com/melonxi/travel_agent_pro/actions/workflows/ci.yml)

![Phase 1 目的地收敛：流式对话、工具调用卡片、阶段指示器与计划面板](screenshots/demos/phase1-recommendations.png)

*截图来自脚本化 demo fixture（确定性回放，非实时 LLM）。工程证据见
[`docs/evidence/`](docs/evidence/portfolio-proof.md)。*

## 这个 Runtime 真正解决的三个问题

**1. 并行 Worker 不能悄悄丢掉某一天的行程。**
Phase 3 会并行派发逐日 Worker。每个提案都进入版本化候选存储（`accepted` / `rejected` /
`superseded`）；只有 accepted 版本会提交到计划，所有提交都经过单一写入者，redispatch
失败会回滚，而不是留下写了一半的一天。
代码：[`agent/phase3/orchestrator.py`](backend/agent/phase3/orchestrator.py) ·
[`agent/phase3/candidate_store.py`](backend/agent/phase3/candidate_store.py) ·
[`state/plan_writers.py`](backend/state/plan_writers.py) —
测试：[`test_orchestrator.py`](backend/tests/test_orchestrator.py) ·
[`test_phase3_candidate_store.py`](backend/tests/test_phase3_candidate_store.py) ·
[`test_day_worker.py`](backend/tests/test_day_worker.py)

**2. 用户必须能干预一个正在执行的运行。**
`POST /api/chat/{session_id}/steer` 在 Agent 运行中把用户指令入队；队列只在安全边界
排空——绝不会插在 assistant `tool_call` 与其 `tool_result` 之间——因此 steering
不可能破坏工具协议。
代码：[`agent/steering.py`](backend/agent/steering.py) —
测试：[`test_steering.py`](backend/tests/test_steering.py)

**3. “它能跑”必须事后可检验。**
每次运行都写入 SQLite flight recorder（LLM 调用、工具输入输出、状态 diff、阶段门控）。
确定性 trace grader 基于这些事件执行评分规则；40 个 YAML golden case 和故障注入场景
对结果做断言。
代码：[`telemetry/trace_recorder.py`](backend/telemetry/trace_recorder.py) ·
[`evals/trace_grader.py`](backend/evals/trace_grader.py) ·
[`evals/runner.py`](backend/evals/runner.py) —
测试：[`test_trace_api.py`](backend/tests/test_trace_api.py) ·
[`test_eval_pipeline.py`](backend/tests/test_eval_pipeline.py)

## 已验证状态（2026-07-17）

| 信号 | 结果 | 复核方式 |
|------|------|----------|
| 每次 push 的 CI | A0 核心套件 + golden case 数量断言 + 前端构建 | [`ci.yml`](.github/workflows/ci.yml) |
| 完整单测套件 | **2054 passed** | `cd backend && OTEL_SDK_DISABLED=true uv run pytest -q -m "not integration"` |
| A0 核心可靠性套件 | **269 passed**（无需 API key） | `./scripts/run-a0-core.sh` |
| Golden eval 用例 | **40** 个可执行 YAML 用例（CI 断言数量） | [`backend/evals/golden_cases/`](backend/evals/golden_cases) |
| 故障注入 | **F1–F6 通过**，F7 部分通过 | [`fault-injection-report.md`](docs/evidence/fault-injection-report.md) |
| 基线 pass@3 | 12 用例 **1.00** —— mock executor，非实时 LLM | [`baseline-summary.md`](docs/evidence/baseline-summary.md) |

每个数字如何得出、以及刻意**不做**的声明，见
[`docs/evidence/portfolio-proof.md`](docs/evidence/portfolio-proof.md)。

## 架构

```text
Human-Agent Loop    React UI · SSE · sessions · steer · backtrack
        │  runtime input 每轮临时构造（system / history / user / turn context）
        ▼
Agent Loop          backend/agent/loop.py — 有界的 think-act-observe 迭代
  LLM turn → tool calls → Tool Engine → Plan Writers → validation · phase gate
        │                  （读工具并行、写工具顺序执行、单一写入者）
        ├─▶ Phase 3 Orchestrator–Workers    backend/agent/phase3/
        │     并行逐日 Worker · 版本化候选 · 仅 accepted 提交
        └─▶ Flight Recorder (SQLite) → Trace Grader → Golden Evals
              backend/telemetry/trace_recorder.py · backend/evals/
```

外层循环解释“用户为什么持续交互”（发送 / 停止 / 继续 / steer / 回退 / 切换
session）；内层循环解释“一轮如何推进 `TravelPlanState`”。两层循环的 mental model 见
[`docs/agent/START_HERE.md`](docs/agent/START_HERE.md)。

**为什么用显式循环而不是框架：** 阶段边界、单一写入者的计划变更、工具协议和失败恢复
都保持在精确、可测试的控制之下。这些契约可以移植到框架（例如 LangGraph）；重点是掌控
不变量，而不是排斥框架。

### 规划阶段

| Phase | 目标 | 代表工具 |
|-------|------|----------|
| 1 · 灵感与目的地锁定 | 把模糊意图收敛为目的地 | `xiaohongshu_search_notes`、`web_search`、`quick_travel_search` |
| 2 · 框架规划 | 旅行画像、候选池、骨架、交通与住宿锁定 | `set_trip_brief`、`set_skeleton_plans`、`search_flights`、`search_accommodations` |
| 3 · 逐日行程详排 | 把选定骨架展开为经过校验的逐日计划 | `optimize_day_route`、`save_day_plan`、`replace_all_day_plans` |
| 4 · 出发前查漏 | 最终检查，冻结 `travel_plan.md` + `checklist.md` 交付物 | `check_weather`、`search_travel_services`、`generate_summary` |

[`PhaseRouter`](backend/phase/router.py) 根据计划状态完整度自动推进阶段；Phase 2 按四个
子步骤（`brief → candidate → skeleton → lock`）渐进开放工具。用户可通过
`POST /api/backtrack/{session_id}` 回退任意阶段。

### 质量 Harness

五个独立层包裹主循环：**Guardrail**（输入净化、中文 prompt 注入检测）→ **Validator**
（硬约束：预算、日期、空值安全）→ **Judge**（LLM 软评分 1–5，带钳位）→
**Feasibility Gate**（基于规则的预算/时长检查，覆盖 30+ 目的地查找表，在昂贵的规划
之前运行）→ 每 session 的**成本与延迟追踪**。细节见
[`docs/agent/deep/harness-architecture.md`](docs/agent/deep/harness-architecture.md)。

## 如果你在 review 这个仓库：十分钟导读

1. [`backend/agent/loop.py`](backend/agent/loop.py) — 有界迭代契约：LLM turn、
   读并行/写顺序的工具批执行、repair notice、阶段切换时的 runtime rebuild。
2. [`backend/agent/steering.py`](backend/agent/steering.py) — 运行中 steering 与
   安全边界排空。
3. [`backend/agent/phase3/`](backend/agent/phase3) — orchestrator、逐日 Worker、
   候选存储：并行但不丢更新。
4. [`backend/state/plan_writers.py`](backend/state/plan_writers.py) — 所有计划变更
   的唯一入口；Worker 永远不直接写 `TravelPlanState`。
5. [`backend/telemetry/trace_recorder.py`](backend/telemetry/trace_recorder.py) +
   [`backend/evals/trace_grader.py`](backend/evals/trace_grader.py) — flight
   recorder 与确定性评分。
6. [`backend/evals/runner.py`](backend/evals/runner.py) +
   [`golden_cases/`](backend/evals/golden_cases) — YAML 用例 → 状态 / 工具 / 文本 /
   行程 / trace 评分断言 → JSON 报告。
7. [`docs/agent/`](docs/agent) — agent（和人）实际用来导航的文档系统：
   `START_HERE.md` → 任务路由 → slices → deep。

## 技术栈

- **后端** — Python 3.12 · FastAPI + `sse-starlette` · Pydantic v2 · OpenAI + Anthropic
  SDK（双 provider）· OpenTelemetry · pytest（约 2000 个测试，CI 跑 A0 核心子集）
- **前端** — TypeScript · React 19 · Vite 6 · Leaflet 地图 · SSE 流式 UI

## 三分钟启动

前置：Python ≥ 3.12、[uv](https://docs.astral.sh/uv/)（或 pip）、Node ≥ 18。
只有实时运行需要 OpenAI/Anthropic key——测试不需要任何 key。

### 后端

```bash
cd backend
uv sync --all-extras --frozen
# 不用 uv 的回退方案：
# python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"

# 实时运行需创建 .env 配置 provider：
cat > .env << 'EOF'
DEFAULT_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o
# 或：DEFAULT_PROVIDER=anthropic / ANTHROPIC_API_KEY / ANTHROPIC_MODEL
EOF

# 可选的非密钥配置：复制 config.example.yaml → ../config.yaml
uv run uvicorn main:app --reload --port 8000
```

### 前端

```bash
cd frontend
npm ci
npm run dev
# http://localhost:5173 — /api 代理到后端
```

### 测试

```bash
# 一键：后端 A0 核心 + 前端构建（与 CI 一致，无需 API key）
./scripts/run-a0-core.sh

# 完整单测套件（OTEL_SDK_DISABLED 免去 Jaeger 依赖）
cd backend && OTEL_SDK_DISABLED=true uv run pytest -q -m "not integration"
```

## 可观测性

OpenTelemetry 默认开启——用 `docker compose -f docker-compose.observability.yml up -d`
启动 Jaeger（UI 在 `localhost:16686`），或用 `OTEL_SDK_DISABLED=true` 关闭。独立于
OTel，每次运行都会把 **flight recorder** 持久化到 SQLite：`trace_runs` /
`trace_events` / `trace_artifacts` / `trace_grades`，约 20 个事件族，涵盖 `llm_call`、
`tool_result`、`state_diff`、`phase_gate`、`context_compression`、`phase3_worker`
等。大体积的 prompt / 工具 / 交付物内容以脱敏 artifact + 哈希存储。

调试一次运行：

```bash
curl http://127.0.0.1:8000/api/traces/<run_id>
curl -X POST http://127.0.0.1:8000/api/traces/<run_id>/grade
sqlite3 backend/data/sessions.db \
  "select event_type,count(*) from trace_events where run_id='<run_id>' group by event_type;"
```

评分规则失败时，`trace_grades` 的 `evidence_event_ids` 会直接指向对应的
`tool_call` / `state_diff` / `phase_gate` 行——从而修复对应的 prompt、工具或
validator，而不是对着最终回答猜。

如实说明边界：run 级持久化、artifact 元数据、trace 评分、40 个 golden case 已实现；
保留策略强制、artifact 权限模型、线上采样评测、CI 发布门禁是未来工作。细节见
[`docs/agent/slices/observability.md`](docs/agent/slices/observability.md) ·
[`docs/agent/deep/trace-flight-recorder.md`](docs/agent/deep/trace-flight-recorder.md)。

## API 端点（节选）

| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/sessions` | 创建 session |
| POST | `/api/chat/{session_id}` | SSE 流式对话 |
| POST | `/api/chat/{session_id}/steer` | 运行中 steering（入队，安全边界排空） |
| POST | `/api/chat/{session_id}/cancel` | 取消进行中的运行 |
| POST | `/api/backtrack/{session_id}` | 阶段 / 计划回退 |
| GET | `/api/plan/{session_id}` | 当前旅行计划状态 |
| GET | `/api/traces/{run_id}` | Flight recorder trace |
| POST | `/api/traces/{run_id}/grade` | 确定性 trace 评分 |
| GET | `/api/sessions/{session_id}/stats` | 成本 / token / 延迟统计 |
| GET | `/api/sessions/{session_id}/deliverables/{filename}` | 冻结的交付物 |

## 环境变量

| 变量 | 是否必需 | 说明 |
|------|----------|------|
| `DEFAULT_PROVIDER` | 实时运行必需 | `openai` 或 `anthropic` |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | 对应 provider 必需 | API key |
| `OPENAI_MODEL` / `ANTHROPIC_MODEL` | 否 | 模型覆盖 |
| `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` | 否 | 自定义端点 |
| `OPENWEATHER_API_KEY` | 否 | 天气 / 可行性工具 |
| `TAVILY_API_KEY` | 否 | Web 搜索回退 |
| `OTEL_SDK_DISABLED` | 否 | `true` 关闭 tracing（CI 使用） |

## 项目结构

```
travel_agent_pro/
├── backend/
│   ├── main.py                # FastAPI 应用（health、sessions、plan、chat SSE）
│   ├── agent/                 # Agent loop、steering、hooks、phase3 orchestrator-workers
│   ├── llm/                   # OpenAI / Anthropic provider + 工厂
│   ├── state/                 # TravelPlanState 模型 + plan writers（唯一写路径）
│   ├── tools/                 # 领域工具：@tool 装饰器，类型注解自动生成 JSON Schema
│   ├── phase/                 # 阶段 prompt、PhaseRouter、回退
│   ├── context/               # 4 层 system message 组装 + soul.md
│   ├── memory/                # 画像 / 工作记忆 / episodes + 分级召回
│   ├── harness/               # Guardrail、validator、judge、feasibility gate
│   ├── telemetry/             # OTel 接入 + SQLite flight recorder + session 统计
│   ├── evals/                 # Golden cases、runner、trace grader、stability
│   └── tests/                 # 约 2000 个 pytest 测试（CI 跑 A0 核心子集）
├── frontend/                  # React 19 + Vite 6：对话、地图、时间线、trace viewer
├── docs/
│   ├── agent/                 # START_HERE → slices → deep（agent 可导航文档）
│   ├── evidence/              # Portfolio 证据：基线、故障注入、traces
│   └── public-source-boundary.md
├── scripts/                   # run-a0-core.sh · demo 与故障分析 harness
└── .github/workflows/ci.yml   # A0 核心套件 + golden case 数量断言 + 前端构建
```

## 文档

- [`docs/agent/START_HERE.md`](docs/agent/START_HERE.md) — 两层循环 mental model，
  再按任务路由读 slices 与 deep（[`docs/agent/INDEX.md`](docs/agent/INDEX.md)）
- [`docs/evidence/portfolio-proof.md`](docs/evidence/portfolio-proof.md) — 30 秒证据
  速览：声明了什么、测量了什么、哪些是 mock
- [`PROJECT_OVERVIEW.md`](PROJECT_OVERVIEW.md) — 全量参考
- [`docs/public-source-boundary.md`](docs/public-source-boundary.md) — 什么公开、
  什么仅本地

## 许可证

MIT — 见 [`LICENSE`](LICENSE)。
