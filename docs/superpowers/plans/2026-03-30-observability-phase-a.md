# 可观测性阶段 A：OTel Tracing 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 travel-agent-pro 引入全链路 tracing 能力，覆盖 HTTP → Agent 迭代 → LLM 调用 → 工具执行 → Phase 转换的完整链路。

**Architecture:** 混合三层方案 —— Layer 1 FastAPI 自动 instrumentation，Layer 2 `@traced` 装饰器标注业务函数，Layer 3 Hook 注入动态属性。通过 `config.yaml` 开关控制，关闭时零开销。

**Tech Stack:** OpenTelemetry SDK, OTLP Exporter, Jaeger all-in-one, opentelemetry-instrumentation-fastapi

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `backend/telemetry/__init__.py` | 导出公共 API: `setup_telemetry`, `traced` |
| `backend/telemetry/setup.py` | OTel SDK 初始化 + Jaeger exporter + FastAPI instrumentation |
| `backend/telemetry/decorators.py` | `@traced` 装饰器实现（支持 sync/async） |
| `backend/telemetry/attributes.py` | span 属性语义化常量 |
| `backend/config.py` | 新增 `TelemetryConfig` 数据类 |
| `backend/main.py` | 调用 `setup_telemetry(app)` |
| `backend/agent/loop.py` | `@traced` 标注 `run()` |
| `backend/llm/openai_provider.py` | `@traced` 标注 `chat()` |
| `backend/llm/anthropic_provider.py` | `@traced` 标注 `chat()` |
| `backend/tools/engine.py` | `@traced` 标注 `execute()` |
| `backend/phase/router.py` | `@traced` 标注 `check_and_apply_transition()` |
| `backend/context/manager.py` | `@traced` 标注 `should_compress()` |
| `docker-compose.observability.yml` | Jaeger all-in-one 容器 |
| `backend/tests/test_telemetry_setup.py` | setup 模块测试 |
| `backend/tests/test_telemetry_decorators.py` | 装饰器测试 |
| `backend/tests/test_telemetry_integration.py` | 集成测试（trace 树验证） |

---

### Task 1: 属性常量模块 `attributes.py`

**Files:**
- Create: `backend/telemetry/__init__.py`
- Create: `backend/telemetry/attributes.py`
- Test: `backend/tests/test_telemetry_attributes.py`

- [ ] **Step 1: 创建 `telemetry/__init__.py` 占位**

```python
# backend/telemetry/__init__.py
```

空文件，后续 Task 补充导出。

- [ ] **Step 2: 编写属性常量测试**

```python
# backend/tests/test_telemetry_attributes.py
from telemetry.attributes import (
    AGENT_SESSION_ID,
    AGENT_PHASE,
    AGENT_ITERATION,
    TOOL_NAME,
    TOOL_STATUS,
    TOOL_ERROR_CODE,
    LLM_PROVIDER,
    LLM_MODEL,
    LLM_TOKENS_IN,
    LLM_TOKENS_OUT,
    PHASE_FROM,
    PHASE_TO,
    CONTEXT_TOKENS_BEFORE,
    CONTEXT_TOKENS_AFTER,
)


def test_attributes_are_strings():
    attrs = [
        AGENT_SESSION_ID, AGENT_PHASE, AGENT_ITERATION,
        TOOL_NAME, TOOL_STATUS, TOOL_ERROR_CODE,
        LLM_PROVIDER, LLM_MODEL, LLM_TOKENS_IN, LLM_TOKENS_OUT,
        PHASE_FROM, PHASE_TO,
        CONTEXT_TOKENS_BEFORE, CONTEXT_TOKENS_AFTER,
    ]
    for attr in attrs:
        assert isinstance(attr, str)
        assert "." in attr, f"{attr} should use dotted notation"


def test_attributes_unique():
    from telemetry import attributes
    values = [v for k, v in vars(attributes).items() if not k.startswith("_")]
    assert len(values) == len(set(values)), "Attribute values must be unique"
```

- [ ] **Step 3: 运行测试，验证失败**

Run: `cd backend && python -m pytest tests/test_telemetry_attributes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telemetry.attributes'`

- [ ] **Step 4: 实现属性常量**

```python
# backend/telemetry/attributes.py
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

- [ ] **Step 5: 运行测试，验证通过**

Run: `cd backend && python -m pytest tests/test_telemetry_attributes.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/telemetry/__init__.py backend/telemetry/attributes.py backend/tests/test_telemetry_attributes.py
git commit -m "feat(telemetry): add span attribute constants"
```

---

### Task 2: 配置扩展 — `TelemetryConfig`

**Files:**
- Modify: `backend/config.py`
- Test: `backend/tests/test_telemetry_config.py`

- [ ] **Step 1: 编写配置测试**

```python
# backend/tests/test_telemetry_config.py
import os
import tempfile
from pathlib import Path
from config import load_config, TelemetryConfig


