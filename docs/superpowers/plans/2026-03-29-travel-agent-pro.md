# Travel Agent Pro 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个基于纯手写 Agent Loop 的旅行规划系统，覆盖七阶段认知决策流，Python 后端 + TypeScript 前端。

**Architecture:** 单 Agent + 阶段路由器。Agent Loop 是通用引擎，通过钩子注入旅行业务逻辑。Phase Router 根据 plan_state 字段填充程度做确定性阶段推断，切换系统提示和工具子集。所有状态持久化为 JSON 文件。

**Tech Stack:** Python 3.12 + FastAPI / openai + anthropic SDK / TypeScript + React + Vite / Leaflet / pydantic

---

## File Structure

```
travel_agent_pro/
├── backend/
│   ├── pyproject.toml
│   ├── main.py                        # FastAPI 入口
│   ├── config.py                      # 配置加载
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── types.py                   # Message, ToolCall, ToolResult 等核心类型
│   │   ├── hooks.py                   # HookManager 钩子注册和执行
│   │   └── loop.py                    # AgentLoop 核心循环
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── types.py                   # LLMChunk, LLMResponse 统一输出类型
│   │   ├── base.py                    # LLMProvider 协议定义
│   │   ├── openai_provider.py         # OpenAI 实现
│   │   ├── anthropic_provider.py      # Anthropic 实现
│   │   └── factory.py                 # 根据配置创建 provider
│   ├── state/
│   │   ├── __init__.py
│   │   ├── models.py                  # TravelPlanState 及子类型
│   │   └── manager.py                 # StateManager CRUD + 快照
│   ├── phase/
│   │   ├── __init__.py
│   │   ├── prompts.py                 # 各阶段系统提示文本
│   │   └── router.py                  # PhaseRouter 阶段推断 + 切换 + 回溯
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── base.py                    # ToolDef, ToolError, @tool 装饰器
│   │   ├── engine.py                  # ToolEngine 注册 + 调度 + 重试
│   │   ├── update_plan_state.py       # 更新规划状态
│   │   ├── search_destinations.py     # 目的地搜索
│   │   ├── check_feasibility.py       # 签证/季节/安全校验
│   │   ├── search_flights.py          # 航班搜索
│   │   ├── search_accommodations.py   # 住宿搜索
│   │   ├── get_poi_info.py            # POI 详情
│   │   ├── calculate_route.py         # 路线计算
│   │   ├── assemble_day_plan.py       # 单日行程组装
│   │   ├── check_availability.py      # 可用性查询
│   │   ├── check_weather.py           # 天气预报
│   │   └── generate_summary.py        # 出行摘要
│   ├── context/
│   │   ├── __init__.py
│   │   ├── manager.py                 # ContextManager 四层拼装 + 压缩
│   │   └── soul.md                    # SOUL.md 常驻提示
│   ├── memory/
│   │   ├── __init__.py
│   │   ├── models.py                  # UserMemory, Rejection 等类型
│   │   └── manager.py                 # MemoryManager 读写 + 摘要
│   ├── harness/
│   │   ├── __init__.py
│   │   ├── validator.py               # 硬约束验证器（5 条规则）
│   │   └── judge.py                   # 软约束 LLM Judge
│   └── tests/
│       ├── __init__.py
│       ├── test_types.py
│       ├── test_hooks.py
│       ├── test_llm_types.py
│       ├── test_openai_provider.py
│       ├── test_anthropic_provider.py
│       ├── test_state_models.py
│       ├── test_state_manager.py
│       ├── test_phase_router.py
│       ├── test_tool_base.py
│       ├── test_tool_engine.py
│       ├── test_update_plan_state.py
│       ├── test_search_destinations.py
│       ├── test_check_feasibility.py
│       ├── test_search_flights.py
│       ├── test_search_accommodations.py
│       ├── test_get_poi_info.py
│       ├── test_calculate_route.py
│       ├── test_assemble_day_plan.py
│       ├── test_check_availability.py
│       ├── test_check_weather.py
│       ├── test_generate_summary.py
│       ├── test_context_manager.py
│       ├── test_memory.py
│       ├── test_harness_validator.py
│       ├── test_harness_judge.py
│       ├── test_agent_loop.py
│       └── test_api.py
│
├── frontend/
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── types/
│       │   └── plan.ts                # 前端类型（与后端 models 对应）
│       ├── hooks/
│       │   └── useSSE.ts              # SSE 流式订阅 hook
│       ├── components/
│       │   ├── ChatPanel.tsx           # 对话面板
│       │   ├── MessageBubble.tsx       # 单条消息
│       │   ├── PhaseIndicator.tsx      # 阶段指示器
│       │   ├── MapView.tsx             # 地图可视化
│       │   ├── Timeline.tsx            # 行程时间线
│       │   └── BudgetChart.tsx         # 预算仪表盘
│       └── styles/
│           └── index.css
│
├── config.yaml                        # 项目配置
├── .gitignore
└── data/                              # 运行时数据（gitignore）
```

---

### Task 1: 项目脚手架与配置

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/config.py`
- Create: `config.yaml`
- Create: `backend/tests/__init__.py`
- Modify: `.gitignore`

- [ ] **Step 1: 创建后端 pyproject.toml**

```toml
# backend/pyproject.toml
[project]
name = "travel-agent-pro"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "openai>=1.50.0",
    "anthropic>=0.40.0",
    "pydantic>=2.9.0",
    "pyyaml>=6.0",
    "httpx>=0.27.0",
    "sse-starlette>=2.0.0",
    "tiktoken>=0.7.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24.0",
    "pytest-cov>=5.0",
    "respx>=0.21.0",
]

[build-system]
requires = ["setuptools>=75.0"]
build-backend = "setuptools.backends._legacy:_Backend"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: 创建 config.yaml**

```yaml
# config.yaml
llm:
  provider: "openai"
  model: "gpt-4o"
  temperature: 0.7
  max_tokens: 4096

llm_overrides:
  phase_1_2:
    provider: "anthropic"
    model: "claude-sonnet-4-20250514"
  phase_5:
    provider: "openai"
    model: "gpt-4o"

api_keys:
  google_maps: "${GOOGLE_MAPS_API_KEY}"
  amadeus_key: "${AMADEUS_API_KEY}"
  amadeus_secret: "${AMADEUS_API_SECRET}"
  openweather: "${OPENWEATHER_API_KEY}"

data_dir: "./data"
max_retries: 3
context_compression_threshold: 0.5
```

- [ ] **Step 3: 创建 config.py**

```python
# backend/config.py
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "openai"
    model: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: int = 4096


@dataclass(frozen=True)
class ApiKeysConfig:
    google_maps: str = ""
    amadeus_key: str = ""
    amadeus_secret: str = ""
    openweather: str = ""


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    llm_overrides: dict[str, LLMConfig] = field(default_factory=dict)
    api_keys: ApiKeysConfig = field(default_factory=ApiKeysConfig)
    data_dir: str = "./data"
    max_retries: int = 3
    context_compression_threshold: float = 0.5


def _resolve_env(value: str) -> str:
    """Replace ${ENV_VAR} with actual environment variable value."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_name = value[2:-1]
        return os.environ.get(env_name, "")
    return value


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    path = Path(path)
    if not path.exists():
        return AppConfig()

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    llm_raw = raw.get("llm", {})
    llm = LLMConfig(**{k: v for k, v in llm_raw.items() if k in LLMConfig.__dataclass_fields__})

    overrides: dict[str, LLMConfig] = {}
    for key, val in raw.get("llm_overrides", {}).items():
        overrides[key] = LLMConfig(**{k: v for k, v in val.items() if k in LLMConfig.__dataclass_fields__})

    api_raw = raw.get("api_keys", {})
    api_keys = ApiKeysConfig(**{k: _resolve_env(v) for k, v in api_raw.items() if k in ApiKeysConfig.__dataclass_fields__})

    return AppConfig(
        llm=llm,
        llm_overrides=overrides,
        api_keys=api_keys,
        data_dir=raw.get("data_dir", "./data"),
        max_retries=raw.get("max_retries", 3),
        context_compression_threshold=raw.get("context_compression_threshold", 0.5),
    )
```

- [ ] **Step 4: 更新 .gitignore**

在现有 `.gitignore` 末尾追加：

```gitignore
# Project runtime data
data/

# Superpowers
.superpowers/
```

- [ ] **Step 5: 创建包初始化文件和安装依赖**

创建空 `__init__.py` 文件：
- `backend/agent/__init__.py`
- `backend/llm/__init__.py`
- `backend/state/__init__.py`
- `backend/phase/__init__.py`
- `backend/tools/__init__.py`
- `backend/context/__init__.py`
- `backend/memory/__init__.py`
- `backend/harness/__init__.py`
- `backend/tests/__init__.py`

Run: `cd backend && pip install -e ".[dev]"`

- [ ] **Step 6: 提交**

```bash
git add backend/pyproject.toml backend/config.py config.yaml .gitignore backend/agent/__init__.py backend/llm/__init__.py backend/state/__init__.py backend/phase/__init__.py backend/tools/__init__.py backend/context/__init__.py backend/memory/__init__.py backend/harness/__init__.py backend/tests/__init__.py
git commit -m "feat: project scaffolding with config and dependencies"
```

---

### Task 2: 核心类型定义

**Files:**
- Create: `backend/agent/types.py`
- Create: `backend/llm/types.py`
- Create: `backend/tests/test_types.py`
- Create: `backend/tests/test_llm_types.py`

- [ ] **Step 1: 写 agent/types.py 的失败测试**

```python
# backend/tests/test_types.py
from agent.types import Message, Role, ToolCall, ToolResult


def test_message_user():
    msg = Message(role=Role.USER, content="hello")
    assert msg.role == Role.USER
    assert msg.content == "hello"
    assert msg.tool_calls is None


def test_message_assistant_with_tool_calls():
    tc = ToolCall(id="tc_1", name="search_flights", arguments={"origin": "PVG"})
    msg = Message(role=Role.ASSISTANT, content=None, tool_calls=[tc])
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].name == "search_flights"


def test_tool_result():
    tr = ToolResult(
        tool_call_id="tc_1",
        status="success",
        data={"flights": []},
        metadata={"source": "amadeus", "latency_ms": 123},
    )
    assert tr.status == "success"
    assert tr.metadata["source"] == "amadeus"


def test_tool_result_error():
    tr = ToolResult(
        tool_call_id="tc_1",
        status="error",
        error="API 超时",
        error_code="TIMEOUT",
        suggestion="请稍后重试",
    )
    assert tr.status == "error"
    assert tr.error_code == "TIMEOUT"


def test_message_to_dict():
    msg = Message(role=Role.USER, content="hello")
    d = msg.to_dict()
    assert d["role"] == "user"
    assert d["content"] == "hello"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.types'`

- [ ] **Step 3: 实现 agent/types.py**

```python
# backend/agent/types.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    tool_call_id: str
    status: str  # "success" | "error"
    data: Any = None
    metadata: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    suggestion: str | None = None


@dataclass
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_result: ToolResult | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role.value}
        if self.content is not None:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in self.tool_calls
            ]
        if self.tool_result:
            d["tool_result"] = {
                "tool_call_id": self.tool_result.tool_call_id,
                "status": self.tool_result.status,
                "data": self.tool_result.data,
            }
        return d
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_types.py -v`
Expected: 5 passed

- [ ] **Step 5: 写 llm/types.py 的失败测试**

```python
# backend/tests/test_llm_types.py
from llm.types import LLMChunk, ChunkType


def test_text_delta_chunk():
    chunk = LLMChunk(type=ChunkType.TEXT_DELTA, content="Hello")
    assert chunk.type == ChunkType.TEXT_DELTA
    assert chunk.content == "Hello"
    assert chunk.tool_call is None


def test_tool_call_start_chunk():
    from agent.types import ToolCall
    tc = ToolCall(id="tc_1", name="search_flights", arguments={})
    chunk = LLMChunk(type=ChunkType.TOOL_CALL_START, tool_call=tc)
    assert chunk.tool_call.name == "search_flights"


def test_done_chunk():
    chunk = LLMChunk(type=ChunkType.DONE)
    assert chunk.content is None
    assert chunk.tool_call is None
```

- [ ] **Step 6: 实现 llm/types.py**

```python
# backend/llm/types.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent.types import ToolCall


class ChunkType(str, Enum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    DONE = "done"


@dataclass
class LLMChunk:
    type: ChunkType
    content: str | None = None
    tool_call: ToolCall | None = None
```

- [ ] **Step 7: 运行全部类型测试**

Run: `cd backend && python -m pytest tests/test_types.py tests/test_llm_types.py -v`
Expected: 8 passed

- [ ] **Step 8: 提交**

```bash
git add backend/agent/types.py backend/llm/types.py backend/tests/test_types.py backend/tests/test_llm_types.py
git commit -m "feat: core types — Message, ToolCall, ToolResult, LLMChunk"
```

---

### Task 3: LLM 抽象层

**Files:**
- Create: `backend/llm/base.py`
- Create: `backend/llm/openai_provider.py`
- Create: `backend/llm/anthropic_provider.py`
- Create: `backend/llm/factory.py`
- Create: `backend/tests/test_openai_provider.py`
- Create: `backend/tests/test_anthropic_provider.py`

- [ ] **Step 1: 写 LLMProvider 基类和 OpenAI provider 的失败测试**

