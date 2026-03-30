# 可观测性设计 — 阶段 A：开发调试 Tracing

## 概述

为 travel-agent-pro 项目引入全链路 tracing 能力，解决当前零可观测性的问题。采用 OpenTelemetry + Jaeger 的混合三层架构，覆盖从 HTTP 请求到 Agent 迭代、LLM 调用、工具执行、Phase 转换的完整链路。

**阶段定位：** 这是两阶段计划的第一阶段（A），聚焦开发调试场景。阶段 B（生产监控：指标、告警、Dashboard）将在阶段 A 稳定后独立设计实施。

## 技术选型

| 决策 | 选择 | 理由 |
|------|------|------|
| Tracing 标准 | OpenTelemetry | 开放标准，不绑定 LLM 框架，阶段 B 可无缝切换后端 |
| 本地可视化 | Jaeger all-in-one | Docker 一键启动，无需注册，与 OTel 标配 |
| 排除项 | LangFuse/LangSmith | 项目非 LangChain 生态，引入会增加平台依赖 |

## 架构：混合三层方案

### Layer 1 — HTTP 自动 Instrumentation

使用 `opentelemetry-instrumentation-fastapi` 为每个 HTTP 请求自动创建 root span。

- 在 `main.py` 启动时一行初始化
- 自动记录：HTTP method、path、status code、延迟
- 零代码侵入

### Layer 2 — `@traced` 装饰器

新建 `backend/telemetry/` 模块，提供 `@traced` 装饰器标注关键业务函数。

- 自动创建 child span，记录函数名、参数摘要、耗时、异常
- 支持同步和异步函数
- span name 默认为 `module.function_name`，可自定义
- 自动捕获异常并记录到 span status + events

### Layer 3 — Hook 注入动态属性

在现有 `HookManager` 回调中，往当前 span 追加运行时属性。

- `before_llm_call`: model name、message count、phase
- `after_tool_call`: tool name、status、error_code、token usage
- 不创建新 span，仅补充上下文

## 模块结构

```
backend/telemetry/
├── __init__.py          # 导出公共 API：setup_telemetry, traced
├── setup.py             # OTel SDK 初始化 + Jaeger exporter + FastAPI instrumentation
├── decorators.py        # @traced 装饰器实现
└── attributes.py        # span 属性常量定义
```

### setup.py

- 初始化 `TracerProvider`，配置 `BatchSpanProcessor` + `OTLPSpanExporter`
- 配置 resource 属性：`service.name=travel-agent-pro`
- 挂载 FastAPI 自动 instrumentation
- 读取 `config.yaml` 中 `telemetry` 配置段

### decorators.py

- `@traced(name=None, record_args=None)` 装饰器
- 支持 async/sync 函数
- 异常自动记录到 span status + event
- `record_args` 指定要记录为 span attributes 的参数名列表

### attributes.py

语义化常量，避免魔法字符串：

```python
AGENT_SESSION_ID = "agent.session_id"
AGENT_PHASE = "agent.phase"
AGENT_ITERATION = "agent.iteration"
TOOL_NAME = "tool.name"
TOOL_STATUS = "tool.status"
TOOL_ERROR_CODE = "tool.error_code"
LLM_PROVIDER = "llm.provider"
LLM_MODEL = "llm.model"
LLM_TOKENS_IN = "llm.tokens.input"
LLM_TOKENS_OUT = "llm.tokens.output"
PHASE_FROM = "phase.from"
PHASE_TO = "phase.to"
CONTEXT_TOKENS_BEFORE = "context.tokens.before"
CONTEXT_TOKENS_AFTER = "context.tokens.after"
```

## Trace 树结构

一个完整请求的 trace 树示例：

```
HTTP POST /chat/{session_id}              ← Layer 1 自动
  └─ agent_loop.run                       ← @traced
      ├─ iteration[0]                     ← @traced
      │   ├─ llm.chat                     ← @traced
      │   │   attrs: model, tokens_in, tokens_out, latency
      │   ├─ tool.execute[search_flights] ← @traced
      │   │   attrs: tool_name, status, duration
      │   └─ tool.execute[check_weather]  ← @traced
      ├─ iteration[1]                     ← @traced
      │   ├─ llm.chat                     ← @traced
      │   ├─ phase.transition             ← @traced
      │   │   attrs: from_phase, to_phase
      │   └─ context.compress             ← @traced（如触发）
      │       attrs: before_tokens, after_tokens
      └─ iteration[2]                     ← @traced
          └─ llm.chat (final response)    ← @traced
```

## 埋点清单

| 文件 | 函数 | Span 名 | 关键属性 |
|------|------|---------|---------|
| `agent/loop.py` | `run()` | `agent_loop.run` | session_id, phase, iteration_count |
| `agent/loop.py` | 每次迭代 | `agent_loop.iteration` | iteration_index |
| `llm/openai_provider.py` | `chat()` | `llm.chat` | model, tokens_in, tokens_out, provider |
| `llm/anthropic_provider.py` | `chat()` | `llm.chat` | model, tokens_in, tokens_out, provider |
| `tools/engine.py` | `execute()` | `tool.execute` | tool_name, status, error_code |
| `phase/router.py` | `check_and_apply_transition()` | `phase.transition` | from_phase, to_phase |
| `context/manager.py` | `compress()` | `context.compress` | before_tokens, after_tokens |
| `agent/hooks.py` | hook 回调 | 不创建新 span | 往当前 span 追加属性 |

## 配置

在 `config.yaml` 新增：

```yaml
telemetry:
  enabled: true
  endpoint: "http://localhost:4317"
  service_name: "travel-agent-pro"
```

**开关机制：** `enabled: false` 时 `@traced` 变为 no-op，零性能开销。不启动 Jaeger 也不影响应用运行。

## 依赖

新增 Python 包（加入 `pyproject.toml`）：

```
opentelemetry-api>=1.20.0
opentelemetry-sdk>=1.20.0
opentelemetry-exporter-otlp>=1.20.0
opentelemetry-instrumentation-fastapi>=0.41b0
```

## 本地开发环境

项目根目录新增 `docker-compose.observability.yml`：

```yaml
services:
  jaeger:
    image: jaegertracing/all-in-one:latest
    ports:
      - "4317:4317"
      - "16686:16686"
    environment:
      - COLLECTOR_OTLP_ENABLED=true
```

启动：`docker compose -f docker-compose.observability.yml up -d`
查看 trace：浏览器打开 `http://localhost:16686`

## 不在阶段 A 范围内

以下能力留给阶段 B（生产监控）：

- Metrics（counters, histograms）
- 告警规则
- Grafana Dashboard
- 日志聚合（结构化 JSON logging）
- Token 成本计算与报表
- 采样策略（生产环境降低 trace 量）