def test_telemetry_config_defaults():
    cfg = TelemetryConfig()
    assert cfg.enabled is True
    assert cfg.endpoint == "http://localhost:4317"
    assert cfg.service_name == "travel-agent-pro"


def test_load_config_without_telemetry_section():
    """config.yaml 没有 telemetry 段时使用默认值。"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("llm:\n  provider: openai\n")
        f.flush()
        cfg = load_config(f.name)
    os.unlink(f.name)
    assert cfg.telemetry.enabled is True
    assert cfg.telemetry.endpoint == "http://localhost:4317"


def test_load_config_with_telemetry_section():
    """config.yaml 有 telemetry 段时使用配置值。"""
    yaml_content = """
llm:
  provider: openai
telemetry:
  enabled: false
  endpoint: "http://otel-collector:4317"
  service_name: "my-app"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(yaml_content)
        f.flush()
        cfg = load_config(f.name)
    os.unlink(f.name)
    assert cfg.telemetry.enabled is False
    assert cfg.telemetry.endpoint == "http://otel-collector:4317"
    assert cfg.telemetry.service_name == "my-app"
```

- [ ] **Step 2: 运行测试，验证失败**

Run: `cd backend && python -m pytest tests/test_telemetry_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'TelemetryConfig' from 'config'`

- [ ] **Step 3: 在 `config.py` 中添加 `TelemetryConfig`**

在 `config.py` 中 `AppConfig` 定义之前添加：

```python
@dataclass(frozen=True)
class TelemetryConfig:
    enabled: bool = True
    endpoint: str = "http://localhost:4317"
    service_name: str = "travel-agent-pro"
```

在 `AppConfig` 的字段列表中添加：

```python
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
```

在 `load_config` 函数的 `return AppConfig(...)` 中添加 telemetry 构建逻辑。在 `api_keys = _build_api_keys(...)` 之后添加：

```python
    tel_raw = raw.get("telemetry", {})
    telemetry = TelemetryConfig(
        enabled=tel_raw.get("enabled", True),
        endpoint=tel_raw.get("endpoint", "http://localhost:4317"),
        service_name=tel_raw.get("service_name", "travel-agent-pro"),
    )
```

在最终 `return AppConfig(...)` 调用中添加 `telemetry=telemetry`。同时在无 YAML 分支中也添加 `telemetry=TelemetryConfig()`。

- [ ] **Step 4: 运行测试，验证通过**

Run: `cd backend && python -m pytest tests/test_telemetry_config.py -v`
Expected: PASS

- [ ] **Step 5: 运行既有测试，确保无回归**

Run: `cd backend && python -m pytest tests/ -v --ignore=tests/test_e2e_golden_path.py -x`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add backend/config.py backend/tests/test_telemetry_config.py
git commit -m "feat(config): add TelemetryConfig dataclass"
```

---

### Task 3: `@traced` 装饰器

**Files:**
- Create: `backend/telemetry/decorators.py`
- Test: `backend/tests/test_telemetry_decorators.py`

- [ ] **Step 1: 编写装饰器测试**