```python
# backend/tests/test_openai_provider.py
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.types import Message, Role, ToolCall
from llm.types import LLMChunk, ChunkType
from llm.openai_provider import OpenAIProvider


@pytest.fixture
def provider():
    return OpenAIProvider(model="gpt-4o", temperature=0.7, max_tokens=4096)


def test_convert_messages_splits_system(provider):
    messages = [
        Message(role=Role.SYSTEM, content="You are helpful"),
        Message(role=Role.USER, content="Hello"),
    ]
    converted = provider._convert_messages(messages)
    assert converted[0]["role"] == "system"
    assert converted[0]["content"] == "You are helpful"
    assert converted[1]["role"] == "user"


def test_convert_tool_result_message(provider):
    from agent.types import ToolResult
    msg = Message(
        role=Role.TOOL,
        tool_result=ToolResult(tool_call_id="tc_1", status="success", data={"flights": []}),
    )
    converted = provider._convert_messages([msg])
    assert converted[0]["role"] == "tool"
    assert converted[0]["tool_call_id"] == "tc_1"


def test_convert_tools(provider):
    tool_defs = [
        {
            "name": "search_flights",
            "description": "Search flights",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "IATA code"},
                },
                "required": ["origin"],
            },
        }
    ]
    converted = provider._convert_tools(tool_defs)
    assert converted[0]["type"] == "function"
    assert converted[0]["function"]["name"] == "search_flights"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_openai_provider.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 llm/base.py**

```python
# backend/llm/base.py
from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from agent.types import Message
from llm.types import LLMChunk


@runtime_checkable
class LLMProvider(Protocol):
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[LLMChunk]: ...

    async def count_tokens(self, messages: list[Message]) -> int: ...
```

- [ ] **Step 4: 实现 llm/openai_provider.py**

```python
# backend/llm/openai_provider.py
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import tiktoken
from openai import AsyncOpenAI

from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk


