# 可观测性设计 — 阶段 B：结构化调试日志（Span Events）

## 概述

在 Phase A tracing 基础上，为关键 span 添加结构化 events，让开发者在 Jaeger 中点开 span 即可看到详细的调试信息（tool 出入参、LLM 请求摘要、phase 状态快照等）。

**阶段定位：** Phase A 回答"调用了什么、耗时多少"，Phase B 回答"具体传了什么、返回了什么"。

**设计约束：**
- 零新增依赖，复用 Phase A 的 OTel + Jaeger 基础设施
- 所有 event 内容做截断保护，单个 event payload 不超过 1KB
- 仅修改已有文件，不新增模块

## 不在范围内

- Metrics / Grafana Dashboard / 告警规则（留给生产阶段）
- 完整 LLM prompt/response 记录（数据量过大）
- 采样策略
- Token 成本计算

## 截断机制

在 `telemetry/attributes.py` 中新增：

```python
def truncate(value: str, max_len: int = 512) -> str:
    if len(value) <= max_len:
        return value
    return value[:max_len] + "...(truncated)"
```

所有 event 的字符串字段统一通过此函数处理。

## Event 名称常量

在 `telemetry/attributes.py` 中新增：

```python
EVENT_TOOL_INPUT = "tool.input"
EVENT_TOOL_OUTPUT = "tool.output"
EVENT_LLM_REQUEST = "llm.request"
EVENT_LLM_RESPONSE = "llm.response"
EVENT_PHASE_PLAN_SNAPSHOT = "phase.plan_snapshot"
EVENT_CONTEXT_COMPRESSION = "context.compression"
```

## 各 Span 的 Event 详情

### `tool.execute` span — 2 个 event

| Event 名 | 时机 | 属性 | 截断规则 |
|-----------|------|------|----------|
| `tool.input` | 执行前 | `arguments`: tool 入参 JSON | 超过 512 字符截断 |
| `tool.output` | 执行后 | 成功: `data` (出参 JSON)；失败: `error`, `error_code` | `data` 超过 512 字符截断 |

### `llm.chat` span — 2 个 event

| Event 名 | 时机 | 属性 | 截断规则 |
|-----------|------|------|----------|
| `llm.request` | 调用前 | `message_count`, `total_chars`, `has_tools` | 固定小，不截断 |
| `llm.response` | 流结束后 | `text_preview` (前 200 字符), `tool_calls` (名称列表 JSON) | `text_preview` 固定 200 上限 |

### `phase.transition` span — 1 个 event

| Event 名 | 时机 | 属性 | 截断规则 |
|-----------|------|------|----------|
| `phase.plan_snapshot` | 转换时 | `destination`, `dates`, `daily_plans_count` | 固定小，不截断 |

### `context.should_compress` span — 1 个 event（仅触发压缩时）

| Event 名 | 时机 | 属性 | 截断规则 |
|-----------|------|------|----------|
| `context.compression` | 判定需压缩时 | `message_count`, `estimated_tokens`, `must_keep_count` | 固定小，不截断 |

## 改动文件清单

| 文件 | 改动内容 |
|------|----------|
| `backend/telemetry/attributes.py` | 新增 `truncate()` 函数 + 6 个 event 名称常量 |
| `backend/tools/engine.py` | 在 `execute()` 的 span 中添加 `tool.input` 和 `tool.output` event |
| `backend/llm/openai_provider.py` | 在 `chat()` 的 span 中添加 `llm.request` 和 `llm.response` event |
| `backend/llm/anthropic_provider.py` | 同上 |
| `backend/phase/router.py` | 在 `phase.transition` span 中添加 `phase.plan_snapshot` event |
| `backend/context/manager.py` | 在 `context.should_compress` span 中添加 `context.compression` event |

## 测试策略

为每个改动点新增测试，验证 span 上存在对应的 event 且内容正确。复用 Phase A 的 OTel test fixture（`InMemorySpanExporter` + `_reset_tracer_provider()`）。

验证方式：
```python
spans = exporter.get_finished_spans()
span = next(s for s in spans if s.name == "tool.execute")
events = span.events
assert any(e.name == "tool.input" for e in events)
```

## Jaeger 中的查看效果

点开一个 `tool.execute` span 后，在 Logs 区域会看到：

```
tool.input    {"arguments": "{\"destination\": \"Tokyo\", \"dates\": ...}"}
tool.output   {"data": "{\"flights\": [{\"airline\": \"ANA\", ...}]...(truncated)"}
```

点开一个 `llm.chat` span 后：

```
llm.request   {"message_count": 5, "total_chars": 2340, "has_tools": true}
llm.response  {"text_preview": "好的，我来帮你搜索东京的航班...", "tool_calls": "[\"search_flights\"]"}
```