```python
# backend/tests/test_telemetry_decorators.py
import pytest
from unittest.mock import patch, MagicMock
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

from telemetry.decorators import traced


@pytest.fixture(autouse=True)
def setup_tracer():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        trace.get_tracer_provider().__class__.__module__
        and __import__(
            "opentelemetry.sdk.trace", fromlist=["SimpleSpanProcessor"]
        ).SimpleSpanProcessor(exporter)
    )
    # 使用更简洁的方式
    from opentelemetry.sdk.trace import TracerProvider as TP
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = InMemorySpanExporter()
    provider = TP()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


def test_traced_sync_function(setup_tracer):
    exporter = setup_tracer

    @traced()
    def add(a, b):
        return a + b

    result = add(1, 2)
    assert result == 3

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "add" in spans[0].name


async def test_traced_async_function(setup_tracer):
    exporter = setup_tracer

    @traced()
    async def fetch(url):
        return f"data from {url}"

    result = await fetch("http://example.com")
    assert result == "data from http://example.com"

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert "fetch" in spans[0].name


def test_traced_custom_name(setup_tracer):
    exporter = setup_tracer

    @traced(name="custom.span.name")
    def my_func():
        return 42

    my_func()
    spans = exporter.get_finished_spans()
    assert spans[0].name == "custom.span.name"


def test_traced_records_exception(setup_tracer):
    exporter = setup_tracer

    @traced()
    def fail():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        fail()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == trace.StatusCode.ERROR
    events = span.events
    assert any(e.name == "exception" for e in events)


async def test_traced_async_records_exception(setup_tracer):
    exporter = setup_tracer

    @traced()
    async def async_fail():
        raise RuntimeError("async boom")

    with pytest.raises(RuntimeError, match="async boom"):
        await async_fail()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR


def test_traced_record_args(setup_tracer):
    exporter = setup_tracer

    @traced(record_args=["name", "count"])
    def greet(name, count, secret):
        return f"hello {name} x{count}"

    greet("alice", 3, "password123")
    spans = exporter.get_finished_spans()
    attrs = dict(spans[0].attributes)
    assert attrs["arg.name"] == "alice"
    assert attrs["arg.count"] == 3
    assert "arg.secret" not in attrs


def test_traced_disabled_is_noop():
    """当 telemetry 未启用时，@traced 不创建 span。"""
    # 重置 provider 为 NoOp
    trace.set_tracer_provider(trace.NoOpTracerProvider())

    @traced()
    def simple():
        return "ok"

    result = simple()
    assert result == "ok"
```

- [ ] **Step 2: 安装 OTel 依赖**

Run: `cd backend && pip install "opentelemetry-api>=1.20.0" "opentelemetry-sdk>=1.20.0"`

- [ ] **Step 3: 运行测试，验证失败**

Run: `cd backend && python -m pytest tests/test_telemetry_decorators.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telemetry.decorators'`

- [ ] **Step 4: 实现 `@traced` 装饰器**

```python
# backend/telemetry/decorators.py
from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Callable, Sequence

from opentelemetry import trace

_MODULE = "travel-agent-pro"


def traced(
    name: str | None = None,
    record_args: Sequence[str] | None = None,
) -> Callable:
    """装饰器：为函数创建 OTel span，支持 sync 和 async。"""

    def decorator(fn: Callable) -> Callable:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"
        sig = inspect.signature(fn)
        param_names = list(sig.parameters.keys())

        def _set_arg_attrs(span: trace.Span, args, kwargs):
            if not record_args:
                return
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            for arg_name in record_args:
                if arg_name in bound.arguments:
                    val = bound.arguments[arg_name]
                    # OTel 仅接受 str/int/float/bool
                    if isinstance(val, (str, int, float, bool)):
                        span.set_attribute(f"arg.{arg_name}", val)
                    else:
                        span.set_attribute(f"arg.{arg_name}", str(val))

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                tracer = trace.get_tracer(_MODULE)
                with tracer.start_as_current_span(span_name) as span:
                    _set_arg_attrs(span, args, kwargs)
                    try:
                        return await fn(*args, **kwargs)
                    except Exception as exc:
                        span.set_status(
                            trace.Status(trace.StatusCode.ERROR, str(exc))
                        )
                        span.record_exception(exc)
                        raise

            return async_wrapper
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                tracer = trace.get_tracer(_MODULE)
                with tracer.start_as_current_span(span_name) as span:
                    _set_arg_attrs(span, args, kwargs)
                    try:
                        return fn(*args, **kwargs)
                    except Exception as exc:
                        span.set_status(
                            trace.Status(trace.StatusCode.ERROR, str(exc))
                        )
                        span.record_exception(exc)
                        raise

            return sync_wrapper

    return decorator
```

- [ ] **Step 5: 运行测试，验证通过**

Run: `cd backend && python -m pytest tests/test_telemetry_decorators.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/telemetry/decorators.py backend/tests/test_telemetry_decorators.py
git commit -m "feat(telemetry): implement @traced decorator with sync/async support"
```

---

### Task 4: OTel SDK 初始化 `setup.py`

**Files:**
- Create: `backend/telemetry/setup.py`
- Test: `backend/tests/test_telemetry_setup.py`

- [ ] **Step 1: 编写 setup 测试**