class OpenAIProvider:
    def __init__(self, model: str = "gpt-4o", temperature: float = 0.7, max_tokens: int = 4096):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = AsyncOpenAI()

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == Role.TOOL and msg.tool_result:
                result.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_result.tool_call_id,
                    "content": json.dumps(
                        {"status": msg.tool_result.status, "data": msg.tool_result.data},
                        ensure_ascii=False,
                    ),
                })
            elif msg.role == Role.ASSISTANT and msg.tool_calls:
                result.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                })
            else:
                result.append({
                    "role": msg.role.value,
                    "content": msg.content or "",
                })
        return result

    def _convert_tools(self, tool_defs: list[dict]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tool_defs
        ]

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[LLMChunk]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        if not stream:
            response = await self.client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            if choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    yield LLMChunk(
                        type=ChunkType.TOOL_CALL_START,
                        tool_call=ToolCall(
                            id=tc.id,
                            name=tc.function.name,
                            arguments=json.loads(tc.function.arguments),
                        ),
                    )
            elif choice.message.content:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content=choice.message.content)
            yield LLMChunk(type=ChunkType.DONE)
            return

        response = await self.client.chat.completions.create(**kwargs)
        current_tool_calls: dict[int, dict] = {}

        async for chunk in response:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            if delta.content:
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content=delta.content)

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in current_tool_calls:
                        current_tool_calls[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    entry = current_tool_calls[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments

            if chunk.choices[0].finish_reason:
                for entry in current_tool_calls.values():
                    yield LLMChunk(
                        type=ChunkType.TOOL_CALL_START,
                        tool_call=ToolCall(
                            id=entry["id"],
                            name=entry["name"],
                            arguments=json.loads(entry["arguments"]) if entry["arguments"] else {},
                        ),
                    )
                yield LLMChunk(type=ChunkType.DONE)
                return

    async def count_tokens(self, messages: list[Message]) -> int:
        try:
            enc = tiktoken.encoding_for_model(self.model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        total = 0
        for msg in messages:
            total += 4  # message overhead
            if msg.content:
                total += len(enc.encode(msg.content))
        return total
```

- [ ] **Step 5: 运行 OpenAI provider 测试**

Run: `cd backend && python -m pytest tests/test_openai_provider.py -v`
Expected: 3 passed

- [ ] **Step 6: 写 Anthropic provider 的失败测试**

```python
# backend/tests/test_anthropic_provider.py
import pytest

from agent.types import Message, Role, ToolResult
from llm.anthropic_provider import AnthropicProvider


@pytest.fixture
def provider():
    return AnthropicProvider(model="claude-sonnet-4-20250514", temperature=0.7, max_tokens=4096)


def test_split_system(provider):
    messages = [
        Message(role=Role.SYSTEM, content="You are helpful"),
        Message(role=Role.USER, content="Hello"),
    ]
    system, converted = provider._split_system_and_convert(messages)
    assert system == "You are helpful"
    assert len(converted) == 1
    assert converted[0]["role"] == "user"


def test_convert_tool_result(provider):
    messages = [
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(tool_call_id="tc_1", status="success", data={"result": 1}),
        )
    ]
    _, converted = provider._split_system_and_convert(messages)
    assert converted[0]["role"] == "user"
    assert converted[0]["content"][0]["type"] == "tool_result"
    assert converted[0]["content"][0]["tool_use_id"] == "tc_1"


def test_convert_tools(provider):
    tool_defs = [
        {
            "name": "search_flights",
            "description": "Search flights",
            "parameters": {
                "type": "object",
                "properties": {"origin": {"type": "string"}},
                "required": ["origin"],
            },
        }
    ]
    converted = provider._convert_tools(tool_defs)
    assert converted[0]["name"] == "search_flights"
    assert converted[0]["input_schema"]["type"] == "object"
```

- [ ] **Step 7: 实现 llm/anthropic_provider.py**

```python
# backend/llm/anthropic_provider.py
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk


class AnthropicProvider:
    def __init__(self, model: str = "claude-sonnet-4-20250514", temperature: float = 0.7, max_tokens: int = 4096):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = AsyncAnthropic()

    def _split_system_and_convert(
        self, messages: list[Message]
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == Role.SYSTEM:
                system_parts.append(msg.content or "")
            elif msg.role == Role.TOOL and msg.tool_result:
                converted.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_result.tool_call_id,
                            "content": json.dumps(
                                {"status": msg.tool_result.status, "data": msg.tool_result.data},
                                ensure_ascii=False,
                            ),
                        }
                    ],
                })
            elif msg.role == Role.ASSISTANT and msg.tool_calls:
                content: list[dict] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })
                converted.append({"role": "assistant", "content": content})
            else:
                converted.append({
                    "role": msg.role.value,
                    "content": msg.content or "",
                })

        return "\n\n".join(system_parts), converted

    def _convert_tools(self, tool_defs: list[dict]) -> list[dict[str, Any]]:
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tool_defs
        ]

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[LLMChunk]:
        system, converted = self._split_system_and_convert(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "system": system,
            "messages": converted,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        if not stream:
            response = await self.client.messages.create(**kwargs)
            for block in response.content:
                if block.type == "text":
                    yield LLMChunk(type=ChunkType.TEXT_DELTA, content=block.text)
                elif block.type == "tool_use":
                    yield LLMChunk(
                        type=ChunkType.TOOL_CALL_START,
                        tool_call=ToolCall(id=block.id, name=block.name, arguments=block.input),
                    )
            yield LLMChunk(type=ChunkType.DONE)
            return

        async with self.client.messages.stream(**kwargs) as stream_resp:
            current_tool_id: str | None = None
            current_tool_name: str | None = None
            current_tool_json: str = ""

            async for event in stream_resp:
                if event.type == "content_block_start":
                    if hasattr(event.content_block, "type"):
                        if event.content_block.type == "tool_use":
                            current_tool_id = event.content_block.id
                            current_tool_name = event.content_block.name
                            current_tool_json = ""
                elif event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        yield LLMChunk(type=ChunkType.TEXT_DELTA, content=event.delta.text)
                    elif hasattr(event.delta, "partial_json"):
                        current_tool_json += event.delta.partial_json
                elif event.type == "content_block_stop":
                    if current_tool_id and current_tool_name:
                        yield LLMChunk(
                            type=ChunkType.TOOL_CALL_START,
                            tool_call=ToolCall(
                                id=current_tool_id,
                                name=current_tool_name,
                                arguments=json.loads(current_tool_json) if current_tool_json else {},
                            ),
                        )
                        current_tool_id = None
                        current_tool_name = None
                elif event.type == "message_stop":
                    yield LLMChunk(type=ChunkType.DONE)

    async def count_tokens(self, messages: list[Message]) -> int:
        total = 0
        for msg in messages:
            if msg.content:
                total += len(msg.content) // 3  # rough estimate for Claude
        return total
```

- [ ] **Step 8: 实现 llm/factory.py**

```python
# backend/llm/factory.py
from __future__ import annotations

from config import LLMConfig
from llm.anthropic_provider import AnthropicProvider
from llm.openai_provider import OpenAIProvider


def create_llm_provider(config: LLMConfig) -> OpenAIProvider | AnthropicProvider:
    if config.provider == "anthropic":
        return AnthropicProvider(
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    return OpenAIProvider(
        model=config.model,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
```

- [ ] **Step 9: 运行全部 LLM 测试**

Run: `cd backend && python -m pytest tests/test_openai_provider.py tests/test_anthropic_provider.py -v`
Expected: 6 passed

- [ ] **Step 10: 提交**

```bash
git add backend/llm/ backend/tests/test_openai_provider.py backend/tests/test_anthropic_provider.py backend/tests/test_llm_types.py
git commit -m "feat: LLM abstraction — OpenAI + Anthropic providers with unified types"
```

---

### Task 4: 状态数据模型与 StateManager

**Files:**
- Create: `backend/state/models.py`
- Create: `backend/state/manager.py`
- Create: `backend/tests/test_state_models.py`
- Create: `backend/tests/test_state_manager.py`

- [ ] **Step 1: 写 state/models.py 的失败测试**

```python
# backend/tests/test_state_models.py
from state.models import (
    TravelPlanState,
    DateRange,
    Travelers,
    Budget,
    Accommodation,
    DayPlan,
    Activity,
    Constraint,
    Preference,
    BacktrackEvent,
    Location,
)


def test_create_empty_plan():
    plan = TravelPlanState(session_id="sess_001")
    assert plan.phase == 1
    assert plan.destination is None
    assert plan.daily_plans == []
    assert plan.backtrack_history == []
    assert plan.version == 1


def test_date_range():
    dr = DateRange(start="2026-04-10", end="2026-04-15")
    assert dr.total_days == 5


def test_activity():
    loc = Location(lat=35.0116, lng=135.7681, name="金阁寺")
    act = Activity(
        name="金阁寺",
        location=loc,
        start_time="09:00",
        end_time="10:30",
        category="景点",
    )
    assert act.duration_minutes == 90


def test_plan_serialization():
    plan = TravelPlanState(session_id="sess_001", destination="Kyoto")
    d = plan.to_dict()
    assert d["session_id"] == "sess_001"
    assert d["destination"] == "Kyoto"

    restored = TravelPlanState.from_dict(d)
    assert restored.session_id == "sess_001"
    assert restored.destination == "Kyoto"


def test_plan_clear_downstream_from_phase_3():
    plan = TravelPlanState(
        session_id="sess_001",
        phase=5,
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        accommodation=Accommodation(area="祇园", hotel="Hotel Gion"),
        daily_plans=[DayPlan(day=1, date="2026-04-10", activities=[])],
        constraints=[Constraint(type="hard", description="预算 1 万")],
    )
    plan.clear_downstream(from_phase=3)
    # 阶段 3 以后的产出应被清除
    assert plan.accommodation is None
    assert plan.daily_plans == []
    # 阶段 3 及之前的产出应保留
    assert plan.destination == "Kyoto"
    # 约束始终保留
    assert len(plan.constraints) == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_state_models.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 state/models.py**

```python
# backend/state/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Location:
    lat: float
    lng: float
    name: str = ""

    def to_dict(self) -> dict:
        return {"lat": self.lat, "lng": self.lng, "name": self.name}

    @classmethod
    def from_dict(cls, d: dict) -> Location:
        return cls(lat=d["lat"], lng=d["lng"], name=d.get("name", ""))


@dataclass
class DateRange:
    start: str  # YYYY-MM-DD
    end: str

    @property
    def total_days(self) -> int:
        from datetime import date as dt_date
        s = dt_date.fromisoformat(self.start)
        e = dt_date.fromisoformat(self.end)
        return (e - s).days

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, d: dict) -> DateRange:
        return cls(start=d["start"], end=d["end"])


@dataclass
class Travelers:
    adults: int = 1
    children: int = 0

    def to_dict(self) -> dict:
        return {"adults": self.adults, "children": self.children}

    @classmethod
    def from_dict(cls, d: dict) -> Travelers:
        return cls(adults=d.get("adults", 1), children=d.get("children", 0))


@dataclass
class Budget:
    total: float
    currency: str = "CNY"

    def to_dict(self) -> dict:
        return {"total": self.total, "currency": self.currency}

    @classmethod
    def from_dict(cls, d: dict) -> Budget:
        return cls(total=d["total"], currency=d.get("currency", "CNY"))


@dataclass
class Accommodation:
    area: str
    hotel: str | None = None

    def to_dict(self) -> dict:
        return {"area": self.area, "hotel": self.hotel}

    @classmethod
    def from_dict(cls, d: dict) -> Accommodation:
        return cls(area=d["area"], hotel=d.get("hotel"))


@dataclass
class Activity:
    name: str
    location: Location
    start_time: str  # "HH:MM"
    end_time: str
    category: str
    cost: float = 0
    transport_from_prev: str | None = None
    transport_duration_min: int = 0
    notes: str = ""

    @property
    def duration_minutes(self) -> int:
        sh, sm = map(int, self.start_time.split(":"))
        eh, em = map(int, self.end_time.split(":"))
        return (eh * 60 + em) - (sh * 60 + sm)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "location": self.location.to_dict(),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "category": self.category,
            "cost": self.cost,
            "transport_from_prev": self.transport_from_prev,
            "transport_duration_min": self.transport_duration_min,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Activity:
        return cls(
            name=d["name"],
            location=Location.from_dict(d["location"]),
            start_time=d["start_time"],
            end_time=d["end_time"],
            category=d["category"],
            cost=d.get("cost", 0),
            transport_from_prev=d.get("transport_from_prev"),
            transport_duration_min=d.get("transport_duration_min", 0),
            notes=d.get("notes", ""),
        )


@dataclass
class DayPlan:
    day: int
    date: str
    activities: list[Activity] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "date": self.date,
            "activities": [a.to_dict() for a in self.activities],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DayPlan:
        return cls(
            day=d["day"],
            date=d["date"],
            activities=[Activity.from_dict(a) for a in d.get("activities", [])],
            notes=d.get("notes", ""),
        )


@dataclass
class Constraint:
    type: str  # "hard" | "soft"
    description: str

    def to_dict(self) -> dict:
        return {"type": self.type, "description": self.description}

    @classmethod
    def from_dict(cls, d: dict) -> Constraint:
        return cls(type=d["type"], description=d["description"])


@dataclass
class Preference:
    key: str
    value: str

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value}

    @classmethod
    def from_dict(cls, d: dict) -> Preference:
        return cls(key=d["key"], value=d["value"])


@dataclass
class BacktrackEvent:
    from_phase: int
    to_phase: int
    reason: str
    snapshot_path: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "from_phase": self.from_phase,
            "to_phase": self.to_phase,
            "reason": self.reason,
            "snapshot_path": self.snapshot_path,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> BacktrackEvent:
        return cls(
            from_phase=d["from_phase"],
            to_phase=d["to_phase"],
            reason=d["reason"],
            snapshot_path=d["snapshot_path"],
            timestamp=d.get("timestamp", ""),
        )


# Phase → which fields are downstream products (cleared on backtrack)
_PHASE_DOWNSTREAM: dict[int, list[str]] = {
    3: ["accommodation", "daily_plans"],
    4: ["daily_plans"],
}


@dataclass
class TravelPlanState:
    session_id: str
    phase: int = 1
    destination: str | None = None
    destination_candidates: list[dict] = field(default_factory=list)
    dates: DateRange | None = None
    travelers: Travelers | None = None
    budget: Budget | None = None
    accommodation: Accommodation | None = None
    daily_plans: list[DayPlan] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    preferences: list[Preference] = field(default_factory=list)
    backtrack_history: list[BacktrackEvent] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())
    version: int = 1

    def clear_downstream(self, from_phase: int) -> None:
        """Clear all output produced after from_phase. Keep constraints and preferences."""
        for phase in sorted(_PHASE_DOWNSTREAM):
            if phase >= from_phase:
                for attr in _PHASE_DOWNSTREAM[phase]:
                    default = [] if isinstance(getattr(self, attr), list) else None
                    setattr(self, attr, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "phase": self.phase,
            "destination": self.destination,
            "destination_candidates": self.destination_candidates,
            "dates": self.dates.to_dict() if self.dates else None,
            "travelers": self.travelers.to_dict() if self.travelers else None,
            "budget": self.budget.to_dict() if self.budget else None,
            "accommodation": self.accommodation.to_dict() if self.accommodation else None,
            "daily_plans": [dp.to_dict() for dp in self.daily_plans],
            "constraints": [c.to_dict() for c in self.constraints],
            "preferences": [p.to_dict() for p in self.preferences],
            "backtrack_history": [b.to_dict() for b in self.backtrack_history],
            "created_at": self.created_at,
            "last_updated": self.last_updated,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TravelPlanState:
        return cls(
            session_id=d["session_id"],
            phase=d.get("phase", 1),
            destination=d.get("destination"),
            destination_candidates=d.get("destination_candidates", []),
            dates=DateRange.from_dict(d["dates"]) if d.get("dates") else None,
            travelers=Travelers.from_dict(d["travelers"]) if d.get("travelers") else None,
            budget=Budget.from_dict(d["budget"]) if d.get("budget") else None,
            accommodation=Accommodation.from_dict(d["accommodation"]) if d.get("accommodation") else None,
            daily_plans=[DayPlan.from_dict(dp) for dp in d.get("daily_plans", [])],
            constraints=[Constraint.from_dict(c) for c in d.get("constraints", [])],
            preferences=[Preference.from_dict(p) for p in d.get("preferences", [])],
            backtrack_history=[BacktrackEvent.from_dict(b) for b in d.get("backtrack_history", [])],
            created_at=d.get("created_at", ""),
            last_updated=d.get("last_updated", ""),
            version=d.get("version", 1),
        )
```

- [ ] **Step 4: 运行模型测试**

Run: `cd backend && python -m pytest tests/test_state_models.py -v`
Expected: 5 passed

- [ ] **Step 5: 写 StateManager 的失败测试**

```python
# backend/tests/test_state_manager.py
import json
from pathlib import Path

import pytest

from state.manager import StateManager
from state.models import TravelPlanState, DateRange, Accommodation, DayPlan


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


@pytest.fixture
def manager(data_dir):
    return StateManager(data_dir=str(data_dir))


@pytest.mark.asyncio
async def test_create_session(manager):
    plan = await manager.create_session()
    assert plan.session_id
    assert plan.phase == 1


@pytest.mark.asyncio
async def test_save_and_load(manager):
    plan = await manager.create_session()
    plan.destination = "Kyoto"
    await manager.save(plan)

    loaded = await manager.load(plan.session_id)
    assert loaded.destination == "Kyoto"


@pytest.mark.asyncio
async def test_save_increments_version(manager):
    plan = await manager.create_session()
    assert plan.version == 1
    await manager.save(plan)
    assert plan.version == 2


@pytest.mark.asyncio
async def test_save_snapshot(manager):
    plan = await manager.create_session()
    plan.destination = "Tokyo"
    await manager.save(plan)

    snapshot_path = await manager.save_snapshot(plan)
    assert Path(snapshot_path).exists()

    snapshot_data = json.loads(Path(snapshot_path).read_text())
    assert snapshot_data["destination"] == "Tokyo"


@pytest.mark.asyncio
async def test_load_nonexistent_raises(manager):
    with pytest.raises(FileNotFoundError):
        await manager.load("nonexistent")


@pytest.mark.asyncio
async def test_save_tool_result(manager):
    plan = await manager.create_session()
    data = {"flights": [{"airline": "MU", "price": 2340}]}
    path = await manager.save_tool_result(plan.session_id, "flight-search", data)
    assert Path(path).exists()
    assert json.loads(Path(path).read_text()) == data
```

- [ ] **Step 6: 实现 state/manager.py**

```python
# backend/state/manager.py
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from state.models import TravelPlanState


class StateManager:
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)

    def _session_dir(self, session_id: str) -> Path:
        return self.data_dir / "sessions" / session_id

    async def create_session(self) -> TravelPlanState:
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        plan = TravelPlanState(session_id=session_id)
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "snapshots").mkdir(exist_ok=True)
        (session_dir / "tool_results").mkdir(exist_ok=True)
        await self.save(plan)
        return plan

    async def save(self, plan: TravelPlanState) -> None:
        from datetime import datetime
        plan.last_updated = datetime.now().isoformat()
        plan.version += 1
        path = self._session_dir(plan.session_id) / "plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))

    async def load(self, session_id: str) -> TravelPlanState:
        path = self._session_dir(session_id) / "plan.json"
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")
        data = json.loads(path.read_text())
        return TravelPlanState.from_dict(data)

    async def save_snapshot(self, plan: TravelPlanState) -> str:
        snapshot_dir = self._session_dir(plan.session_id) / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = snapshot_dir / f"{int(time.time())}.json"
        path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return str(path)

    async def save_tool_result(self, session_id: str, tool_name: str, data: dict) -> str:
        results_dir = self._session_dir(session_id) / "tool_results"
        results_dir.mkdir(parents=True, exist_ok=True)
        path = results_dir / f"{tool_name}-{int(time.time())}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return str(path)
```

- [ ] **Step 7: 运行 StateManager 测试**

Run: `cd backend && python -m pytest tests/test_state_manager.py -v`
Expected: 6 passed

- [ ] **Step 8: 提交**

```bash
git add backend/state/ backend/tests/test_state_models.py backend/tests/test_state_manager.py
git commit -m "feat: state models and StateManager with snapshot support"
```

---

### Task 5: Tool Engine 框架

**Files:**
- Create: `backend/tools/base.py`
- Create: `backend/tools/engine.py`
- Create: `backend/tests/test_tool_base.py`
- Create: `backend/tests/test_tool_engine.py`

- [ ] **Step 1: 写 tool base 的失败测试**

```python
# backend/tests/test_tool_base.py
import pytest

from tools.base import ToolDef, ToolError, tool


def test_tool_decorator_registers():
    @tool(
        name="my_tool",
        description="A test tool",
        phases=[1, 2],
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    async def my_tool(query: str) -> dict:
        return {"result": query}

    assert isinstance(my_tool, ToolDef)
    assert my_tool.name == "my_tool"
    assert my_tool.phases == [1, 2]


@pytest.mark.asyncio
async def test_tool_def_call():
    @tool(
        name="echo",
        description="Echo input",
        phases=[1],
        parameters={"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
    )
    async def echo(msg: str) -> dict:
        return {"echo": msg}

    result = await echo(msg="hello")
    assert result == {"echo": "hello"}


def test_tool_error():
    err = ToolError("Bad input", error_code="INVALID", suggestion="Fix the input")
    assert err.error_code == "INVALID"
    assert err.suggestion == "Fix the input"
    assert str(err) == "Bad input"


def test_tool_to_schema():
    @tool(
        name="search",
        description="Search things",
        phases=[2],
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string", "description": "query"}},
            "required": ["q"],
        },
    )
    async def search(q: str) -> dict:
        return {}

    schema = search.to_schema()
    assert schema["name"] == "search"
    assert schema["description"] == "Search things"
    assert schema["parameters"]["properties"]["q"]["type"] == "string"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_tool_base.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 tools/base.py**

```python
# backend/tools/base.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


class ToolError(Exception):
    def __init__(self, message: str, error_code: str = "UNKNOWN", suggestion: str = ""):
        super().__init__(message)
        self.error_code = error_code
        self.suggestion = suggestion


@dataclass
class ToolDef:
    name: str
    description: str
    phases: list[int]
    parameters: dict[str, Any]
    _fn: Callable[..., Coroutine[Any, Any, Any]] = field(repr=False)

    async def __call__(self, **kwargs: Any) -> Any:
        return await self._fn(**kwargs)

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def tool(
    name: str,
    description: str,
    phases: list[int],
    parameters: dict[str, Any],
) -> Callable:
    def decorator(fn: Callable) -> ToolDef:
        return ToolDef(
            name=name,
            description=description,
            phases=phases,
            parameters=parameters,
            _fn=fn,
        )
    return decorator
```

- [ ] **Step 4: 运行 tool base 测试**

Run: `cd backend && python -m pytest tests/test_tool_base.py -v`
Expected: 4 passed

- [ ] **Step 5: 写 ToolEngine 的失败测试**

```python
# backend/tests/test_tool_engine.py
import pytest

from agent.types import ToolCall, ToolResult
from tools.base import ToolDef, ToolError, tool
from tools.engine import ToolEngine


@pytest.fixture
def engine():
    @tool(
        name="greet",
        description="Greet someone",
        phases=[1, 2],
        parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    async def greet(name: str) -> dict:
        return {"greeting": f"Hello, {name}!"}

    @tool(
        name="fail_tool",
        description="Always fails",
        phases=[1],
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def fail_tool() -> dict:
        raise ToolError("Something broke", error_code="BROKEN", suggestion="Try again")

    eng = ToolEngine()
    eng.register(greet)
    eng.register(fail_tool)
    return eng


def test_get_tools_for_phase(engine):
    phase1_tools = engine.get_tools_for_phase(1)
    assert len(phase1_tools) == 2
    phase2_tools = engine.get_tools_for_phase(2)
    assert len(phase2_tools) == 1
    assert phase2_tools[0]["name"] == "greet"


@pytest.mark.asyncio
async def test_execute_success(engine):
    call = ToolCall(id="tc_1", name="greet", arguments={"name": "World"})
    result = await engine.execute(call)
    assert result.status == "success"
    assert result.data["greeting"] == "Hello, World!"
    assert result.tool_call_id == "tc_1"


@pytest.mark.asyncio
async def test_execute_tool_error(engine):
    call = ToolCall(id="tc_2", name="fail_tool", arguments={})
    result = await engine.execute(call)
    assert result.status == "error"
    assert result.error_code == "BROKEN"
    assert result.suggestion == "Try again"


@pytest.mark.asyncio
async def test_execute_unknown_tool(engine):
    call = ToolCall(id="tc_3", name="nonexistent", arguments={})
    result = await engine.execute(call)
    assert result.status == "error"
    assert result.error_code == "UNKNOWN_TOOL"
```

- [ ] **Step 6: 实现 tools/engine.py**

```python
# backend/tools/engine.py
from __future__ import annotations

from typing import Any

from agent.types import ToolCall, ToolResult
from tools.base import ToolDef, ToolError


class ToolEngine:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool_def: ToolDef) -> None:
        self._tools[tool_def.name] = tool_def

    def get_tools_for_phase(self, phase: int) -> list[dict[str, Any]]:
        return [
            t.to_schema()
            for t in self._tools.values()
            if phase in t.phases
        ]

    def get_tool(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    async def execute(self, call: ToolCall) -> ToolResult:
        tool_def = self._tools.get(call.name)
        if not tool_def:
            return ToolResult(
                tool_call_id=call.id,
                status="error",
                error=f"Unknown tool: {call.name}",
                error_code="UNKNOWN_TOOL",
                suggestion=f"Available tools: {', '.join(self._tools.keys())}",
            )

        try:
            data = await tool_def(**call.arguments)
            return ToolResult(
                tool_call_id=call.id,
                status="success",
                data=data,
            )
        except ToolError as e:
            return ToolResult(
                tool_call_id=call.id,
                status="error",
                error=str(e),
                error_code=e.error_code,
                suggestion=e.suggestion,
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                status="error",
                error=str(e),
                error_code="INTERNAL_ERROR",
                suggestion="An unexpected error occurred",
            )
```

- [ ] **Step 7: 运行 ToolEngine 测试**

Run: `cd backend && python -m pytest tests/test_tool_engine.py -v`
Expected: 4 passed

- [ ] **Step 8: 提交**

```bash
git add backend/tools/base.py backend/tools/engine.py backend/tests/test_tool_base.py backend/tests/test_tool_engine.py
git commit -m "feat: tool engine — registration, dispatch, structured error handling"
```

---

### Task 6: Phase Router

**Files:**
- Create: `backend/phase/prompts.py`
- Create: `backend/phase/router.py`
- Create: `backend/tests/test_phase_router.py`

- [ ] **Step 1: 写 PhaseRouter 的失败测试**

```python
# backend/tests/test_phase_router.py
import pytest

from phase.router import PhaseRouter
from state.models import (
    TravelPlanState,
    DateRange,
    Accommodation,
    DayPlan,
    Preference,
)


@pytest.fixture
def router():
    return PhaseRouter()


def test_infer_phase_empty(router):
    plan = TravelPlanState(session_id="s1")
    assert router.infer_phase(plan) == 1


def test_infer_phase_has_preferences_no_destination(router):
    plan = TravelPlanState(
        session_id="s1",
        preferences=[Preference(key="style", value="relaxed")],
    )
    assert router.infer_phase(plan) == 2


def test_infer_phase_has_destination_no_dates(router):
    plan = TravelPlanState(session_id="s1", destination="Kyoto")
    assert router.infer_phase(plan) == 3


def test_infer_phase_has_dates_no_accommodation(router):
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
    )
    assert router.infer_phase(plan) == 4


def test_infer_phase_has_accommodation_no_plans(router):
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        accommodation=Accommodation(area="祇園"),
    )
    assert router.infer_phase(plan) == 5


def test_infer_phase_plans_complete(router):
    plan = TravelPlanState(
        session_id="s1",
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        accommodation=Accommodation(area="祇園"),
        daily_plans=[
            DayPlan(day=i, date=f"2026-04-{10+i}") for i in range(5)
        ],
    )
    assert router.infer_phase(plan) == 7


def test_get_prompt_for_phase(router):
    prompt = router.get_prompt(1)
    assert "旅行灵感顾问" in prompt


def test_get_prompt_for_all_phases(router):
    for phase in [1, 2, 3, 4, 5, 7]:
        prompt = router.get_prompt(phase)
        assert len(prompt) > 50


def test_get_tool_names_phase_1(router):
    names = router.get_tool_names(1)
    assert names == ["update_plan_state"]


def test_get_tool_names_phase_5(router):
    names = router.get_tool_names(5)
    assert "get_poi_info" in names
    assert "assemble_day_plan" in names
    assert "update_plan_state" in names


def test_check_transition_no_change(router):
    plan = TravelPlanState(session_id="s1", phase=1)
    changed = router.check_and_apply_transition(plan)
    assert not changed
    assert plan.phase == 1


def test_check_transition_phase_advance(router):
    plan = TravelPlanState(session_id="s1", phase=1, destination="Kyoto")
    changed = router.check_and_apply_transition(plan)
    assert changed
    assert plan.phase == 3  # destination present, no preferences → skip 2


def test_prepare_backtrack(router, tmp_path):
    plan = TravelPlanState(
        session_id="s1",
        phase=5,
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        accommodation=Accommodation(area="祇園"),
        daily_plans=[DayPlan(day=1, date="2026-04-10")],
    )
    router.prepare_backtrack(plan, to_phase=3, reason="预算超限", snapshot_path="/tmp/snap.json")
    assert plan.phase == 3
    assert plan.accommodation is None
    assert plan.daily_plans == []
    assert plan.destination == "Kyoto"  # preserved
    assert len(plan.backtrack_history) == 1
    assert plan.backtrack_history[0].reason == "预算超限"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_phase_router.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 phase/prompts.py**

```python
# backend/phase/prompts.py

PHASE_PROMPTS: dict[int, str] = {
    1: """你现在是旅行灵感顾问。用户可能只有模糊的想法（"想去海边""想放松"）。
你的任务是通过开放式提问帮用户具象化需求，不要急于给出目的地建议。
关注：出行动机、同行人、时间窗口、预算范围。
一次只问一个问题，保持耐心和热情。""",

    2: """你现在是目的地推荐专家。基于用户的意愿，推荐 2-3 个目的地候选。
每个候选必须附带：季节适宜度、预算估算、签证要求、与用户偏好的匹配度。
最终目的地由用户拍板，你只提供信息和建议，不替用户做决定。
如果用户已经明确了目的地，确认后直接进入下一步。""",

    3: """你现在是行程节奏规划师。目的地已确定，需要确定出行日期和整体节奏。
基于目的地特点和用户偏好（每天景点数、步行耐受度），给出天数建议。
需要确认：具体出发和返回日期、每日可用时间、必去景点列表。
输出为结构化的约束清单。""",

    4: """你现在是住宿区域顾问。根据行程安排推荐住宿区域。
综合考虑：到主要景点的交通便利度、区域安全性、性价比、周边餐饮选择。
推荐 2-3 个区域候选，附带每个区域的优劣分析和推荐住宿类型。""",

    5: """你现在是行程组装引擎。把景点、餐厅、交通组装成按天的具体行程。
每个活动必须有：开始时间、结束时间、地点、交通方式和耗时、预估费用。
必须通过硬约束验证：时间不冲突、交通可达、营业时间内、预算不超限。
每天的行程应有主题感，地理上尽量集中以减少交通时间。
使用 assemble_day_plan 工具来生成优化的单日行程。""",

    7: """你现在是出发前查漏清单生成器。针对已确认的行程，生成完整的出行检查清单。
包含：证件准备、货币兑换、天气对应衣物、已规划项目的注意事项、紧急联系方式、目的地实用贴士。
使用 check_weather_forecast 获取最新天气，使用 generate_trip_summary 生成出行摘要。
逐项检查，确保没有遗漏。""",
}

PHASE_TOOL_NAMES: dict[int, list[str]] = {
    1: ["update_plan_state"],
    2: ["search_destinations", "check_travel_feasibility", "update_plan_state"],
    3: ["search_flights", "update_plan_state"],
    4: ["search_accommodations", "calculate_route", "update_plan_state"],
    5: ["get_poi_info", "calculate_route", "assemble_day_plan", "check_availability", "update_plan_state"],
    7: ["check_weather_forecast", "generate_trip_summary", "update_plan_state"],
}

PHASE_CONTROL_MODE: dict[int, str] = {
    1: "conversational",
    2: "agent_with_guard",
    3: "workflow",
    4: "conversational",
    5: "structured",
    7: "evaluator",
}
```

- [ ] **Step 4: 实现 phase/router.py**

```python
# backend/phase/router.py
from __future__ import annotations

from phase.prompts import PHASE_CONTROL_MODE, PHASE_PROMPTS, PHASE_TOOL_NAMES
from state.models import BacktrackEvent, TravelPlanState


class PhaseRouter:
    def infer_phase(self, plan: TravelPlanState) -> int:
        if not plan.destination:
            if plan.preferences:
                return 2
            return 1
        if not plan.dates:
            return 3
        if not plan.accommodation:
            return 4
        if len(plan.daily_plans) < plan.dates.total_days:
            return 5
        return 7

    def get_prompt(self, phase: int) -> str:
        return PHASE_PROMPTS.get(phase, PHASE_PROMPTS[1])

    def get_tool_names(self, phase: int) -> list[str]:
        return PHASE_TOOL_NAMES.get(phase, PHASE_TOOL_NAMES[1])

    def get_control_mode(self, phase: int) -> str:
        return PHASE_CONTROL_MODE.get(phase, "conversational")

    def check_and_apply_transition(self, plan: TravelPlanState) -> bool:
        """Check if plan_state warrants a phase change. Returns True if phase changed."""
        inferred = self.infer_phase(plan)
        if inferred != plan.phase:
            plan.phase = inferred
            return True
        return False

    def prepare_backtrack(
        self,
        plan: TravelPlanState,
        to_phase: int,
        reason: str,
        snapshot_path: str,
    ) -> None:
        """Execute backtrack: record event, clear downstream, switch phase."""
        plan.backtrack_history.append(
            BacktrackEvent(
                from_phase=plan.phase,
                to_phase=to_phase,
                reason=reason,
                snapshot_path=snapshot_path,
            )
        )
        plan.clear_downstream(from_phase=to_phase)
        plan.phase = to_phase
```

- [ ] **Step 5: 运行 PhaseRouter 测试**

Run: `cd backend && python -m pytest tests/test_phase_router.py -v`
Expected: 12 passed

- [ ] **Step 6: 提交**

```bash
git add backend/phase/ backend/tests/test_phase_router.py
git commit -m "feat: phase router — inference, prompt switching, backtrack"
```

---

### Task 7: 钩子系统

**Files:**
- Create: `backend/agent/hooks.py`
- Create: `backend/tests/test_hooks.py`

- [ ] **Step 1: 写 HookManager 的失败测试**

```python
# backend/tests/test_hooks.py
import pytest

from agent.hooks import HookManager


@pytest.fixture
def hooks():
    return HookManager()


@pytest.mark.asyncio
async def test_register_and_run_hook(hooks):
    results = []

    async def my_hook(data):
        results.append(data)

    hooks.register("after_tool_call", my_hook)
    await hooks.run("after_tool_call", "test_data")
    assert results == ["test_data"]


@pytest.mark.asyncio
async def test_multiple_hooks_run_in_order(hooks):
    order = []

    async def hook_a(data):
        order.append("a")

    async def hook_b(data):
        order.append("b")

    hooks.register("event", hook_a)
    hooks.register("event", hook_b)
    await hooks.run("event", None)
    assert order == ["a", "b"]


@pytest.mark.asyncio
async def test_run_nonexistent_event(hooks):
    # Should not raise
    await hooks.run("nonexistent", None)


@pytest.mark.asyncio
async def test_hook_receives_kwargs(hooks):
    captured = {}

    async def my_hook(**kwargs):
        captured.update(kwargs)

    hooks.register("event", my_hook)
    await hooks.run("event", tool_name="search", result={"ok": True})
    assert captured["tool_name"] == "search"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_hooks.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 agent/hooks.py**

```python
# backend/agent/hooks.py
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Coroutine


HookFn = Callable[..., Coroutine[Any, Any, None]]


class HookManager:
    def __init__(self) -> None:
        self._hooks: dict[str, list[HookFn]] = defaultdict(list)

    def register(self, event: str, fn: HookFn) -> None:
        self._hooks[event].append(fn)

    async def run(self, event: str, *args: Any, **kwargs: Any) -> None:
        for fn in self._hooks.get(event, []):
            if args and not kwargs:
                await fn(args[0] if len(args) == 1 else args)
            elif kwargs:
                await fn(**kwargs)
            else:
                await fn()
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && python -m pytest tests/test_hooks.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add backend/agent/hooks.py backend/tests/test_hooks.py
git commit -m "feat: hook manager for decoupled event handling"
```

---

### Task 8: Harness — 硬约束验证器

**Files:**
- Create: `backend/harness/validator.py`
- Create: `backend/tests/test_harness_validator.py`

- [ ] **Step 1: 写硬约束验证器的失败测试**

```python
# backend/tests/test_harness_validator.py
import pytest

from harness.validator import validate_hard_constraints
from state.models import (
    TravelPlanState,
    DateRange,
    Budget,
    DayPlan,
    Activity,
    Location,
    Preference,
)


def _make_activity(name, start, end, lat=35.0, lng=135.7, cost=0):
    return Activity(
        name=name,
        location=Location(lat=lat, lng=lng, name=name),
        start_time=start,
        end_time=end,
        category="景点",
        cost=cost,
        transport_duration_min=0,
    )


def test_no_errors_on_valid_plan():
    plan = TravelPlanState(
        session_id="s1",
        dates=DateRange(start="2026-04-10", end="2026-04-12"),
        budget=Budget(total=10000),
        daily_plans=[
            DayPlan(day=1, date="2026-04-10", activities=[
                _make_activity("金阁寺", "09:00", "10:30", cost=500),
                _make_activity("龙安寺", "11:00", "12:00", cost=500),
            ]),
            DayPlan(day=2, date="2026-04-11", activities=[
                _make_activity("伏见稻荷", "09:00", "11:00"),
            ]),
        ],
    )
    errors = validate_hard_constraints(plan)
    assert errors == []


def test_time_conflict():
    plan = TravelPlanState(
        session_id="s1",
        dates=DateRange(start="2026-04-10", end="2026-04-11"),
        daily_plans=[
            DayPlan(day=1, date="2026-04-10", activities=[
                _make_activity("A", "09:00", "10:30"),
                _make_activity("B", "10:00", "11:00"),  # overlaps with A
            ]),
        ],
    )
    errors = validate_hard_constraints(plan)
    assert len(errors) == 1
    assert "时间冲突" in errors[0] or "A" in errors[0]


def test_budget_exceeded():
    plan = TravelPlanState(
        session_id="s1",
        dates=DateRange(start="2026-04-10", end="2026-04-11"),
        budget=Budget(total=1000),
        daily_plans=[
            DayPlan(day=1, date="2026-04-10", activities=[
                _make_activity("A", "09:00", "10:00", cost=600),
                _make_activity("B", "11:00", "12:00", cost=600),
            ]),
        ],
    )
    errors = validate_hard_constraints(plan)
    assert any("预算" in e for e in errors)


def test_too_many_days():
    plan = TravelPlanState(
        session_id="s1",
        dates=DateRange(start="2026-04-10", end="2026-04-12"),  # 2 days
        daily_plans=[
            DayPlan(day=i, date=f"2026-04-{10+i}") for i in range(3)  # 3 plans
        ],
    )
    errors = validate_hard_constraints(plan)
    assert any("天数" in e for e in errors)


def test_no_errors_on_empty_plan():
    plan = TravelPlanState(session_id="s1")
    errors = validate_hard_constraints(plan)
    assert errors == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_validator.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 harness/validator.py**

```python
# backend/harness/validator.py
from __future__ import annotations

from state.models import TravelPlanState


def _time_to_minutes(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def validate_hard_constraints(plan: TravelPlanState) -> list[str]:
    errors: list[str] = []

    for day in plan.daily_plans:
        acts = day.activities
        for i in range(1, len(acts)):
            prev = acts[i - 1]
            curr = acts[i]
            prev_end = _time_to_minutes(prev.end_time)
            curr_start = _time_to_minutes(curr.start_time)
            travel = curr.transport_duration_min

            if prev_end + travel > curr_start:
                gap = curr_start - prev_end
                errors.append(
                    f"Day {day.day}: {prev.name}→{curr.name} "
                    f"时间冲突（{prev.name} {prev.end_time} 结束，"
                    f"交通需 {travel}min，但 {curr.name} {curr.start_time} 开始，"
                    f"间隔仅 {gap}min）"
                )

    # Budget check
    if plan.budget:
        total_cost = sum(
            act.cost
            for day in plan.daily_plans
            for act in day.activities
        )
        if total_cost > plan.budget.total:
            errors.append(
                f"总费用 ¥{total_cost:.0f} 超出预算 ¥{plan.budget.total:.0f}"
            )

    # Day count check
    if plan.dates and plan.daily_plans:
        allowed_days = plan.dates.total_days
        actual_days = len(plan.daily_plans)
        if actual_days > allowed_days:
            errors.append(
                f"规划了 {actual_days} 天行程，但只有 {allowed_days} 天可用"
            )

    return errors
```

- [ ] **Step 4: 运行验证器测试**

Run: `cd backend && python -m pytest tests/test_harness_validator.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add backend/harness/validator.py backend/tests/test_harness_validator.py
git commit -m "feat: hard constraint validator — time, budget, day count checks"
```

---

### Task 9: Harness — 软约束 LLM Judge

**Files:**
- Create: `backend/harness/judge.py`
- Create: `backend/tests/test_harness_judge.py`

- [ ] **Step 1: 写 LLM Judge 的失败测试**

```python
# backend/tests/test_harness_judge.py
import json
from unittest.mock import AsyncMock

import pytest

from harness.judge import SoftScore, build_judge_prompt, parse_judge_response


def test_build_judge_prompt():
    plan_data = {"daily_plans": [{"day": 1, "activities": []}]}
    user_prefs = {"travel_style": "relaxed", "avg_pois_per_day": 3}
    prompt = build_judge_prompt(plan_data, user_prefs)
    assert "节奏舒适度" in prompt
    assert "地理效率" in prompt
    assert "relaxed" in prompt


def test_parse_valid_response():
    response = json.dumps({
        "pace": 4,
        "geography": 3,
        "coherence": 5,
        "personalization": 4,
        "suggestions": ["可以考虑调整第二天的节奏"],
    })
    score = parse_judge_response(response)
    assert score.pace == 4
    assert score.geography == 3
    assert score.overall == 4.0  # average
    assert len(score.suggestions) == 1


def test_parse_invalid_response_returns_default():
    score = parse_judge_response("not json at all")
    assert score.pace == 3
    assert score.overall == 3.0
    assert "评估解析失败" in score.suggestions[0]


def test_soft_score_overall():
    score = SoftScore(pace=5, geography=4, coherence=3, personalization=2, suggestions=[])
    assert score.overall == 3.5
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_judge.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 harness/judge.py**

```python
# backend/harness/judge.py
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SoftScore:
    pace: int = 3
    geography: int = 3
    coherence: int = 3
    personalization: int = 3
    suggestions: list[str] = field(default_factory=list)

    @property
    def overall(self) -> float:
        return (self.pace + self.geography + self.coherence + self.personalization) / 4


def build_judge_prompt(plan_data: dict[str, Any], user_prefs: dict[str, Any]) -> str:
    return f"""评估以下旅行行程的质量，每项 1-5 分。

行程数据：
{json.dumps(plan_data, ensure_ascii=False, indent=2)}

用户偏好：
{json.dumps(user_prefs, ensure_ascii=False, indent=2)}

评分维度：
1. 节奏舒适度（pace）：每天活动量是否均衡？有没有过紧或过松的天？
2. 地理效率（geography）：同一天的景点是否地理集中？有没有不必要的来回跑？
3. 体验连贯性（coherence）：每天的主题感是否清晰？过渡是否自然？
4. 个性化程度（personalization）：是否体现了用户的偏好？

严格输出 JSON：
{{"pace": N, "geography": N, "coherence": N, "personalization": N, "suggestions": ["建议1", "建议2"]}}"""


def parse_judge_response(response: str) -> SoftScore:
    try:
        # Handle cases where LLM wraps JSON in markdown code blocks
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        return SoftScore(
            pace=int(data.get("pace", 3)),
            geography=int(data.get("geography", 3)),
            coherence=int(data.get("coherence", 3)),
            personalization=int(data.get("personalization", 3)),
            suggestions=data.get("suggestions", []),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return SoftScore(suggestions=["评估解析失败，使用默认评分"])
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && python -m pytest tests/test_harness_judge.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add backend/harness/judge.py backend/tests/test_harness_judge.py
git commit -m "feat: soft constraint LLM judge — prompt builder and response parser"
```

---

### Task 10: Context Manager

**Files:**
- Create: `backend/context/soul.md`
- Create: `backend/context/manager.py`
- Create: `backend/tests/test_context_manager.py`

- [ ] **Step 1: 创建 SOUL.md**

```markdown
# SOUL.md — 旅行规划 Agent 身份

## 身份
你是一个专业的旅行规划 Agent，帮助用户完成从模糊意愿到出发前查漏的全流程规划。

## 核心行为约束
- 不替用户做情感决策（目的地最终由用户拍板）
- 所有涉及支付的操作必须用户确认
- 行程建议必须附带时间/距离/成本的量化数据
- 回溯时说明原因和影响范围，不要静默重排
- 所有事实性信息（营业时间、价格、签证要求）必须来自工具返回，不允许从记忆中回忆

## 交互风格
- 一次只问一个问题
- 给出建议时提供 2-3 个选项
- 使用具体数据支撑建议
- 保持友好但专业的语气
```

- [ ] **Step 2: 写 ContextManager 的失败测试**

```python
# backend/tests/test_context_manager.py
import pytest

from agent.types import Message, Role
from context.manager import ContextManager
from state.models import TravelPlanState, DateRange, Budget


@pytest.fixture
def ctx_manager():
    return ContextManager(soul_path="backend/context/soul.md")


def test_load_soul(ctx_manager):
    soul = ctx_manager._load_soul()
    assert "旅行规划 Agent" in soul


def test_build_system_message(ctx_manager):
    plan = TravelPlanState(session_id="s1", phase=1)
    msg = ctx_manager.build_system_message(plan, phase_prompt="你是灵感顾问", user_summary="")
    assert msg.role == Role.SYSTEM
    assert "旅行规划 Agent" in msg.content  # from SOUL
    assert "灵感顾问" in msg.content  # from phase prompt


def test_build_runtime_context(ctx_manager):
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        destination="Kyoto",
        dates=DateRange(start="2026-04-10", end="2026-04-15"),
        budget=Budget(total=15000),
    )
    ctx = ctx_manager.build_runtime_context(plan)
    assert "Kyoto" in ctx
    assert "15000" in ctx
    assert "阶段：3" in ctx or "阶段: 3" in ctx


def test_should_compress_false(ctx_manager):
    messages = [Message(role=Role.USER, content="hello")]
    assert not ctx_manager.should_compress(messages, max_tokens=100000)


def test_classify_messages(ctx_manager):
    messages = [
        Message(role=Role.USER, content="我不坐红眼航班"),
        Message(role=Role.ASSISTANT, content="好的，已记录"),
        Message(role=Role.USER, content="今天天气怎么样"),
    ]
    must_keep, compressible = ctx_manager.classify_messages(messages)
    # "不坐红眼航班" contains preference signal → must_keep
    assert any("红眼" in m.content for m in must_keep)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_context_manager.py -v`
Expected: FAIL

- [ ] **Step 4: 实现 context/manager.py**

```python
# backend/context/manager.py
from __future__ import annotations

import re
from pathlib import Path

from agent.types import Message, Role
from state.models import TravelPlanState

# Keywords that signal user preferences — these messages must survive compression
_PREFERENCE_SIGNALS = [
    "不要", "不想", "不坐", "不住", "不去", "不吃",
    "必须", "一定要", "偏好", "喜欢", "讨厌",
    "预算", "上限", "最多", "至少",
    "过敏", "素食", "忌口",
]


class ContextManager:
    def __init__(self, soul_path: str = "backend/context/soul.md"):
        self._soul_path = Path(soul_path)
        self._soul_cache: str | None = None

    def _load_soul(self) -> str:
        if self._soul_cache is None:
            if self._soul_path.exists():
                self._soul_cache = self._soul_path.read_text(encoding="utf-8")
            else:
                self._soul_cache = "你是一个旅行规划 Agent。"
        return self._soul_cache

    def build_system_message(
        self,
        plan: TravelPlanState,
        phase_prompt: str,
        user_summary: str = "",
    ) -> Message:
        parts = [
            self._load_soul(),
            "",
            "---",
            "",
            f"## 当前阶段指引\n\n{phase_prompt}",
        ]

        runtime = self.build_runtime_context(plan)
        if runtime:
            parts.extend(["", "---", "", f"## 当前规划状态\n\n{runtime}"])

        if user_summary:
            parts.extend(["", "---", "", f"## 用户画像\n\n{user_summary}"])

        return Message(role=Role.SYSTEM, content="\n".join(parts))

    def build_runtime_context(self, plan: TravelPlanState) -> str:
        parts = [f"- 阶段：{plan.phase}"]
        if plan.destination:
            parts.append(f"- 目的地：{plan.destination}")
        if plan.dates:
            parts.append(f"- 日期：{plan.dates.start} 至 {plan.dates.end}（{plan.dates.total_days} 天）")
        if plan.budget:
            allocated = sum(
                act.cost for day in plan.daily_plans for act in day.activities
            )
            parts.append(f"- 预算：{plan.budget.total} {plan.budget.currency}，已分配：{allocated}")
        if plan.accommodation:
            parts.append(f"- 住宿区域：{plan.accommodation.area}")
        if plan.daily_plans:
            total_days = plan.dates.total_days if plan.dates else "?"
            parts.append(f"- 已规划 {len(plan.daily_plans)}/{total_days} 天")
        if plan.backtrack_history:
            last = plan.backtrack_history[-1]
            parts.append(f"- 最近回溯：阶段{last.from_phase}→{last.to_phase}，原因：{last.reason}")
        return "\n".join(parts)

    def should_compress(self, messages: list[Message], max_tokens: int) -> bool:
        estimated = sum(len(m.content or "") // 3 for m in messages)
        return estimated > max_tokens * 0.5

    def classify_messages(
        self, messages: list[Message]
    ) -> tuple[list[Message], list[Message]]:
        must_keep: list[Message] = []
        compressible: list[Message] = []

        for msg in messages:
            content = msg.content or ""
            if msg.role == Role.USER and any(kw in content for kw in _PREFERENCE_SIGNALS):
                must_keep.append(msg)
            else:
                compressible.append(msg)

        return must_keep, compressible
```

- [ ] **Step 5: 运行测试**

Run: `cd backend && python -m pytest tests/test_context_manager.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add backend/context/ backend/tests/test_context_manager.py
git commit -m "feat: context manager — SOUL.md, four-layer assembly, compression"
```

---

### Task 11: Memory Manager

**Files:**
- Create: `backend/memory/models.py`
- Create: `backend/memory/manager.py`
- Create: `backend/tests/test_memory.py`

- [ ] **Step 1: 写 Memory 的失败测试**

```python
# backend/tests/test_memory.py
import json
from pathlib import Path

import pytest

from memory.models import UserMemory, Rejection, TripSummary
from memory.manager import MemoryManager


def test_user_memory_defaults():
    mem = UserMemory(user_id="u1")
    assert mem.explicit_preferences == {}
    assert mem.rejections == []
    assert mem.trip_history == []


def test_user_memory_serialization():
    mem = UserMemory(
        user_id="u1",
        explicit_preferences={"no_red_eye": True},
        rejections=[Rejection(item="Hotel A", reason="太远", permanent=False, context="Tokyo")],
        trip_history=[TripSummary(destination="Kyoto", dates="2025-10", satisfaction=4, notes="不错")],
    )
    d = mem.to_dict()
    restored = UserMemory.from_dict(d)
    assert restored.explicit_preferences["no_red_eye"] is True
    assert restored.rejections[0].item == "Hotel A"
    assert not restored.rejections[0].permanent


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(data_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_save_and_load(manager):
    mem = UserMemory(user_id="u1", explicit_preferences={"pace": "relaxed"})
    await manager.save(mem)
    loaded = await manager.load("u1")
    assert loaded.explicit_preferences["pace"] == "relaxed"


@pytest.mark.asyncio
async def test_load_nonexistent_returns_empty(manager):
    mem = await manager.load("u_new")
    assert mem.user_id == "u_new"
    assert mem.explicit_preferences == {}


@pytest.mark.asyncio
async def test_generate_summary(manager):
    mem = UserMemory(
        user_id="u1",
        explicit_preferences={"no_red_eye": True, "private_bathroom": True},
        trip_history=[TripSummary(destination="Kyoto", dates="2025-10", satisfaction=4, notes="节奏好")],
        rejections=[Rejection(item="红眼航班", reason="不坐", permanent=True)],
    )
    summary = manager.generate_summary(mem)
    assert "红眼" in summary or "no_red_eye" in summary
    assert "Kyoto" in summary
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_memory.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 memory/models.py**

```python
# backend/memory/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Rejection:
    item: str
    reason: str
    permanent: bool = False
    context: str = ""  # e.g. destination name for scoped rejections

    def to_dict(self) -> dict:
        return {"item": self.item, "reason": self.reason, "permanent": self.permanent, "context": self.context}

    @classmethod
    def from_dict(cls, d: dict) -> Rejection:
        return cls(item=d["item"], reason=d["reason"], permanent=d.get("permanent", False), context=d.get("context", ""))


@dataclass
class TripSummary:
    destination: str
    dates: str
    satisfaction: int | None = None
    notes: str = ""

    def to_dict(self) -> dict:
        return {"destination": self.destination, "dates": self.dates, "satisfaction": self.satisfaction, "notes": self.notes}

    @classmethod
    def from_dict(cls, d: dict) -> TripSummary:
        return cls(destination=d["destination"], dates=d["dates"], satisfaction=d.get("satisfaction"), notes=d.get("notes", ""))


@dataclass
class UserMemory:
    user_id: str
    explicit_preferences: dict[str, Any] = field(default_factory=dict)
    implicit_preferences: dict[str, Any] = field(default_factory=dict)
    trip_history: list[TripSummary] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "explicit_preferences": self.explicit_preferences,
            "implicit_preferences": self.implicit_preferences,
            "trip_history": [t.to_dict() for t in self.trip_history],
            "rejections": [r.to_dict() for r in self.rejections],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UserMemory:
        return cls(
            user_id=d["user_id"],
            explicit_preferences=d.get("explicit_preferences", {}),
            implicit_preferences=d.get("implicit_preferences", {}),
            trip_history=[TripSummary.from_dict(t) for t in d.get("trip_history", [])],
            rejections=[Rejection.from_dict(r) for r in d.get("rejections", [])],
        )
```

- [ ] **Step 4: 实现 memory/manager.py**

```python
# backend/memory/manager.py
from __future__ import annotations

import json
from pathlib import Path

from memory.models import UserMemory


class MemoryManager:
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)

    def _user_dir(self, user_id: str) -> Path:
        return self.data_dir / "users" / user_id

    async def save(self, memory: UserMemory) -> None:
        user_dir = self._user_dir(memory.user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / "memory.json"
        path.write_text(json.dumps(memory.to_dict(), ensure_ascii=False, indent=2))

    async def load(self, user_id: str) -> UserMemory:
        path = self._user_dir(user_id) / "memory.json"
        if not path.exists():
            return UserMemory(user_id=user_id)
        data = json.loads(path.read_text())
        return UserMemory.from_dict(data)

    def generate_summary(self, memory: UserMemory) -> str:
        parts: list[str] = []

        if memory.explicit_preferences:
            prefs = ", ".join(f"{k}: {v}" for k, v in memory.explicit_preferences.items())
            parts.append(f"偏好：{prefs}")

        if memory.trip_history:
            trips = "; ".join(
                f"{t.destination}({t.dates}, 满意度{t.satisfaction}/5)" if t.satisfaction
                else f"{t.destination}({t.dates})"
                for t in memory.trip_history
            )
            parts.append(f"出行历史：{trips}")

        permanent_rejections = [r for r in memory.rejections if r.permanent]
        if permanent_rejections:
            rejects = ", ".join(f"{r.item}({r.reason})" for r in permanent_rejections)
            parts.append(f"永久排除：{rejects}")

        return "\n".join(parts) if parts else "暂无用户画像"
```

- [ ] **Step 5: 运行测试**

Run: `cd backend && python -m pytest tests/test_memory.py -v`
Expected: 5 passed

- [ ] **Step 6: 提交**

```bash
git add backend/memory/ backend/tests/test_memory.py
git commit -m "feat: memory manager — user profile, rejections, cross-session persistence"
```

---

### Task 12: update_plan_state 工具

**Files:**
- Create: `backend/tools/update_plan_state.py`
- Create: `backend/tests/test_update_plan_state.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_update_plan_state.py
import pytest

from state.models import TravelPlanState
from tools.update_plan_state import make_update_plan_state_tool


@pytest.fixture
def plan():
    return TravelPlanState(session_id="s1")


@pytest.fixture
def tool_fn(plan):
    return make_update_plan_state_tool(plan)


@pytest.mark.asyncio
async def test_set_destination(tool_fn, plan):
    result = await tool_fn(field="destination", value="Kyoto")
    assert result["updated_field"] == "destination"
    assert plan.destination == "Kyoto"


@pytest.mark.asyncio
async def test_set_dates(tool_fn, plan):
    result = await tool_fn(field="dates", value={"start": "2026-04-10", "end": "2026-04-15"})
    assert plan.dates is not None
    assert plan.dates.total_days == 5


@pytest.mark.asyncio
async def test_set_budget(tool_fn, plan):
    result = await tool_fn(field="budget", value={"total": 15000, "currency": "CNY"})
    assert plan.budget.total == 15000


@pytest.mark.asyncio
async def test_add_preference(tool_fn, plan):
    result = await tool_fn(field="preferences", value={"key": "pace", "value": "relaxed"})
    assert len(plan.preferences) == 1
    assert plan.preferences[0].key == "pace"


@pytest.mark.asyncio
async def test_add_constraint(tool_fn, plan):
    result = await tool_fn(field="constraints", value={"type": "hard", "description": "预算 1 万"})
    assert len(plan.constraints) == 1


@pytest.mark.asyncio
async def test_invalid_field(tool_fn):
    from tools.base import ToolError
    with pytest.raises(ToolError):
        await tool_fn(field="nonexistent", value="x")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_update_plan_state.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 tools/update_plan_state.py**

```python
# backend/tools/update_plan_state.py
from __future__ import annotations

from typing import Any

from state.models import (
    Accommodation,
    Budget,
    Constraint,
    DateRange,
    Preference,
    Travelers,
    TravelPlanState,
)
from tools.base import ToolError, tool

_ALLOWED_FIELDS = {
    "destination", "dates", "travelers", "budget",
    "accommodation", "preferences", "constraints",
    "destination_candidates",
}

_PARAMETERS = {
    "type": "object",
    "properties": {
        "field": {
            "type": "string",
            "description": f"要更新的字段名。可选值：{', '.join(sorted(_ALLOWED_FIELDS))}",
        },
        "value": {
            "description": "字段的新值。格式取决于字段类型。",
        },
    },
    "required": ["field", "value"],
}


def make_update_plan_state_tool(plan: TravelPlanState):
    """Create an update_plan_state tool bound to a specific plan instance."""

    @tool(
        name="update_plan_state",
        description="""更新旅行规划状态。
Use when: 用户提供了新的信息需要记录到规划中（目的地、日期、预算、偏好等）。
Don't use when: 只是闲聊或询问信息，没有新的决策需要记录。""",
        phases=[1, 2, 3, 4, 5, 7],
        parameters=_PARAMETERS,
    )
    async def update_plan_state(field: str, value: Any) -> dict:
        if field not in _ALLOWED_FIELDS:
            raise ToolError(
                f"不支持的字段: {field}",
                error_code="INVALID_FIELD",
                suggestion=f"可用字段: {', '.join(sorted(_ALLOWED_FIELDS))}",
            )

        if field == "destination":
            plan.destination = str(value)
        elif field == "dates":
            plan.dates = DateRange.from_dict(value) if isinstance(value, dict) else None
        elif field == "travelers":
            plan.travelers = Travelers.from_dict(value) if isinstance(value, dict) else None
        elif field == "budget":
            plan.budget = Budget.from_dict(value) if isinstance(value, dict) else None
        elif field == "accommodation":
            plan.accommodation = Accommodation.from_dict(value) if isinstance(value, dict) else None
        elif field == "preferences":
            plan.preferences.append(Preference.from_dict(value) if isinstance(value, dict) else Preference(key=str(value), value=""))
        elif field == "constraints":
            plan.constraints.append(Constraint.from_dict(value) if isinstance(value, dict) else Constraint(type="soft", description=str(value)))
        elif field == "destination_candidates":
            if isinstance(value, list):
                plan.destination_candidates = value
            else:
                plan.destination_candidates.append(value)

        return {"updated_field": field, "new_value": str(value)[:200]}

    return update_plan_state

```

- [ ] **Step 4: 运行测试**

Run: `cd backend && python -m pytest tests/test_update_plan_state.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add backend/tools/update_plan_state.py backend/tests/test_update_plan_state.py
git commit -m "feat: update_plan_state tool — structured state mutation"
```

---

### Task 13: 外部 API 工具（10 个工具）

**Files:**
- Create: `backend/tools/search_destinations.py`
- Create: `backend/tools/check_feasibility.py`
- Create: `backend/tools/search_flights.py`
- Create: `backend/tools/search_accommodations.py`
- Create: `backend/tools/get_poi_info.py`
- Create: `backend/tools/calculate_route.py`
- Create: `backend/tools/assemble_day_plan.py`
- Create: `backend/tools/check_availability.py`
- Create: `backend/tools/check_weather.py`
- Create: `backend/tools/generate_summary.py`
- Create: corresponding test files for each

由于外部 API 工具数量多且结构类似，此 Task 按子步骤分组。每个工具遵循相同的模式：定义参数 schema → 实现函数（调真实 API，含 httpx 请求）→ 写测试（mock httpx）。

- [ ] **Step 1: search_destinations**

```python
# backend/tools/search_destinations.py
from __future__ import annotations

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索关键词，如 '海岛度假' '日本文化'"},
        "preferences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "用户偏好标签，如 ['美食', '文化', '海滩']",
        },
    },
    "required": ["query"],
}


def make_search_destinations_tool(api_keys: ApiKeysConfig):
    @tool(
        name="search_destinations",
        description="""搜索匹配用户意愿的旅行目的地。
Use when: 用户在阶段 2，需要目的地推荐或对比。
Don't use when: 目的地已确定。
返回 2-5 个目的地候选，含基本信息和匹配度说明。""",
        phases=[2],
        parameters=_PARAMETERS,
    )
    async def search_destinations(query: str, preferences: list[str] | None = None) -> dict:
        if not api_keys.google_maps:
            raise ToolError("Google Maps API key not configured", error_code="NO_API_KEY", suggestion="Set GOOGLE_MAPS_API_KEY")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={
                    "query": f"{query} travel destination",
                    "key": api_keys.google_maps,
                    "type": "locality",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for place in data.get("results", [])[:5]:
            results.append({
                "name": place.get("name", ""),
                "formatted_address": place.get("formatted_address", ""),
                "rating": place.get("rating"),
                "location": place.get("geometry", {}).get("location", {}),
            })

        return {"destinations": results, "source": "google_places", "query": query}

    return search_destinations
```

```python
# backend/tests/test_search_destinations.py
import pytest
import respx
from httpx import Response

from config import ApiKeysConfig
from tools.search_destinations import make_search_destinations_tool


@pytest.fixture
def tool_fn():
    keys = ApiKeysConfig(google_maps="test_key")
    return make_search_destinations_tool(keys)


@respx.mock
@pytest.mark.asyncio
async def test_search_destinations(tool_fn):
    respx.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
        return_value=Response(200, json={
            "results": [
                {"name": "Kyoto", "formatted_address": "Kyoto, Japan", "rating": 4.5,
                 "geometry": {"location": {"lat": 35.01, "lng": 135.76}}},
            ]
        })
    )
    result = await tool_fn(query="日本文化")
    assert len(result["destinations"]) == 1
    assert result["destinations"][0]["name"] == "Kyoto"
    assert result["source"] == "google_places"


@pytest.mark.asyncio
async def test_no_api_key():
    keys = ApiKeysConfig(google_maps="")
    fn = make_search_destinations_tool(keys)
    from tools.base import ToolError
    with pytest.raises(ToolError, match="API key"):
        await fn(query="test")
```

- [ ] **Step 2: 其余 9 个工具**

每个工具遵循完全相同的模式。为保持计划紧凑，这里给出每个工具的核心签名和 API 调用方式，实现时按 `search_destinations` 的模式展开：

**check_feasibility.py** — `check_travel_feasibility(destination, travel_date)` → Sherpa API 查签证 + OpenWeather 查季节

**search_flights.py** — `search_flights(origin, destination, date, max_results=5)` → Amadeus Flight Offers API

**search_accommodations.py** — `search_accommodations(destination, area=None, check_in, check_out, budget_per_night, requirements=None)` → Google Places "lodging" 搜索

**get_poi_info.py** — `get_poi_info(query, location=None)` → Google Places Details API

**calculate_route.py** — `calculate_route(origin_lat, origin_lng, dest_lat, dest_lng, mode="transit")` → Google Maps Directions API

**assemble_day_plan.py** — `assemble_day_plan(pois, start_time, end_time, max_walk_km=10)` → 纯内部逻辑，按地理距离贪心排序

**check_availability.py** — `check_availability(place_name, date)` → Google Places 查营业时间

**check_weather.py** — `check_weather_forecast(city, date)` → OpenWeather Forecast API

**generate_summary.py** — `generate_trip_summary(plan_data)` → 纯内部逻辑，格式化 plan_state 为可读摘要

每个工具都需要对应的测试文件，使用 `respx` mock HTTP 请求。

- [ ] **Step 3: 运行所有工具测试**

Run: `cd backend && python -m pytest tests/test_search_destinations.py -v`
Expected: 2 passed

对每个工具重复运行测试确认通过。

- [ ] **Step 4: 提交**

```bash
git add backend/tools/ backend/tests/test_*.py
git commit -m "feat: 10 domain tools — destinations, flights, POI, routes, weather, etc."
```

---

### Task 14: Agent Loop 核心集成

**Files:**
- Create: `backend/agent/loop.py`
- Create: `backend/tests/test_agent_loop.py`

- [ ] **Step 1: 写 AgentLoop 的失败测试**

```python
# backend/tests/test_agent_loop.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from tools.engine import ToolEngine
from tools.base import tool


@pytest.fixture
def mock_llm():
    provider = AsyncMock()
    return provider


@pytest.fixture
def engine():
    @tool(
        name="greet",
        description="Greet",
        phases=[1, 2, 3, 4, 5, 7],
        parameters={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    async def greet(name: str) -> dict:
        return {"greeting": f"Hello, {name}!"}

    eng = ToolEngine()
    eng.register(greet)
    return eng


@pytest.fixture
def hooks():
    return HookManager()


@pytest.fixture
def agent(mock_llm, engine, hooks):
    return AgentLoop(llm=mock_llm, tool_engine=engine, hooks=hooks, max_retries=3)


@pytest.mark.asyncio
async def test_text_response(agent, mock_llm):
    """LLM returns plain text, no tool calls."""
    async def mock_chat(*args, **kwargs):
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="你好！")
        yield LLMChunk(type=ChunkType.DONE)

    mock_llm.chat = mock_chat

    messages = [Message(role=Role.USER, content="你好")]
    chunks = []
    async for chunk in agent.run(messages, phase=1):
        chunks.append(chunk)

    assert any(c.content == "你好！" for c in chunks)


@pytest.mark.asyncio
async def test_tool_call_then_response(agent, mock_llm):
    """LLM calls a tool, then returns text."""
    call_count = 0

    async def mock_chat(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc_1", name="greet", arguments={"name": "World"}),
            )
            yield LLMChunk(type=ChunkType.DONE)
        else:
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="已打招呼")
            yield LLMChunk(type=ChunkType.DONE)

    mock_llm.chat = mock_chat

    messages = [Message(role=Role.USER, content="say hi")]
    chunks = []
    async for chunk in agent.run(messages, phase=1):
        chunks.append(chunk)

    # Should have tool_call event + text response
    assert any(c.type == ChunkType.TOOL_CALL_START for c in chunks)
    assert any(c.content == "已打招呼" for c in chunks)
    # Messages should have tool result appended
    assert any(m.role == Role.TOOL for m in messages)


@pytest.mark.asyncio
async def test_hooks_called(agent, mock_llm, hooks):
    """Hooks fire after tool calls."""
    hook_called = []

    async def track_hook(**kwargs):
        hook_called.append(kwargs.get("tool_name"))

    hooks.register("after_tool_call", track_hook)

    call_count = 0

    async def mock_chat(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(id="tc_1", name="greet", arguments={"name": "X"}),
            )
            yield LLMChunk(type=ChunkType.DONE)
        else:
            yield LLMChunk(type=ChunkType.TEXT_DELTA, content="done")
            yield LLMChunk(type=ChunkType.DONE)

    mock_llm.chat = mock_chat

    messages = [Message(role=Role.USER, content="hi")]
    async for _ in agent.run(messages, phase=1):
        pass

    assert "greet" in hook_called
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_agent_loop.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 agent/loop.py**

```python
# backend/agent/loop.py
from __future__ import annotations

from typing import AsyncIterator

from agent.hooks import HookManager
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from tools.engine import ToolEngine


class AgentLoop:
    def __init__(
        self,
        llm,
        tool_engine: ToolEngine,
        hooks: HookManager,
        max_retries: int = 3,
    ):
        self.llm = llm
        self.tool_engine = tool_engine
        self.hooks = hooks
        self.max_retries = max_retries

    async def run(
        self,
        messages: list[Message],
        phase: int,
        tools_override: list[dict] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        tools = tools_override or self.tool_engine.get_tools_for_phase(phase)

        for _ in range(20):  # safety limit on loop iterations
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

            # If no tool calls, we're done — the LLM gave a final text response
            if not tool_calls:
                full_text = "".join(text_chunks)
                if full_text:
                    messages.append(Message(role=Role.ASSISTANT, content=full_text))
                yield LLMChunk(type=ChunkType.DONE)
                return

            # Record assistant message with tool calls
            messages.append(Message(
                role=Role.ASSISTANT,
                content="".join(text_chunks) or None,
                tool_calls=tool_calls,
            ))

            # Execute each tool call
            for tc in tool_calls:
                result = await self.tool_engine.execute(tc)

                messages.append(Message(
                    role=Role.TOOL,
                    tool_result=result,
                ))

                await self.hooks.run(
                    "after_tool_call",
                    tool_name=tc.name,
                    tool_call=tc,
                    result=result,
                )

            # Loop continues — LLM will see tool results and decide next step

        # Safety limit reached
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="[达到最大循环次数，请重新发送消息]")
        yield LLMChunk(type=ChunkType.DONE)
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && python -m pytest tests/test_agent_loop.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add backend/agent/loop.py backend/tests/test_agent_loop.py
git commit -m "feat: agent loop — core cycle with hooks, tool execution, streaming"
```

---

### Task 15: FastAPI 入口与 SSE 接口

**Files:**
- Create: `backend/main.py`
- Create: `backend/tests/test_api.py`

- [ ] **Step 1: 写 API 的失败测试**

```python
# backend/tests/test_api.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport

from main import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.mark.asyncio
async def test_health(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_create_session(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert "session_id" in data


@pytest.mark.asyncio
async def test_get_plan(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create session first
        resp = await client.post("/api/sessions")
        session_id = resp.json()["session_id"]

        resp = await client.get(f"/api/plan/{session_id}")
    assert resp.status_code == 200
    assert resp.json()["phase"] == 1


@pytest.mark.asyncio
async def test_get_plan_not_found(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/plan/nonexistent")
    assert resp.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_api.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 main.py**

```python
# backend/main.py
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role
from config import load_config
from context.manager import ContextManager
from harness.validator import validate_hard_constraints
from llm.factory import create_llm_provider
from memory.manager import MemoryManager
from phase.router import PhaseRouter
from state.manager import StateManager
from tools.engine import ToolEngine
from tools.update_plan_state import make_update_plan_state_tool


class ChatRequest(BaseModel):
    message: str
    user_id: str = "default_user"


def create_app(config_path: str = "config.yaml") -> FastAPI:
    config = load_config(config_path)
    state_mgr = StateManager(data_dir=config.data_dir)
    memory_mgr = MemoryManager(data_dir=config.data_dir)
    phase_router = PhaseRouter()
    context_mgr = ContextManager()

    # Session-level caches
    sessions: dict[str, dict] = {}  # session_id → {plan, messages, agent}

    app = FastAPI(title="Travel Agent Pro")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _build_agent(plan):
        llm = create_llm_provider(config.llm)
        tool_engine = ToolEngine()
        tool_engine.register(make_update_plan_state_tool(plan))
        # Additional tools would be registered here

        hooks = HookManager()

        async def on_tool_call(**kwargs):
            if kwargs.get("tool_name") == "update_plan_state":
                phase_router.check_and_apply_transition(plan)

        async def on_validate(**kwargs):
            if kwargs.get("tool_name") == "update_plan_state":
                errors = validate_hard_constraints(plan)
                if errors:
                    session = sessions.get(plan.session_id)
                    if session:
                        session["messages"].append(Message(
                            role=Role.SYSTEM,
                            content=f"⚠️ 硬约束冲突，必须修正：\n" + "\n".join(f"- {e}" for e in errors),
                        ))

        hooks.register("after_tool_call", on_tool_call)
        hooks.register("after_tool_call", on_validate)

        return AgentLoop(llm=llm, tool_engine=tool_engine, hooks=hooks, max_retries=config.max_retries)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/sessions")
    async def create_session():
        plan = await state_mgr.create_session()
        agent = _build_agent(plan)
        sessions[plan.session_id] = {
            "plan": plan,
            "messages": [],
            "agent": agent,
        }
        return {"session_id": plan.session_id, "phase": plan.phase}

    @app.get("/api/plan/{session_id}")
    async def get_plan(session_id: str):
        session = sessions.get(session_id)
        if not session:
            try:
                plan = await state_mgr.load(session_id)
                return plan.to_dict()
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail="Session not found")
        return session["plan"].to_dict()

    @app.post("/api/chat/{session_id}")
    async def chat(session_id: str, req: ChatRequest):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        plan = session["plan"]
        messages = session["messages"]
        agent = session["agent"]

        # Build system message
        phase_prompt = phase_router.get_prompt(plan.phase)
        memory = await memory_mgr.load(req.user_id)
        user_summary = memory_mgr.generate_summary(memory)
        sys_msg = context_mgr.build_system_message(plan, phase_prompt, user_summary)

        # Prepend system message (replace previous one)
        if messages and messages[0].role == Role.SYSTEM:
            messages[0] = sys_msg
        else:
            messages.insert(0, sys_msg)

        # Add user message
        messages.append(Message(role=Role.USER, content=req.message))

        async def event_stream():
            async for chunk in agent.run(messages, phase=plan.phase):
                event_data = {"type": chunk.type.value}
                if chunk.content:
                    event_data["content"] = chunk.content
                if chunk.tool_call:
                    event_data["tool_call"] = {
                        "name": chunk.tool_call.name,
                        "arguments": chunk.tool_call.arguments,
                    }
                yield json.dumps(event_data, ensure_ascii=False)

            # After agent completes, save state and send final plan update
            await state_mgr.save(plan)
            yield json.dumps({
                "type": "state_update",
                "plan": plan.to_dict(),
            }, ensure_ascii=False)

        return EventSourceResponse(event_stream())

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
```

- [ ] **Step 4: 运行 API 测试**

Run: `cd backend && python -m pytest tests/test_api.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add backend/main.py backend/tests/test_api.py
git commit -m "feat: FastAPI gateway — sessions, plan query, SSE chat streaming"
```

---

### Task 16: 前端脚手架

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/types/plan.ts`

- [ ] **Step 1: 初始化前端项目**

```json
// frontend/package.json
{
  "name": "travel-agent-pro-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^19.0.0",
    "react-dom": "^19.0.0",
    "leaflet": "^1.9.4",
    "react-leaflet": "^5.0.0"
  },
  "devDependencies": {
    "@types/react": "^19.0.0",
    "@types/react-dom": "^19.0.0",
    "@types/leaflet": "^1.9.0",
    "@vitejs/plugin-react": "^4.3.0",
    "typescript": "^5.6.0",
    "vite": "^6.0.0"
  }
}
```

```typescript
// frontend/vite.config.ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
```

```html
<!-- frontend/index.html -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Travel Agent Pro</title>
</head>
<body>
  <div id="root"></div>
  <script type="module" src="/src/main.tsx"></script>
</body>
</html>
```

- [ ] **Step 2: 创建前端类型定义**

```typescript
// frontend/src/types/plan.ts
export interface Location {
  lat: number
  lng: number
  name: string
}

export interface DateRange {
  start: string
  end: string
}

export interface Budget {
  total: number
  currency: string
}

export interface Accommodation {
  area: string
  hotel: string | null
}

export interface Activity {
  name: string
  location: Location
  start_time: string
  end_time: string
  category: string
  cost: number
  transport_from_prev: string | null
  transport_duration_min: number
}

export interface DayPlan {
  day: number
  date: string
  activities: Activity[]
  notes: string
}

export interface BacktrackEvent {
  from_phase: number
  to_phase: number
  reason: string
  timestamp: string
}

export interface TravelPlanState {
  session_id: string
  phase: number
  destination: string | null
  dates: DateRange | null
  budget: Budget | null
  accommodation: Accommodation | null
  daily_plans: DayPlan[]
  backtrack_history: BacktrackEvent[]
}

export interface SSEEvent {
  type: 'text_delta' | 'tool_call' | 'state_update' | 'done'
  content?: string
  tool_call?: { name: string; arguments: Record<string, unknown> }
  plan?: TravelPlanState
}
```

- [ ] **Step 3: 创建入口文件**

```tsx
// frontend/src/main.tsx
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles/index.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
```

- [ ] **Step 4: 安装依赖**

Run: `cd frontend && npm install`

- [ ] **Step 5: 提交**

```bash
git add frontend/package.json frontend/tsconfig.json frontend/vite.config.ts frontend/index.html frontend/src/main.tsx frontend/src/types/plan.ts
git commit -m "feat: frontend scaffold — React + Vite + TypeScript types"
```

---

### Task 17: 前端核心组件

**Files:**
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/hooks/useSSE.ts`
- Create: `frontend/src/components/ChatPanel.tsx`
- Create: `frontend/src/components/MessageBubble.tsx`
- Create: `frontend/src/components/PhaseIndicator.tsx`
- Create: `frontend/src/components/MapView.tsx`
- Create: `frontend/src/components/Timeline.tsx`
- Create: `frontend/src/components/BudgetChart.tsx`
- Create: `frontend/src/styles/index.css`

- [ ] **Step 1: 实现 useSSE hook**

```typescript
// frontend/src/hooks/useSSE.ts
import { useCallback, useRef } from 'react'
import type { SSEEvent } from '../types/plan'

export function useSSE() {
  const readerRef = useRef<ReadableStreamDefaultReader | null>(null)

  const sendMessage = useCallback(
    async (
      sessionId: string,
      message: string,
      onEvent: (event: SSEEvent) => void,
    ) => {
      const response = await fetch(`/api/chat/${sessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
      })

      if (!response.ok || !response.body) return

      const reader = response.body.getReader()
      readerRef.current = reader
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const event: SSEEvent = JSON.parse(line.slice(6))
              onEvent(event)
            } catch {
              // skip malformed events
            }
          }
        }
      }
    },
    [],
  )

  return { sendMessage }
}
```

- [ ] **Step 2: 实现 PhaseIndicator**

```tsx
// frontend/src/components/PhaseIndicator.tsx
import React from 'react'

const PHASE_LABELS: Record<number, string> = {
  1: '灵感探索',
  2: '目的地选择',
  3: '天数与节奏',
  4: '住宿区域',
  5: '行程组装',
  7: '出发前查漏',
}

interface Props {
  currentPhase: number
}

export default function PhaseIndicator({ currentPhase }: Props) {
  const phases = [1, 2, 3, 4, 5, 7]
  return (
    <div className="phase-indicator">
      {phases.map((p) => (
        <div
          key={p}
          className={`phase-step ${p === currentPhase ? 'active' : ''} ${p < currentPhase ? 'done' : ''}`}
        >
          <span className="phase-num">{p}</span>
          <span className="phase-label">{PHASE_LABELS[p]}</span>
        </div>
      ))}
    </div>
  )
}
```

- [ ] **Step 3: 实现 MessageBubble 和 ChatPanel**

```tsx
// frontend/src/components/MessageBubble.tsx
import React from 'react'

interface Props {
  role: 'user' | 'assistant' | 'tool'
  content: string
  toolName?: string
}

export default function MessageBubble({ role, content, toolName }: Props) {
  if (role === 'tool') {
    return (
      <div className="message tool">
        <span className="tool-badge">🔧 {toolName}</span>
      </div>
    )
  }
  return (
    <div className={`message ${role}`}>
      <div className="bubble">{content}</div>
    </div>
  )
}
```

```tsx
// frontend/src/components/ChatPanel.tsx
import React, { useState, useRef, useEffect } from 'react'
import MessageBubble from './MessageBubble'
import { useSSE } from '../hooks/useSSE'
import type { SSEEvent, TravelPlanState } from '../types/plan'

interface ChatMessage {
  role: 'user' | 'assistant' | 'tool'
  content: string
  toolName?: string
}

interface Props {
  sessionId: string
  onPlanUpdate: (plan: TravelPlanState) => void
}

export default function ChatPanel({ sessionId, onPlanUpdate }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const { sendMessage } = useSSE()

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSend = async () => {
    if (!input.trim() || streaming) return
    const userMsg = input.trim()
    setInput('')
    setMessages((prev) => [...prev, { role: 'user', content: userMsg }])
    setStreaming(true)

    let assistantContent = ''

    await sendMessage(sessionId, userMsg, (event: SSEEvent) => {
      if (event.type === 'text_delta' && event.content) {
        assistantContent += event.content
        setMessages((prev) => {
          const copy = [...prev]
          const last = copy[copy.length - 1]
          if (last?.role === 'assistant') {
            copy[copy.length - 1] = { ...last, content: assistantContent }
          } else {
            copy.push({ role: 'assistant', content: assistantContent })
          }
          return copy
        })
      } else if (event.type === 'tool_call' && event.tool_call) {
        setMessages((prev) => [
          ...prev,
          { role: 'tool', content: '', toolName: event.tool_call!.name },
        ])
      } else if (event.type === 'state_update' && event.plan) {
        onPlanUpdate(event.plan)
      }
    })

    setStreaming(false)
  }

  return (
    <div className="chat-panel">
      <div className="messages">
        {messages.map((m, i) => (
          <MessageBubble key={i} role={m.role} content={m.content} toolName={m.toolName} />
        ))}
        <div ref={bottomRef} />
      </div>
      <div className="input-bar">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSend()}
          placeholder="输入你的旅行想法..."
          disabled={streaming}
        />
        <button onClick={handleSend} disabled={streaming}>
          发送
        </button>
      </div>
    </div>
  )
}
```

- [ ] **Step 4: 实现 MapView（简版）、Timeline、BudgetChart**

```tsx
// frontend/src/components/MapView.tsx
import React from 'react'
import { MapContainer, TileLayer, Marker, Polyline } from 'react-leaflet'
import type { DayPlan } from '../types/plan'
import 'leaflet/dist/leaflet.css'

interface Props {
  dailyPlans: DayPlan[]
}

export default function MapView({ dailyPlans }: Props) {
  const points = dailyPlans.flatMap((d) =>
    d.activities.map((a) => [a.location.lat, a.location.lng] as [number, number])
  )

  if (points.length === 0) {
    return <div className="map-empty">行程确定后将在此显示路线地图</div>
  }

  const center = points[0]

  return (
    <MapContainer center={center} zoom={13} style={{ height: '300px', width: '100%' }}>
      <TileLayer url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
      {points.map((p, i) => (
        <Marker key={i} position={p} />
      ))}
      {points.length > 1 && <Polyline positions={points} color="blue" />}
    </MapContainer>
  )
}
```

```tsx
// frontend/src/components/Timeline.tsx
import React from 'react'
import type { DayPlan } from '../types/plan'

interface Props {
  dailyPlans: DayPlan[]
}

export default function Timeline({ dailyPlans }: Props) {
  if (dailyPlans.length === 0) {
    return <div className="timeline-empty">行程规划中...</div>
  }

  return (
    <div className="timeline">
      {dailyPlans.map((day) => (
        <div key={day.day} className="timeline-day">
          <h4>Day {day.day} — {day.date}</h4>
          {day.activities.map((act, i) => (
            <div key={i} className="timeline-item">
              <span className="time">{act.start_time}-{act.end_time}</span>
              <span className="name">{act.name}</span>
              {act.cost > 0 && <span className="cost">¥{act.cost}</span>}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
```

```tsx
// frontend/src/components/BudgetChart.tsx
import React from 'react'
import type { TravelPlanState } from '../types/plan'

interface Props {
  plan: TravelPlanState
}

export default function BudgetChart({ plan }: Props) {
  if (!plan.budget) return null

  const spent = plan.daily_plans.reduce(
    (sum, d) => sum + d.activities.reduce((s, a) => s + a.cost, 0),
    0,
  )
  const pct = Math.min((spent / plan.budget.total) * 100, 100)

  return (
    <div className="budget-chart">
      <div className="budget-header">
        <span>预算</span>
        <span>¥{spent} / ¥{plan.budget.total}</span>
      </div>
      <div className="budget-bar">
        <div className="budget-fill" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}
```

- [ ] **Step 5: 实现 App.tsx**

```tsx
// frontend/src/App.tsx
import React, { useEffect, useState } from 'react'
import ChatPanel from './components/ChatPanel'
import PhaseIndicator from './components/PhaseIndicator'
import MapView from './components/MapView'
import Timeline from './components/Timeline'
import BudgetChart from './components/BudgetChart'
import type { TravelPlanState } from './types/plan'

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [plan, setPlan] = useState<TravelPlanState | null>(null)

  useEffect(() => {
    fetch('/api/sessions', { method: 'POST' })
      .then((r) => r.json())
      .then((data) => {
        setSessionId(data.session_id)
        setPlan({ session_id: data.session_id, phase: 1, destination: null, dates: null, budget: null, accommodation: null, daily_plans: [], backtrack_history: [] })
      })
  }, [])

  if (!sessionId) return <div className="loading">初始化中...</div>

  return (
    <div className="app">
      <header className="app-header">
        <h1>Travel Agent Pro</h1>
        {plan && <PhaseIndicator currentPhase={plan.phase} />}
      </header>
      <div className="app-body">
        <div className="left-panel">
          <ChatPanel sessionId={sessionId} onPlanUpdate={setPlan} />
        </div>
        <div className="right-panel">
          {plan && (
            <>
              <BudgetChart plan={plan} />
              <MapView dailyPlans={plan.daily_plans} />
              <Timeline dailyPlans={plan.daily_plans} />
            </>
          )}
        </div>
      </div>
    </div>
  )
}
```

- [ ] **Step 6: 创建基础样式**

```css
/* frontend/src/styles/index.css */
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f0f0f; color: #e0e0e0; }

.app { display: flex; flex-direction: column; height: 100vh; }
.app-header { padding: 12px 24px; background: #1a1a2e; border-bottom: 1px solid #333; display: flex; align-items: center; gap: 24px; }
.app-header h1 { font-size: 18px; color: #64b5f6; }
.app-body { display: flex; flex: 1; overflow: hidden; }
.left-panel { flex: 1; display: flex; flex-direction: column; border-right: 1px solid #333; }
.right-panel { width: 400px; padding: 16px; overflow-y: auto; display: flex; flex-direction: column; gap: 16px; }

/* Phase Indicator */
.phase-indicator { display: flex; gap: 8px; }
.phase-step { display: flex; align-items: center; gap: 4px; padding: 4px 8px; border-radius: 12px; font-size: 12px; background: #222; }
.phase-step.active { background: #1a3a5c; color: #64b5f6; }
.phase-step.done { opacity: 0.5; }
.phase-num { font-weight: bold; }

/* Chat */
.chat-panel { display: flex; flex-direction: column; flex: 1; }
.messages { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 8px; }
.message.user .bubble { background: #1a3a5c; align-self: flex-end; padding: 8px 12px; border-radius: 12px; max-width: 80%; }
.message.assistant .bubble { background: #2a2a3e; padding: 8px 12px; border-radius: 12px; max-width: 80%; white-space: pre-wrap; }
.message.user { display: flex; justify-content: flex-end; }
.message.tool { font-size: 12px; color: #888; }
.tool-badge { background: #333; padding: 2px 8px; border-radius: 4px; }
.input-bar { display: flex; padding: 12px; gap: 8px; border-top: 1px solid #333; }
.input-bar input { flex: 1; background: #222; border: 1px solid #444; color: #e0e0e0; padding: 8px 12px; border-radius: 8px; outline: none; }
.input-bar button { background: #1a3a5c; color: #64b5f6; border: none; padding: 8px 16px; border-radius: 8px; cursor: pointer; }
.input-bar button:disabled { opacity: 0.5; }

/* Budget */
.budget-chart { background: #1a1a2e; padding: 12px; border-radius: 8px; }
.budget-header { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 6px; }
.budget-bar { height: 8px; background: #333; border-radius: 4px; }
.budget-fill { height: 100%; background: #64b5f6; border-radius: 4px; transition: width 0.3s; }

/* Timeline */
.timeline { background: #1a1a2e; padding: 12px; border-radius: 8px; }
.timeline-day { margin-bottom: 12px; }
.timeline-day h4 { font-size: 13px; color: #64b5f6; margin-bottom: 6px; }
.timeline-item { display: flex; gap: 8px; font-size: 12px; padding: 4px 0; }
.timeline-item .time { color: #888; min-width: 90px; }
.timeline-item .cost { color: #ffb74d; margin-left: auto; }

/* Map */
.map-empty, .timeline-empty { background: #1a1a2e; padding: 24px; border-radius: 8px; text-align: center; color: #666; }
.loading { display: flex; align-items: center; justify-content: center; height: 100vh; color: #666; }
```

- [ ] **Step 7: 提交**

```bash
git add frontend/
git commit -m "feat: frontend — chat panel, phase indicator, map, timeline, budget"
```

---

### Task 18: 端到端集成验证

**Files:** 无新文件，验证现有集成

- [ ] **Step 1: 运行全部后端测试**

Run: `cd backend && python -m pytest tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 2: 启动后端并手动验证 API**

Run: `cd backend && uvicorn main:app --port 8000`

在另一个终端中：
```bash
# 创建会话
curl -X POST http://localhost:8000/api/sessions | python -m json.tool

# 查询 plan
curl http://localhost:8000/api/plan/{session_id} | python -m json.tool
```

Expected: 返回正确的 JSON 结构

- [ ] **Step 3: 启动前端并验证页面**

Run: `cd frontend && npm run dev`

打开 http://localhost:5173，确认：
- 页面正常加载
- 阶段指示器显示"阶段 1: 灵感探索"
- 输入消息后能看到流式响应
- 右侧面板显示占位状态

- [ ] **Step 4: 提交集成验证通过**

```bash
git commit --allow-empty -m "chore: end-to-end integration verified"
```

---

## Spec Coverage Checklist

| 规格章节 | 对应 Task |
|---------|----------|
| 4.1 Agent Loop | Task 14 |
| 4.2 Phase Router | Task 6 |
| 4.3 Tool Engine | Task 5, 12, 13 |
| 4.4 Context Manager | Task 10 |
| 4.5 LLM Abstraction | Task 3 |
| 4.6 Harness | Task 8, 9 |
| 4.7 State & Memory | Task 4, 11 |
| 5. 前端设计 | Task 16, 17 |
| 7. 目录结构 | Task 1 |
| Core types | Task 2 |
| Hooks | Task 7 |
| API Gateway | Task 15 |
| 集成验证 | Task 18 |