```python
# backend/tests/test_telemetry_setup.py
from unittest.mock import MagicMock, patch

from opentelemetry import trace

from config import TelemetryConfig


def test_setup_telemetry_enabled():
    """enabled=True 时应配置 TracerProvider。"""
    from telemetry.setup import setup_telemetry

    app = MagicMock()
    config = TelemetryConfig(enabled=True, endpoint="http://localhost:4317")
    setup_telemetry(app, config)

    provider = trace.get_tracer_provider()
    # 验证 provider 不是 NoOp
    assert not isinstance(provider, trace.NoOpTracerProvider)


def test_setup_telemetry_disabled():
    """enabled=False 时不应配置 TracerProvider，保持 NoOp。"""
    # 先重置为 NoOp
    trace.set_tracer_provider(trace.NoOpTracerProvider())

    from telemetry.setup import setup_telemetry

    app = MagicMock()
    config = TelemetryConfig(enabled=False)
    setup_telemetry(app, config)

    provider = trace.get_tracer_provider()
    assert isinstance(provider, trace.NoOpTracerProvider)


def test_setup_telemetry_sets_service_name():
    """应在 Resource 中设置 service.name。"""
    from telemetry.setup import setup_telemetry
    from opentelemetry.sdk.trace import TracerProvider as SdkTP

    app = MagicMock()
    config = TelemetryConfig(
        enabled=True,
        service_name="test-service",
        endpoint="http://localhost:4317",
    )
    setup_telemetry(app, config)

    provider = trace.get_tracer_provider()
    if isinstance(provider, SdkTP):
        resource_attrs = dict(provider.resource.attributes)
        assert resource_attrs.get("service.name") == "test-service"
```

- [ ] **Step 2: 安装额外 OTel 依赖**

Run: `cd backend && pip install "opentelemetry-exporter-otlp>=1.20.0" "opentelemetry-instrumentation-fastapi>=0.41b0"`

- [ ] **Step 3: 运行测试，验证失败**

Run: `cd backend && python -m pytest tests/test_telemetry_setup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'telemetry.setup'`

- [ ] **Step 4: 实现 `setup.py`**

```python
# backend/telemetry/setup.py
from __future__ import annotations

from fastapi import FastAPI

from config import TelemetryConfig


def setup_telemetry(app: FastAPI, config: TelemetryConfig) -> None:
    """初始化 OTel tracing。enabled=False 时为 no-op。"""
    if not config.enabled:
        return

    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    resource = Resource.create({"service.name": config.service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=config.endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
```

- [ ] **Step 5: 运行测试，验证通过**

Run: `cd backend && python -m pytest tests/test_telemetry_setup.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/telemetry/setup.py backend/tests/test_telemetry_setup.py
git commit -m "feat(telemetry): implement OTel SDK setup with OTLP exporter"
```

---

### Task 5: 更新 `__init__.py` 导出 + 添加依赖

**Files:**
- Modify: `backend/telemetry/__init__.py`
- Modify: `backend/pyproject.toml`

- [ ] **Step 1: 更新 `telemetry/__init__.py` 导出公共 API**

```python
# backend/telemetry/__init__.py
from telemetry.setup import setup_telemetry
from telemetry.decorators import traced

__all__ = ["setup_telemetry", "traced"]
```

- [ ] **Step 2: 在 `pyproject.toml` 添加 OTel 依赖**

在 `dependencies` 列表末尾添加：

```toml
    "opentelemetry-api>=1.20.0",
    "opentelemetry-sdk>=1.20.0",
    "opentelemetry-exporter-otlp>=1.20.0",
    "opentelemetry-instrumentation-fastapi>=0.41b0",
```

在 `[tool.setuptools.packages.find]` 的 `include` 列表中添加 `"telemetry*"`。

- [ ] **Step 3: 验证导入正常**

Run: `cd backend && python -c "from telemetry import setup_telemetry, traced; print('OK')"`
Expected: `OK`

- [ ] **Step 4: 提交**

```bash
git add backend/telemetry/__init__.py backend/pyproject.toml
git commit -m "feat(telemetry): export public API and add OTel dependencies"
```

---

### Task 6: 在 `main.py` 中接入 `setup_telemetry`

**Files:**
- Modify: `backend/main.py`
- Test: `backend/tests/test_telemetry_integration.py`

- [ ] **Step 1: 编写集成测试**

```python
# backend/tests/test_telemetry_integration.py
from unittest.mock import patch, MagicMock
from config import TelemetryConfig


def test_create_app_calls_setup_telemetry():
    """create_app 应调用 setup_telemetry。"""
    with patch("main.setup_telemetry") as mock_setup:
        from main import create_app
        app = create_app()
        mock_setup.assert_called_once()
        call_args = mock_setup.call_args
        # 第一个参数是 FastAPI app
        assert call_args[0][0] is app
        # 第二个参数是 TelemetryConfig
        assert isinstance(call_args[0][1], TelemetryConfig)
```

- [ ] **Step 2: 运行测试，验证失败**

Run: `cd backend && python -m pytest tests/test_telemetry_integration.py::test_create_app_calls_setup_telemetry -v`
Expected: FAIL — `AttributeError: module 'main' has no attribute 'setup_telemetry'`

- [ ] **Step 3: 修改 `main.py`**

在 `main.py` 的 import 区域添加：

```python
from telemetry import setup_telemetry
```

在 `create_app` 函数中，`app = FastAPI(title="Travel Agent Pro")` 之后、`app.add_middleware(...)` 之前添加：

```python
    setup_telemetry(app, config.telemetry)
```

- [ ] **Step 4: 运行测试，验证通过**

Run: `cd backend && python -m pytest tests/test_telemetry_integration.py::test_create_app_calls_setup_telemetry -v`
Expected: PASS

- [ ] **Step 5: 运行全量测试确保无回归**

Run: `cd backend && python -m pytest tests/ -v --ignore=tests/test_e2e_golden_path.py -x`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add backend/main.py backend/tests/test_telemetry_integration.py
git commit -m "feat(telemetry): wire setup_telemetry into FastAPI app"
```

---

### Task 7: 埋点 — Agent Loop

**Files:**
- Modify: `backend/agent/loop.py`
- Test: `backend/tests/test_telemetry_agent_loop.py`

- [ ] **Step 1: 编写 agent loop tracing 测试**

```python
# backend/tests/test_telemetry_agent_loop.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolResult
from llm.types import ChunkType, LLMChunk
from tools.engine import ToolEngine


@pytest.fixture(autouse=True)
def otel_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


async def test_agent_loop_creates_span(otel_exporter):
    """AgentLoop.run() 应创建 agent_loop.run span。"""
    async def fake_chat(messages, tools=None, stream=True):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="hello")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    engine = ToolEngine()
    hooks = HookManager()

    loop = AgentLoop(llm=llm, tool_engine=engine, hooks=hooks)
    messages = [Message(role=Role.USER, content="hi")]

    chunks = []
    async for chunk in loop.run(messages, phase=1):
        chunks.append(chunk)

    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert any("agent_loop.run" in n for n in span_names)
```

- [ ] **Step 2: 运行测试，验证失败**

Run: `cd backend && python -m pytest tests/test_telemetry_agent_loop.py -v`
Expected: FAIL — 没有匹配到 `agent_loop.run` span

- [ ] **Step 3: 在 `agent/loop.py` 添加 tracing**

在 `agent/loop.py` 顶部添加导入：

```python
from opentelemetry import trace
from telemetry.attributes import AGENT_PHASE, AGENT_ITERATION
```

将 `run` 方法体用 span 包裹。在 `async def run(...)` 方法中，将整个函数体包裹在 span 上下文中：

```python
    async def run(
        self,
        messages: list[Message],
        phase: int,
        tools_override: list[dict] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        tracer = trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("agent_loop.run") as span:
            span.set_attribute(AGENT_PHASE, phase)
            tools = tools_override or self.tool_engine.get_tools_for_phase(phase)

            for iteration in range(20):
                with tracer.start_as_current_span("agent_loop.iteration") as iter_span:
                    iter_span.set_attribute(AGENT_ITERATION, iteration)
                    await self.hooks.run("before_llm_call", messages=messages, phase=phase)

                    tool_calls: list[ToolCall] = []
                    text_chunks: list[str] = []

                    async for chunk in self.llm.chat(messages, tools=tools, stream=True):
                        if chunk.type == ChunkType.TEXT_DELTA:
                            text_chunks.append(chunk.content or "")
                            yield chunk
                        elif chunk.type == ChunkType.TOOL_CALL_START and chunk.tool_call:
                            tool_calls.append(chunk.tool_call)
                            yield chunk
                        elif chunk.type == ChunkType.DONE:
                            pass

                    if not tool_calls:
                        full_text = "".join(text_chunks)
                        if full_text:
                            messages.append(Message(role=Role.ASSISTANT, content=full_text))
                        yield LLMChunk(type=ChunkType.DONE)
                        return

                    messages.append(
                        Message(
                            role=Role.ASSISTANT,
                            content="".join(text_chunks) or None,
                            tool_calls=tool_calls,
                        )
                    )

                    for tc in tool_calls:
                        result = await self.tool_engine.execute(tc)
                        messages.append(Message(role=Role.TOOL, tool_result=result))
                        await self.hooks.run(
                            "after_tool_call",
                            tool_name=tc.name,
                            tool_call=tc,
                            result=result,
                        )

            yield LLMChunk(
                type=ChunkType.TEXT_DELTA, content="[达到最大循环次数，请重新发送消息]"
            )
            yield LLMChunk(type=ChunkType.DONE)
```

- [ ] **Step 4: 运行测试，验证通过**

Run: `cd backend && python -m pytest tests/test_telemetry_agent_loop.py -v`
Expected: PASS

- [ ] **Step 5: 运行既有 agent loop 测试确保无回归**

Run: `cd backend && python -m pytest tests/test_agent_loop.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/agent/loop.py backend/tests/test_telemetry_agent_loop.py
git commit -m "feat(telemetry): add tracing to AgentLoop.run with iteration spans"
```

---

### Task 8: 埋点 — Tool Engine

**Files:**
- Modify: `backend/tools/engine.py`
- Test: `backend/tests/test_telemetry_tool_engine.py`

- [ ] **Step 1: 编写 tool engine tracing 测试**

```python
# backend/tests/test_telemetry_tool_engine.py
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

from agent.types import ToolCall
from tools.base import ToolDef, ToolError
from tools.engine import ToolEngine


@pytest.fixture(autouse=True)
def otel_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


async def test_tool_execute_creates_span(otel_exporter):
    engine = ToolEngine()

    async def my_tool(**kwargs):
        return {"result": "ok"}

    engine.register(ToolDef(
        name="test_tool",
        description="test",
        phases=[1],
        parameters={},
        _fn=my_tool,
    ))

    call = ToolCall(id="t1", name="test_tool", arguments={})
    result = await engine.execute(call)

    assert result.status == "success"
    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "tool.execute" in span_names

    span = next(s for s in spans if s.name == "tool.execute")
    assert span.attributes["tool.name"] == "test_tool"
    assert span.attributes["tool.status"] == "success"


async def test_tool_execute_error_span(otel_exporter):
    engine = ToolEngine()

    async def fail_tool(**kwargs):
        raise ToolError("bad input", error_code="INVALID_INPUT")

    engine.register(ToolDef(
        name="fail_tool",
        description="test",
        phases=[1],
        parameters={},
        _fn=fail_tool,
    ))

    call = ToolCall(id="t2", name="fail_tool", arguments={})
    result = await engine.execute(call)

    assert result.status == "error"
    spans = otel_exporter.get_finished_spans()
    span = next(s for s in spans if s.name == "tool.execute")
    assert span.attributes["tool.name"] == "fail_tool"
    assert span.attributes["tool.status"] == "error"
    assert span.attributes["tool.error_code"] == "INVALID_INPUT"
```

- [ ] **Step 2: 运行测试，验证失败**

Run: `cd backend && python -m pytest tests/test_telemetry_tool_engine.py -v`
Expected: FAIL — 找不到 `tool.execute` span

- [ ] **Step 3: 在 `tools/engine.py` 添加 tracing**

在 `tools/engine.py` 顶部添加导入：

```python
from opentelemetry import trace
from telemetry.attributes import TOOL_NAME, TOOL_STATUS, TOOL_ERROR_CODE
```

修改 `execute` 方法，用 span 包裹：

```python
    async def execute(self, call: ToolCall) -> ToolResult:
        tracer = trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("tool.execute") as span:
            span.set_attribute(TOOL_NAME, call.name)

            tool_def = self._tools.get(call.name)
            if not tool_def:
                span.set_attribute(TOOL_STATUS, "error")
                span.set_attribute(TOOL_ERROR_CODE, "UNKNOWN_TOOL")
                return ToolResult(
                    tool_call_id=call.id,
                    status="error",
                    error=f"Unknown tool: {call.name}",
                    error_code="UNKNOWN_TOOL",
                    suggestion=f"Available tools: {', '.join(self._tools.keys())}",
                )

            try:
                data = await tool_def(**call.arguments)
                span.set_attribute(TOOL_STATUS, "success")
                return ToolResult(
                    tool_call_id=call.id,
                    status="success",
                    data=data,
                )
            except ToolError as e:
                span.set_attribute(TOOL_STATUS, "error")
                span.set_attribute(TOOL_ERROR_CODE, e.error_code or "TOOL_ERROR")
                return ToolResult(
                    tool_call_id=call.id,
                    status="error",
                    error=str(e),
                    error_code=e.error_code,
                    suggestion=e.suggestion,
                )
            except Exception as e:
                span.set_attribute(TOOL_STATUS, "error")
                span.set_attribute(TOOL_ERROR_CODE, "INTERNAL_ERROR")
                span.record_exception(e)
                return ToolResult(
                    tool_call_id=call.id,
                    status="error",
                    error=str(e),
                    error_code="INTERNAL_ERROR",
                    suggestion="An unexpected error occurred",
                )
```

- [ ] **Step 4: 运行测试，验证通过**

Run: `cd backend && python -m pytest tests/test_telemetry_tool_engine.py tests/test_tool_engine.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/tools/engine.py backend/tests/test_telemetry_tool_engine.py
git commit -m "feat(telemetry): add tracing to ToolEngine.execute"
```

---

### Task 9: 埋点 — LLM Providers

**Files:**
- Modify: `backend/llm/openai_provider.py`
- Modify: `backend/llm/anthropic_provider.py`
- Test: `backend/tests/test_telemetry_llm.py`

- [ ] **Step 1: 编写 LLM tracing 测试**

```python
# backend/tests/test_telemetry_llm.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

from agent.types import Message, Role
from telemetry.attributes import LLM_PROVIDER, LLM_MODEL


@pytest.fixture(autouse=True)
def otel_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


async def test_openai_chat_creates_span(otel_exporter):
    """OpenAI provider chat 应创建 llm.chat span。"""
    from llm.types import ChunkType, LLMChunk

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "hi"
    mock_choice.message.tool_calls = None
    mock_response.choices = [mock_choice]

    with patch("llm.openai_provider.AsyncOpenAI") as MockClient:
        instance = MockClient.return_value
        instance.chat.completions.create = AsyncMock(return_value=mock_response)

        from llm.openai_provider import OpenAIProvider
        provider = OpenAIProvider(model="gpt-4o")
        messages = [Message(role=Role.USER, content="hello")]

        chunks = []
        async for c in provider.chat(messages, stream=False):
            chunks.append(c)

    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "llm.chat" in span_names

    span = next(s for s in spans if s.name == "llm.chat")
    assert span.attributes[LLM_PROVIDER] == "openai"
    assert span.attributes[LLM_MODEL] == "gpt-4o"
```

- [ ] **Step 2: 运行测试，验证失败**

Run: `cd backend && python -m pytest tests/test_telemetry_llm.py -v`
Expected: FAIL — 找不到 `llm.chat` span

- [ ] **Step 3: 在 `openai_provider.py` 添加 tracing**

在 `llm/openai_provider.py` 顶部添加导入：

```python
from opentelemetry import trace as otel_trace
from telemetry.attributes import LLM_PROVIDER, LLM_MODEL
```

在 `chat` 方法开头创建 span：在 `async def chat(...)` 方法中，在 `kwargs` 构建之前添加：

```python
        tracer = otel_trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("llm.chat") as span:
            span.set_attribute(LLM_PROVIDER, "openai")
            span.set_attribute(LLM_MODEL, self.model)
```

将整个方法体（从 `kwargs = {...}` 开始）缩进包裹在该 `with` 块内。所有 `yield` 和 `return` 保持在该块内。

- [ ] **Step 4: 在 `anthropic_provider.py` 添加同样的 tracing**

在 `llm/anthropic_provider.py` 顶部添加导入：

```python
from opentelemetry import trace as otel_trace
from telemetry.attributes import LLM_PROVIDER, LLM_MODEL
```

在 `chat` 方法开头创建 span，包裹整个方法体：

```python
        tracer = otel_trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("llm.chat") as span:
            span.set_attribute(LLM_PROVIDER, "anthropic")
            span.set_attribute(LLM_MODEL, self.model)
            # ... 原有方法体缩进包裹 ...
```

- [ ] **Step 5: 运行测试，验证通过**

Run: `cd backend && python -m pytest tests/test_telemetry_llm.py tests/test_openai_provider.py tests/test_anthropic_provider.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add backend/llm/openai_provider.py backend/llm/anthropic_provider.py backend/tests/test_telemetry_llm.py
git commit -m "feat(telemetry): add tracing to LLM providers"
```

---

### Task 10: 埋点 — Phase Router & Context Manager

**Files:**
- Modify: `backend/phase/router.py`
- Modify: `backend/context/manager.py`
- Test: `backend/tests/test_telemetry_phase_context.py`

- [ ] **Step 1: 编写测试**

```python
# backend/tests/test_telemetry_phase_context.py
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

from agent.types import Message, Role
from phase.router import PhaseRouter
from context.manager import ContextManager
from state.models import TravelPlanState


@pytest.fixture(autouse=True)
def otel_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    provider.shutdown()


def test_phase_transition_creates_span(otel_exporter):
    router = PhaseRouter()
    plan = TravelPlanState(session_id="s1", phase=1, destination="Tokyo")
    changed = router.check_and_apply_transition(plan)

    assert changed is True
    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "phase.transition" in span_names

    span = next(s for s in spans if s.name == "phase.transition")
    assert span.attributes["phase.from"] == 1
    assert span.attributes["phase.to"] == plan.phase


def test_no_transition_no_span(otel_exporter):
    """没有发生 phase 变化时不应创建 transition span。"""
    router = PhaseRouter()
    plan = TravelPlanState(session_id="s1", phase=1)
    changed = router.check_and_apply_transition(plan)

    assert changed is False
    spans = otel_exporter.get_finished_spans()
    transition_spans = [s for s in spans if s.name == "phase.transition"]
    assert len(transition_spans) == 0


def test_context_compress_check_creates_span(otel_exporter):
    ctx = ContextManager()
    messages = [
        Message(role=Role.USER, content="a" * 3000),
        Message(role=Role.ASSISTANT, content="b" * 3000),
    ]
    result = ctx.should_compress(messages, max_tokens=100)

    spans = otel_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert "context.should_compress" in span_names
```

- [ ] **Step 2: 运行测试，验证失败**

Run: `cd backend && python -m pytest tests/test_telemetry_phase_context.py -v`
Expected: FAIL — 找不到相关 span

- [ ] **Step 3: 在 `phase/router.py` 添加 tracing**

在 `phase/router.py` 顶部添加导入：

```python
from opentelemetry import trace
from telemetry.attributes import PHASE_FROM, PHASE_TO
```

修改 `check_and_apply_transition` 方法：

```python
    def check_and_apply_transition(self, plan: TravelPlanState) -> bool:
        inferred = self.infer_phase(plan)
        if inferred != plan.phase:
            tracer = trace.get_tracer("travel-agent-pro")
            with tracer.start_as_current_span("phase.transition") as span:
                span.set_attribute(PHASE_FROM, plan.phase)
                span.set_attribute(PHASE_TO, inferred)
                plan.phase = inferred
            return True
        return False
```

- [ ] **Step 4: 在 `context/manager.py` 添加 tracing**

在 `context/manager.py` 顶部添加导入：

```python
from opentelemetry import trace
from telemetry.attributes import CONTEXT_TOKENS_BEFORE, CONTEXT_TOKENS_AFTER
```

修改 `should_compress` 方法：

```python
    def should_compress(self, messages: list[Message], max_tokens: int) -> bool:
        tracer = trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("context.should_compress") as span:
            estimated = sum(len(m.content or "") // 3 for m in messages)
            span.set_attribute(CONTEXT_TOKENS_BEFORE, estimated)
            span.set_attribute("context.max_tokens", max_tokens)
            result = estimated > max_tokens * 0.5
            return result
```

- [ ] **Step 5: 运行测试，验证通过**

Run: `cd backend && python -m pytest tests/test_telemetry_phase_context.py tests/test_phase_router.py tests/test_context_manager.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add backend/phase/router.py backend/context/manager.py backend/tests/test_telemetry_phase_context.py
git commit -m "feat(telemetry): add tracing to PhaseRouter and ContextManager"
```

---

### Task 11: Docker Compose — Jaeger

**Files:**
- Create: `docker-compose.observability.yml`

- [ ] **Step 1: 创建 docker-compose 文件**

```yaml
# docker-compose.observability.yml
services:
  jaeger:
    image: jaegertracing/all-in-one:latest
    ports:
      - "4317:4317"   # OTLP gRPC
      - "16686:16686" # Jaeger UI
    environment:
      - COLLECTOR_OTLP_ENABLED=true
```

- [ ] **Step 2: 验证文件语法**

Run: `docker compose -f docker-compose.observability.yml config --quiet && echo "VALID"`
Expected: `VALID`

- [ ] **Step 3: 提交**

```bash
git add docker-compose.observability.yml
git commit -m "infra: add Jaeger docker-compose for local tracing"
```

---

### Task 12: 全量回归测试

**Files:** 无新增

- [ ] **Step 1: 运行全量单元测试**

Run: `cd backend && python -m pytest tests/ -v --ignore=tests/test_e2e_golden_path.py`
Expected: 全部 PASS

- [ ] **Step 2: 修复任何失败的测试**

如有失败，分析原因并修复。常见问题：
- OTel 相关 import 在无依赖环境下失败 → 确保依赖已安装
- span context 干扰既有测试 → 在 fixture 中重置 tracer provider

- [ ] **Step 3: 提交修复（如有）**

```bash
git add -u
git commit -m "fix(telemetry): resolve test regressions"
```
