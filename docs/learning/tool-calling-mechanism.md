# Tool Calling 机制解析

## 整体架构

项目采用自研的多阶段 Agent 循环架构实现 Tool Calling，直接对接 Anthropic 原生 API，未依赖 LangChain 等框架。核心分为五层：

```
┌─────────────────────────────────────────────────────────┐
│  第五层：API 传输层  (main.py)                            │
│  HTTP 端点 · SSE 流式推送 · 会话管理 · Hook 注册          │
├─────────────────────────────────────────────────────────┤
│  第四层：LLM 适配层  (anthropic_provider.py)              │
│  消息格式转换 · 工具 Schema 转换 · 流式响应解析            │
├─────────────────────────────────────────────────────────┤
│  第三层：Agent 循环层  (loop.py)                          │
│  ReAct 循环编排 · 流式输出 · Hook 触发 · 安全上限          │
├─────────────────────────────────────────────────────────┤
│  第二层：工具执行层  (engine.py)                           │
│  工具注册 · 阶段过滤 · 执行调度 · 三级错误处理 · 遥测      │
├─────────────────────────────────────────────────────────┤
│  第一层：工具定义层  (base.py + 各工具文件)                 │
│  @tool 装饰器 · ToolDef 数据类 · ToolError 异常           │
└─────────────────────────────────────────────────────────┘
```

### 一次完整调用的数据流

```
用户发送 "帮我搜一下东京的景点"
  → POST /api/chat/{session_id}                    [第五层：接收 HTTP 请求]
  → 构建系统消息 + 注入阶段 prompt                    [第五层：上下文组装]
  → AgentLoop.run(messages, phase=2)                [第三层：启动循环]
  → AnthropicProvider.chat(messages, tools)          [第四层：调用 Claude API]
  → Claude 返回 tool_use: search_destinations        [第四层：流式解析]
  → ToolEngine.execute(ToolCall)                     [第二层：执行工具]
  → search_destinations(**kwargs)                    [第一层：实际工具函数]
  → ToolResult 追加到 messages                       [第三层：结果回注]
  → 再次调用 Claude → 返回文本总结                     [第三层→第四层]
  → SSE 流式推送给前端                                [第五层：响应输出]
```

---

## 第一层：工具定义层

**文件**：`backend/tools/base.py` + 各工具实现文件（如 `search_destinations.py`）

这一层解决的问题：**如何用最少的代码定义一个对 LLM 可见的工具？**

打个比方：LLM（Claude）就像一个聪明但没有手的大脑。它能思考、能规划，但不能自己去查航班、搜景点。**工具就是给这个大脑装上的"手"**——每只手有不同的能力（搜目的地、查天气、算路线等）。第一层要做的事情就是：**定义每只"手"长什么样、能干什么、什么时候能用**。

### 1.1 ToolError — 会说话的错误

普通的 Python 异常就像一个只会说"出错了"的人，没有任何有用信息。而 `ToolError` 是一个**会说话的错误**——它不仅告诉你"哪里出错了"，还告诉 LLM"你可以怎么补救"。

**生活类比**：你去餐厅点菜——

- 普通异常 `Exception("出错了")` → 服务员说："不行。"（然后转身走了）
- `ToolError("没有牛排", error_code="OUT_OF_STOCK", suggestion="试试鸡排")` → 服务员说："牛排卖完了（错误码），但今天鸡排不错您要不要试试？（建议）"

LLM 收到带 `suggestion` 的错误后，就能像聪明的顾客一样自己调整策略，而不是傻愣在那里。

```python
class ToolError(Exception):
    def __init__(self, message: str, error_code: str = "UNKNOWN", suggestion: str = ""):
        super().__init__(message)
        self.error_code = error_code   # 错误编号，方便程序判断是哪类错误
        self.suggestion = suggestion   # 给 LLM 的"补救建议"
```

**实际场景**：工具发现 API Key 没配置时——

```python
raise ToolError(
    "Google Maps API key not configured",   # 人类可读的错误描述
    error_code="NO_API_KEY",                # 机器可读的错误码
    suggestion="Set GOOGLE_MAPS_API_KEY",   # 告诉 LLM 怎么办
)
```

LLM 收到这个错误后，就知道不是自己参数传错了，而是系统配置问题，它可以换一种方式回答用户，而不是反复重试同一个工具。

### 1.2 ToolDef — 工具的"身份证"

`ToolDef` 是每个工具的"身份证"，上面写着这个工具的所有关键信息。

**生活类比**：想象一个公司的员工工牌——

| 工牌字段 | ToolDef 字段 | 含义 |
|---------|-------------|------|
| 姓名 | `name` | 工具叫什么，LLM 用这个名字来"点名"调用 |
| 岗位描述 | `description` | 这个工具能干什么，LLM 看这个来决定什么时候该用它 |
| 排班表 | `phases` | 这个工具在哪些阶段"上班"（比如只在阶段 2 可用） |
| 操作手册 | `parameters` | 调用这个工具需要传什么参数（JSON Schema 格式） |
| 本人 | `_fn` | 工具背后真正干活的那个异步函数 |

```python
@dataclass
class ToolDef:
    name: str                    # "姓名"：如 "search_destinations"
    description: str             # "岗位描述"：如 "搜索旅行目的地"
    phases: list[int]            # "排班表"：如 [2] 表示只在阶段2上班
    parameters: dict[str, Any]   # "操作手册"：需要什么输入参数
    _fn: Callable[..., Coroutine[Any, Any, Any]]  # "本人"：实际干活的函数
```

ToolDef 有两个重要方法：

**`__call__`** —— 让"身份证"本身就能干活

```python
async def __call__(self, **kwargs: Any) -> Any:
    return await self._fn(**kwargs)
```

正常情况下，身份证只是一张卡片，你不能对着卡片说"帮我干活"。但 Python 的 `__call__` 魔法方法让 ToolDef 实例可以像函数一样被调用。也就是说：

```python
# 这两种写法效果完全一样：
result = await tool_def._fn(query="东京")     # 直接调用内部函数
result = await tool_def(query="东京")          # 把 ToolDef 当函数调用（更优雅）
```

**`to_schema`** —— 生成给 LLM 看的"简历"

```python
def to_schema(self) -> dict[str, Any]:
    return {
        "name": self.name,
        "description": self.description,
        "parameters": self.parameters,
    }
```

LLM 不需要知道工具内部怎么实现的（`_fn`），它只需要知道：叫什么名字、能干什么、需要什么参数。`to_schema()` 就是把"身份证"转成一份精简的"简历"发给 LLM。

### 1.3 @tool 装饰器 — 工具的"制证机"

写一个工具时，你本质上要做两件事：① 写干活的函数 ② 填工具的身份信息。`@tool` 装饰器把这两步合成了一步。

**生活类比**：`@tool` 就像公司的"制证机"——你把一个新员工（函数）和他的信息（名字、岗位、排班）一起塞进去，出来的就是一张完整的工牌（ToolDef）。

```python
def tool(name, description, phases, parameters):
    def decorator(fn):
        # 把函数 fn 和元信息打包成一个 ToolDef
        return ToolDef(name=name, description=description,
                       phases=phases, parameters=parameters, _fn=fn)
    return decorator
```

**使用前 vs 使用后**：

```python
# ❌ 不用装饰器，手动创建（啰嗦）
async def search_destinations(query: str) -> dict:
    ...
search_destinations_tool = ToolDef(
    name="search_destinations", description="搜索目的地",
    phases=[2], parameters={...}, _fn=search_destinations
)

# ✅ 用装饰器，一步到位（简洁）
@tool(name="search_destinations", description="搜索目的地", phases=[2], parameters={...})
async def search_destinations(query: str) -> dict:
    ...
# 此时 search_destinations 已经不是普通函数了，它是一个 ToolDef 实例！
# 但因为有 __call__，你仍然可以像调用函数一样调用它
```

**关键理解**：装饰器执行后，`search_destinations` 这个变量指向的不再是原来的函数，而是一个 `ToolDef` 对象。这个对象既能当"数据"用（提取 schema 给 LLM 看），又能当"函数"用（直接调用执行）。一石二鸟。

### 1.4 实际工具示例：search_destinations

理解了上面三个概念，现在来看一个完整的真实工具是怎么写的。

**第一步：定义参数手册（JSON Schema）**

这是告诉 LLM "调用这个工具时需要传什么参数"的说明书：

```python
_PARAMETERS = {
    "type": "object",          # 参数整体是一个对象
    "properties": {
        "query": {
            "type": "string",  # query 是字符串类型
            "description": "搜索关键词，如 '海岛度假' '日本文化'",
            # ↑ 这段描述是给 LLM 看的，帮它理解该传什么值
        },
        "preferences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "用户偏好标签，如 ['美食', '文化', '海滩']",
        },
    },
    "required": ["query"],     # query 是必填的，preferences 是可选的
}
```

LLM 看到这个 schema 后就知道：调用 `search_destinations` 时，必须传一个 `query` 字符串，可以选传一个 `preferences` 数组。

**第二步：用工厂函数 + @tool 装饰器定义工具**

```python
def make_search_destinations_tool(api_keys: ApiKeysConfig):
    # ↑ 工厂函数：接收依赖（API 密钥），返回一个配置好的工具

    @tool(
        name="search_destinations",
        description="""搜索匹配用户意愿的旅行目的地。
Use when: 用户在阶段 2，需要目的地推荐或对比。
Don't use when: 目的地已确定。
返回 2-5 个目的地候选，含基本信息和匹配度说明。""",
        # ↑ description 写得越清楚，LLM 越知道什么时候该用/不该用这个工具

        phases=[2],             # 只在阶段 2（目的地选择）可用
        parameters=_PARAMETERS, # 上面定义的参数手册
    )
    async def search_destinations(query: str, preferences: list[str] | None = None) -> dict:
        # 检查 API Key 是否配置
        if not api_keys.google_maps:
            raise ToolError(
                "Google Maps API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set GOOGLE_MAPS_API_KEY",
            )
            # ↑ 抛出 ToolError 而不是普通异常，LLM 能看到建议

        # 调用 Google Places API 搜索目的地
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={"query": f"{query} travel destination", "key": api_keys.google_maps},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        # 提取前 5 个结果，只保留关键字段
        results = [
            {
                "name": place.get("name", ""),
                "formatted_address": place.get("formatted_address", ""),
                "rating": place.get("rating"),
                "location": place.get("geometry", {}).get("location", {}),
            }
            for place in data.get("results", [])[:5]
        ]
        return {"destinations": results, "source": "google_places", "query": query}
        # ↑ 返回值会被包装成 ToolResult.data，LLM 会看到这些数据并据此回答用户

    return search_destinations
    # ↑ 返回的是 ToolDef 实例（不是普通函数），可以注册到 ToolEngine
```

**为什么要用工厂函数 `make_xxx_tool()`，而不是直接定义？**

类比：你开了一家连锁餐厅，每家分店的厨师（函数逻辑）是一样的，但每家店用的食材供应商（API Key）不同。工厂函数就像"开分店"的流程——你告诉它用哪个供应商，它就给你造出一个配置好的厨师。

```python
# 在 main.py 中，用不同的配置"开分店"
tool_engine.register(make_search_destinations_tool(config.api_keys))
tool_engine.register(make_search_flights_tool(config.api_keys, flyai_client))
# 每个工具都通过工厂函数拿到自己需要的依赖，互不干扰
```

**`phases=[2]` 的实际效果**：

```
阶段 1（需求收集）：LLM 能看到的工具 → [update_plan_state]
                    search_destinations 不在列表中，LLM 根本不知道有这个工具

阶段 2（目的地选择）：LLM 能看到的工具 → [search_destinations, quick_travel_search, ...]
                      现在 LLM 看到了，可以决定是否调用

阶段 3（行程规划）：LLM 能看到的工具 → [calculate_route, assemble_day_plan, ...]
                    search_destinations 又消失了，防止 LLM 在规划阶段跑去重新搜目的地
```

这就像不同阶段给 LLM 发不同的"工具菜单"——阶段 1 只给它一把螺丝刀，阶段 2 给它一套扳手，阶段 3 换成电钻。LLM 只能从当前菜单里选，不会乱用工具。

---

## 🔗 串联解析：工具定义如何变成 API 的 tools 参数？

用 OpenAI / Anthropic SDK 时，我们最终要把工具列表放在 API 请求的 `tools` 字段里，像这样：

```python
# 最终发给 Anthropic API 的样子
client.messages.create(
    model="claude-sonnet-4-20250514",
    messages=[...],
    tools=[                          # ← 就是这个 tools 列表
        {
            "name": "search_destinations",
            "description": "搜索匹配用户意愿的旅行目的地...",
            "input_schema": {        # ← Anthropic 用 input_schema
                "type": "object",
                "properties": {"query": {"type": "string", ...}},
                "required": ["query"]
            }
        },
        # ... 更多工具
    ]
)
```

**问题是：这个 tools 列表是怎么从 Python 函数一步步变成上面这个格式的？**

答案是经过了 **4 次变形**，每一层做一点转换：

```
第一层                第一层               第二层                  第三层              第四层
@tool 装饰器    →    ToolDef 实例    →    to_schema() 字典    →    传递给 LLM    →    转为 API 格式
(Python 函数)       (数据+函数合体)       (纯数据，无函数)        (原样传递)         (字段名映射)
```

### 变形第 ① 步：@tool 装饰器 → ToolDef 实例（第一层）

```python
# 你写的代码
@tool(
    name="search_destinations",
    description="搜索匹配用户意愿的旅行目的地...",
    phases=[2],
    parameters={"type": "object", "properties": {...}, "required": ["query"]},
)
async def search_destinations(query: str, ...) -> dict:
    ...
```

装饰器执行后，`search_destinations` 变成了一个 ToolDef 对象：

```python
# 内存中的 ToolDef 实例（伪代码表示）
search_destinations = ToolDef(
    name="search_destinations",
    description="搜索匹配用户意愿的旅行目的地...",
    phases=[2],
    parameters={"type": "object", "properties": {...}, "required": ["query"]},
    _fn=<原始的 async function>,    # ← 还带着可执行的函数
)
```

### 变形第 ② 步：ToolDef → schema 字典（第二层 ToolEngine）

当 Agent 循环需要知道"当前阶段有哪些工具"时，调用 `get_tools_for_phase`：

```python
# backend/tools/engine.py
def get_tools_for_phase(self, phase: int) -> list[dict]:
    return [t.to_schema() for t in self._tools.values() if phase in t.phases]
```

`to_schema()` 把 ToolDef 转成纯字典，**丢掉了 `_fn` 和 `phases`**（LLM 不需要知道这些）：

```python
# to_schema() 的输出
{
    "name": "search_destinations",
    "description": "搜索匹配用户意愿的旅行目的地...",
    "parameters": {                  # ← 注意这里叫 "parameters"
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词..."},
            "preferences": {"type": "array", ...}
        },
        "required": ["query"]
    }
}
```

如果当前是阶段 2，可能返回 5 个工具的 schema 列表。如果是阶段 1，`search_destinations` 因为 `phases=[2]` 会被过滤掉。

### 变形第 ③ 步：schema 列表 → 传给 LLM（第三层 AgentLoop）

```python
# backend/agent/loop.py
async def run(self, messages, phase, tools_override=None):
    tools = tools_override or self.tool_engine.get_tools_for_phase(phase)
    #                         ↑ 拿到第②步的 schema 列表

    async for chunk in self.llm.chat(messages, tools=tools, stream=True):
    #                                          ↑ 原样传给第四层
        ...
```

第三层不做任何转换，只是把 schema 列表原样传给 LLM 适配层。它的职责是编排循环，不关心数据格式。

### 变形第 ④ 步：内部格式 → Anthropic API 格式（第四层 AnthropicProvider）

这是最关键的一步——**字段名映射**：

```python
# backend/llm/anthropic_provider.py
def _convert_tools(self, tool_defs: list[dict]) -> list[dict]:
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],    # ← 关键！parameters → input_schema
        }
        for t in tool_defs
    ]
```

转换后的结果：

```python
# _convert_tools() 的输出 —— 这就是最终发给 Anthropic API 的格式
{
    "name": "search_destinations",
    "description": "搜索匹配用户意愿的旅行目的地...",
    "input_schema": {                # ← 从 "parameters" 变成了 "input_schema"
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词..."},
            "preferences": {"type": "array", ...}
        },
        "required": ["query"]
    }
}
```

最后在 `chat()` 方法中塞进 API 请求：

```python
# backend/llm/anthropic_provider.py → chat()
kwargs = {
    "model": self.model,
    "system": system,
    "messages": converted,
    "temperature": self.temperature,
    "max_tokens": self.max_tokens,
}
if tools:
    kwargs["tools"] = self._convert_tools(tools)   # ← 放进 tools 字段

async with self.client.messages.stream(**kwargs) as stream_resp:
    ...  # Claude 现在知道有哪些工具可以用了
```

### 为什么不直接一步到位？

你可能会想：为什么不在 `@tool` 装饰器里直接生成 Anthropic 的 `input_schema` 格式，省掉中间步骤？

因为**内部格式要保持 API 无关**。如果明天要换成 OpenAI 的 API：

```python
# OpenAI 的 tools 格式长这样（和 Anthropic 不同）
{
    "type": "function",
    "function": {
        "name": "search_destinations",
        "description": "...",
        "parameters": {...}          # ← OpenAI 用 "parameters"，不用 "input_schema"
    }
}
```

只需要写一个新的第四层 `OpenAIProvider._convert_tools()`，前三层的代码一行都不用改。这就是分层的价值。

### 完整变形链一图总结

```
┌─────────────────────────────────────────────────────────────────┐
│  @tool(name, desc, phases, params)                              │
│  async def search_destinations(...):                            │
│      ...                                          [第一层：定义] │
└──────────────────────┬──────────────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  ToolDef(name, desc, phases, params, _fn)                        │
│                                                                  │
│  ToolEngine.register(tool_def)  → 存入 _tools 字典               │
│  ToolEngine.get_tools_for_phase(2)                               │
│    → [tool_def.to_schema()]                                      │
│    → [{"name", "description", "parameters"}]     [第二层：过滤]   │
└──────────────────────┬───────────────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  AgentLoop.run(messages, phase=2)                                │
│    tools = tool_engine.get_tools_for_phase(2)                    │
│    llm.chat(messages, tools=tools)               [第三层：传递]   │
└──────────────────────┬───────────────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│  AnthropicProvider._convert_tools(tools)                         │
│    "parameters" → "input_schema"                                 │
│                                                                  │
│  client.messages.stream(                                         │
│    model=..., messages=...,                                      │
│    tools=[{"name", "description", "input_schema"}]               │
│  )                                               [第四层：转换]   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 第二层：工具执行层

**文件**：`backend/tools/engine.py`

这一层解决的问题：**如何安全地管理和执行工具，并将结果标准化返回给 Agent 循环？**

### 2.1 ToolEngine 核心结构

```python
class ToolEngine:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}   # name → ToolDef 的注册表

    def register(self, tool_def: ToolDef) -> None:
        self._tools[tool_def.name] = tool_def

    def get_tools_for_phase(self, phase: int) -> list[dict[str, Any]]:
        return [t.to_schema() for t in self._tools.values() if phase in t.phases]
        #      ↑ 只返回 schema，不暴露内部函数    ↑ 阶段过滤的核心逻辑

    def get_tool(self, name: str) -> ToolDef | None:
        return self._tools.get(name)
```

`get_tools_for_phase` 是阶段感知的关键——它根据当前阶段筛选工具 schema，传给 LLM 的 `tools` 参数。LLM 只能看到当前阶段允许的工具。

### 2.2 三级错误处理 + OpenTelemetry 遥测

```python
async def execute(self, call: ToolCall) -> ToolResult:
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("tool.execute") as span:
        span.add_event(EVENT_TOOL_INPUT, {
            "arguments": truncate(json.dumps(call.arguments, ensure_ascii=False)),
        })

        tool_def = self._tools.get(call.name)

        # ❶ 工具不存在 → UNKNOWN_TOOL（LLM 幻觉了一个不存在的工具名）
        if not tool_def:
            return ToolResult(
                tool_call_id=call.id, status="error",
                error_code="UNKNOWN_TOOL",
                suggestion=f"Available tools: {', '.join(self._tools.keys())}",
            )

        try:
            # ❷ 正常执行
            data = await tool_def(**call.arguments)
            return ToolResult(tool_call_id=call.id, status="success", data=data)

        except ToolError as e:
            # ❸ 业务错误 → 带 error_code 和 suggestion，LLM 可据此重试
            return ToolResult(
                tool_call_id=call.id, status="error",
                error_code=e.error_code, suggestion=e.suggestion,
            )

        except Exception as e:
            # ❹ 未预期异常 → INTERNAL_ERROR，记录异常堆栈
            span.record_exception(e)
            return ToolResult(
                tool_call_id=call.id, status="error",
                error_code="INTERNAL_ERROR",
                suggestion="An unexpected error occurred",
            )
```

**设计要点**：
- 所有错误都被捕获并转为 `ToolResult`，永远不会让异常冒泡到 Agent 循环层
- `suggestion` 字段是给 LLM 的"修复提示"，比如告诉它可用的工具列表
- 每次执行都创建 OpenTelemetry span，记录输入参数和输出结果，便于调试和监控

---

## 第三层：Agent 循环层

**文件**：`backend/agent/loop.py`

这一层解决的问题：**如何编排 LLM 调用与工具执行的交替循环，直到得到最终回答？**

这是经典的 **ReAct（Reasoning + Acting）模式**的实现。

### 3.1 循环流程图

```
AgentLoop.run(messages, phase=2)
  │
  ├─ for iteration in range(max_retries):     ← 安全上限，防止无限循环
  │   │
  │   ├─ hooks.run("before_llm_call")         ← 钩子：上下文压缩等
  │   │
  │   ├─ llm.chat(messages, tools, stream=True)
  │   │   ├─ TEXT_DELTA → yield 给前端        ← 实时流式输出
  │   │   └─ TOOL_CALL_START → 收集到列表
  │   │
  │   ├─ 没有 tool_calls？
  │   │   └─ YES → 记录 assistant 消息 → yield DONE → return  ✅ 循环结束
  │   │
  │   ├─ 记录 assistant 消息（含 tool_calls）
  │   │
  │   └─ for tc in tool_calls:
  │       ├─ tool_engine.execute(tc) → ToolResult
  │       ├─ 记录 tool 消息（含 result）
  │       ├─ yield KEEPALIVE                  ← SSE 心跳保活
  │       └─ hooks.run("after_tool_call")     ← 钩子：阶段转换、约束校验
  │
  └─ 达到 max_retries → yield 错误提示 → return  ⚠️ 安全退出
```

### 3.2 核心代码（逐行注释）

```python
class AgentLoop:
    def __init__(self, llm, tool_engine: ToolEngine, hooks: HookManager, max_retries: int = 3):
        self.llm = llm
        self.tool_engine = tool_engine
        self.hooks = hooks
        self.max_retries = max_retries

    async def run(self, messages: list[Message], phase: int,
                  tools_override: list[dict] | None = None) -> AsyncIterator[LLMChunk]:

        tools = tools_override or self.tool_engine.get_tools_for_phase(phase)
        # ↑ 从第二层获取当前阶段的工具 schema

        for iteration in range(self.max_retries):
            await self.hooks.run("before_llm_call", messages=messages, phase=phase)

            tool_calls: list[ToolCall] = []
            text_chunks: list[str] = []

            # 流式调用 LLM（第四层）
            async for chunk in self.llm.chat(messages, tools=tools, stream=True):
                if chunk.type == ChunkType.TEXT_DELTA:
                    text_chunks.append(chunk.content or "")
                    yield chunk                          # 文本实时推给前端
                elif chunk.type == ChunkType.TOOL_CALL_START and chunk.tool_call:
                    tool_calls.append(chunk.tool_call)   # 收集工具调用
                    yield chunk                          # 工具调用信息也推给前端

            # 判断：LLM 是否给出了最终回答（无工具调用）
            if not tool_calls:
                full_text = "".join(text_chunks)
                if full_text:
                    messages.append(Message(role=Role.ASSISTANT, content=full_text))
                yield LLMChunk(type=ChunkType.DONE)
                return                                   # ✅ 循环结束

            # 记录 assistant 消息（包含工具调用信息，供下轮 LLM 参考）
            messages.append(Message(
                role=Role.ASSISTANT,
                content="".join(text_chunks) or None,
                tool_calls=tool_calls,
            ))

            # 逐个执行工具调用
            for tc in tool_calls:
                result = await self.tool_engine.execute(tc)   # 调用第二层
                messages.append(Message(role=Role.TOOL, tool_result=result))
                yield LLMChunk(type=ChunkType.KEEPALIVE)      # SSE 心跳
                await self.hooks.run("after_tool_call",
                                     tool_name=tc.name, tool_call=tc, result=result)

            # 循环继续 → LLM 将看到工具结果，决定下一步

        # 安全上限
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="[达到最大循环次数，请重新发送消息]")
        yield LLMChunk(type=ChunkType.DONE)
```

### 3.3 一次实际循环的消息演变

假设用户说"帮我搜一下东京的景点"，messages 列表的变化过程：

```
初始状态:
  [0] SYSTEM: "你是旅行规划助手...当前阶段2..."
  [1] USER: "帮我搜一下东京的景点"

── 第 1 次迭代 ──
LLM 返回: tool_use search_destinations(query="东京景点")
追加:
  [2] ASSISTANT: {content: null, tool_calls: [{name: "search_destinations", ...}]}
  [3] TOOL: {status: "success", data: {destinations: [...]}}

── 第 2 次迭代 ──
LLM 看到工具结果，返回纯文本总结
追加:
  [4] ASSISTANT: "我为您找到了以下东京景点：1. 浅草寺..."
→ 无 tool_calls → 循环结束 ✅
```

**设计要点**：
- **流式优先**：`yield chunk` 让前端实时看到 LLM 的思考过程和工具调用
- **SSE 心跳**：工具执行可能耗时数秒，`KEEPALIVE` 防止代理/浏览器断开连接
- **Hook 机制**：`before_llm_call` 做上下文压缩，`after_tool_call` 做阶段转换和约束校验
- **安全上限**：`max_retries` 防止 LLM 陷入无限工具调用循环

---

## 第四层：LLM 适配层

**文件**：`backend/llm/anthropic_provider.py`

这一层解决的问题：**如何将内部统一的数据结构转换为 Anthropic API 的特定格式，并解析流式响应？**

### 4.1 消息格式转换

内部使用统一的 `Message` 类型，但 Anthropic API 有自己的格式要求：

```python
def _split_system_and_convert(self, messages: list[Message]):
    system_parts: list[str] = []
    converted: list[dict] = []

    for msg in messages:
        if msg.role == Role.SYSTEM:
            # Anthropic 要求 system 作为独立参数，不在 messages 数组中
            system_parts.append(msg.content or "")

        elif msg.role == Role.TOOL and msg.tool_result:
            # 工具结果必须包装为 role="user" + type="tool_result"
            converted.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.tool_result.tool_call_id,
                    "content": json.dumps({
                        "status": msg.tool_result.status,
                        "data": msg.tool_result.data,
                    }, ensure_ascii=False),
                }],
            })

        elif msg.role == Role.ASSISTANT and msg.tool_calls:
            # 带工具调用的 assistant 消息 → text + tool_use 混合内容块
            content = []
            if msg.content:
                content.append({"type": "text", "text": msg.content})
            for tc in msg.tool_calls:
                content.append({
                    "type": "tool_use",
                    "id": tc.id, "name": tc.name, "input": tc.arguments,
                })
            converted.append({"role": "assistant", "content": content})

        else:
            converted.append({"role": msg.role.value, "content": msg.content or ""})

    return "\n\n".join(system_parts), converted
```

**转换对照表**：

| 内部格式 | Anthropic API 格式 |
|---------|-------------------|
| `Role.SYSTEM` 消息 | 提取为独立 `system` 参数 |
| `Role.TOOL` + tool_result | `role: "user"` + `type: "tool_result"` 内容块 |
| `Role.ASSISTANT` + tool_calls | `type: "tool_use"` 内容块 |
| 普通 USER/ASSISTANT | 直接映射 role + content |

### 4.2 工具 Schema 转换

```python
def _convert_tools(self, tool_defs: list[dict]) -> list[dict]:
    return [
        {"name": t["name"], "description": t["description"],
         "input_schema": t["parameters"]}    # parameters → input_schema
        for t in tool_defs
    ]
```

内部用 `parameters`（JSON Schema 通用叫法），Anthropic 要求 `input_schema`。一行映射搞定。

### 4.3 流式响应解析（核心难点）

Anthropic 的流式 tool_use 响应分三个阶段到达，需要状态机式的拼接：

```python
async with self.client.messages.stream(**kwargs) as stream_resp:
    current_tool_id: str | None = None
    current_tool_name: str | None = None
    current_tool_json: str = ""          # 拼接用的缓冲区

    async for event in stream_resp:
        if event.type == "content_block_start":
            if event.content_block.type == "tool_use":
                # ❶ 工具调用开始：记录 id 和 name
                current_tool_id = event.content_block.id
                current_tool_name = event.content_block.name
                current_tool_json = ""

        elif event.type == "content_block_delta":
            if hasattr(event.delta, "text"):
                # 文本块：直接 yield
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content=event.delta.text)
            elif hasattr(event.delta, "partial_json"):
                # ❷ 工具参数分块到达：拼接 JSON 片段
                current_tool_json += event.delta.partial_json

        elif event.type == "content_block_stop":
            if current_tool_id and current_tool_name:
                # ❸ 工具调用完整：解析拼接好的 JSON，yield 完整的 ToolCall
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
```

**流式 tool_use 的三阶段**：
```
content_block_start  → 拿到 tool id + name
content_block_delta  → 拼接 partial_json（可能有多个 delta）
content_block_stop   → json.loads 拼接结果，生成完整 ToolCall
```

这是 Anthropic 流式 API 最容易踩坑的地方：参数 JSON 不是一次性到达的，必须用缓冲区拼接后在 `content_block_stop` 时统一解析。

---

## 第五层：API 传输层

**文件**：`backend/main.py`

这一层解决的问题：**如何将 Agent 系统暴露为 HTTP 服务，管理会话状态，并通过 SSE 实现流式推送？**

这是整个系统的最外层，也是唯一直接面对前端的层。

### 5.1 会话管理

```python
sessions: dict[str, dict] = {}  # session_id → {plan, messages, agent}

@app.post("/api/sessions")
async def create_session():
    plan = await state_mgr.create_session()
    agent = _build_agent(plan)                    # 为每个会话创建独立的 Agent
    sessions[plan.session_id] = {
        "plan": plan,                              # 旅行计划状态
        "messages": [],                            # 对话历史
        "agent": agent,                            # AgentLoop 实例
    }
    return {"session_id": plan.session_id, "phase": plan.phase}
```

每个会话拥有独立的 `plan`、`messages` 和 `agent`，互不干扰。

### 5.2 Agent 构建与 Hook 注册

```python
def _build_agent(plan):
    llm = create_llm_provider(config.llm)
    tool_engine = ToolEngine()

    # 注册所有工具（每个工具通过工厂函数注入依赖）
    tool_engine.register(make_search_destinations_tool(config.api_keys))
    tool_engine.register(make_search_flights_tool(config.api_keys, flyai_client))
    tool_engine.register(make_calculate_route_tool(config.api_keys))
    # ... 共 13 个工具

    hooks = HookManager()

    # Hook 1: LLM 调用前 → 上下文压缩
    async def on_before_llm(**kwargs):
        msgs = kwargs.get("messages")
        threshold = int(config.llm.max_tokens * config.context_compression_threshold)
        if not context_mgr.should_compress(msgs, threshold):
            return
        # 将旧消息压缩为摘要，保留最近 4 条
        must_keep, compressible = context_mgr.classify_messages(msgs)
        summary = Message(role=Role.SYSTEM, content=f"[对话摘要]\n...")
        msgs.clear()
        # 重建：system + must_keep + summary + recent
        ...

    # Hook 2: 工具调用后 → 阶段自动转换
    async def on_tool_call(**kwargs):
        if kwargs.get("tool_name") == "update_plan_state":
            phase_router.check_and_apply_transition(plan)

    # Hook 3: 工具调用后 → 硬约束校验
    async def on_validate(**kwargs):
        if kwargs.get("tool_name") == "update_plan_state":
            errors = validate_hard_constraints(plan)
            if errors:
                session["messages"].append(
                    Message(role=Role.SYSTEM,
                            content=f"⚠️ 硬约束冲突：\n" + "\n".join(f"- {e}" for e in errors))
                )

    # Hook 4: 工具调用后 → Judge LLM 质量评估
    async def on_soft_judge(**kwargs):
        if kwargs.get("tool_name") not in ("assemble_day_plan", "generate_summary"):
            return
        prompt_text = build_judge_prompt(plan.to_dict(), prefs)
        judge_llm = create_llm_provider(config.llm)
        # ... 调用 Judge LLM 评分，将建议注入消息历史

    hooks.register("before_llm_call", on_before_llm)
    hooks.register("after_tool_call", on_tool_call)
    hooks.register("after_tool_call", on_validate)
    hooks.register("after_tool_call", on_soft_judge)

    return AgentLoop(llm=llm, tool_engine=tool_engine, hooks=hooks,
                     max_retries=config.max_retries)
```

**Hook 注册发生在第五层**，但 Hook 执行发生在第三层（Agent 循环中）。这种"注册与执行分离"的设计让业务逻辑（阶段转换、约束校验）与循环编排解耦。

### 5.3 聊天端点与 SSE 流式推送

```python
@app.post("/api/chat/{session_id}")
async def chat(session_id: str, req: ChatRequest):
    session = sessions.get(session_id)
    plan, messages, agent = session["plan"], session["messages"], session["agent"]

    # ① 隐式回退检测：用户说"换个目的地"→ 自动回退到阶段 2
    backtrack_target = _detect_backtrack(req.message, plan)
    if backtrack_target is not None:
        snapshot_path = await state_mgr.save_snapshot(plan)
        phase_router.prepare_backtrack(plan, backtrack_target, ...)
        session["agent"] = _build_agent(plan)    # 重建 Agent（工具集可能变了）
    else:
        # ② 提取旅行事实（日期、人数等）并检查阶段转换
        updated_fields = apply_trip_facts(plan, req.message)
        if updated_fields:
            phase_router.check_and_apply_transition(plan)

    # ③ 构建系统消息（阶段 prompt + 用户记忆 + 计划状态）
    sys_msg = context_mgr.build_system_message(plan, phase_prompt, user_summary)
    if messages and messages[0].role == Role.SYSTEM:
        messages[0] = sys_msg          # 替换旧的系统消息
    else:
        messages.insert(0, sys_msg)

    messages.append(Message(role=Role.USER, content=req.message))

    # ④ SSE 流式推送
    async def event_stream():
        async for chunk in agent.run(messages, phase=plan.phase):
            if chunk.type.value == "keepalive":
                yield {"comment": "ping"}      # SSE 注释，客户端忽略但连接保活
                continue

            event_data = {"type": chunk.type.value}
            if chunk.content:
                event_data["content"] = chunk.content
            if chunk.tool_call:
                event_data["tool_call"] = {
                    "name": chunk.tool_call.name,
                    "arguments": chunk.tool_call.arguments,
                }
            yield json.dumps(event_data, ensure_ascii=False)

        # ⑤ Agent 循环结束后，保存状态并推送最终计划
        await state_mgr.save(plan)
        yield json.dumps({"type": "state_update", "plan": plan.to_dict()}, ensure_ascii=False)

    return EventSourceResponse(event_stream())
```

### 5.4 隐式回退检测

```python
_BACKTRACK_PATTERNS: dict[int, list[str]] = {
    1: ["重新开始", "从头来", "换个需求"],
    2: ["换个目的地", "不想去这里", "不去了", "换地方"],
    3: ["改日期", "换时间", "日期不对"],
    4: ["换住宿", "不住这", "换个区域"],
}

def _detect_backtrack(message: str, plan: TravelPlanState) -> int | None:
    for target_phase, patterns in _BACKTRACK_PATTERNS.items():
        if target_phase >= plan.phase:    # 只能回退到更早的阶段
            continue
        if any(p in message for p in patterns):
            return target_phase
    return None
```

用户说"换个目的地"时，不需要显式调用回退 API，系统自动检测并回退到阶段 2。

### 5.5 前端收到的 SSE 事件流示例

```
data: {"type": "text_delta", "content": "好的，我来帮您搜索"}
data: {"type": "text_delta", "content": "东京的景点信息"}
data: {"type": "tool_call", "tool_call": {"name": "search_destinations", "arguments": {"query": "东京景点"}}}
: ping
: ping
data: {"type": "text_delta", "content": "我找到了以下景点：\n1. 浅草寺..."}
data: {"type": "done"}
data: {"type": "state_update", "plan": {"phase": 2, "destinations": [...]}}
```

注意 `: ping` 是 SSE 注释格式，浏览器 EventSource 会自动忽略，但它能防止连接超时。

---

## 涉及的数据类型

### agent/types.py — Agent 层核心类型

```python
class Role(str, Enum):
    SYSTEM = "system"       # 系统指令
    USER = "user"           # 用户输入
    ASSISTANT = "assistant" # LLM 回复
    TOOL = "tool"           # 工具执行结果

@dataclass
class ToolCall:
    id: str                 # Anthropic 生成的唯一 ID，用于关联 tool_result
    name: str               # 工具名，对应 ToolDef.name
    arguments: dict         # LLM 生成的参数，对应 JSON Schema

@dataclass
class ToolResult:
    tool_call_id: str       # 关联的 ToolCall.id
    status: str             # "success" | "error"
    data: Any = None        # 成功时的返回数据
    metadata: dict = None   # 附加元数据
    error: str = None       # 错误描述
    error_code: str = None  # 机器可读错误码
    suggestion: str = None  # 给 LLM 的修复建议

@dataclass
class Message:
    role: Role
    content: str = None           # 文本内容
    tool_calls: list[ToolCall] = None   # ASSISTANT 消息可能携带工具调用
    tool_result: ToolResult = None      # TOOL 消息携带执行结果
```

### llm/types.py — 流式传输类型

```python
class ChunkType(str, Enum):
    TEXT_DELTA = "text_delta"           # 文本流块
    TOOL_CALL_START = "tool_call_start" # 完整的工具调用
    TOOL_CALL_DELTA = "tool_call_delta" # 工具参数增量（预留）
    KEEPALIVE = "keepalive"             # SSE 心跳
    DONE = "done"                       # 响应完成

@dataclass
class LLMChunk:
    type: ChunkType
    content: str | None = None          # TEXT_DELTA 时的文本片段
    tool_call: ToolCall | None = None   # TOOL_CALL_START 时的完整调用
```

---

## 五层协作小结

| 层级 | 文件 | 核心职责 | 关键输入 | 关键输出 |
|------|------|---------|---------|---------|
| ① 工具定义 | `tools/base.py` + 各工具文件 | 定义工具的 schema 和执行逻辑 | 函数 + 元数据 | `ToolDef` 实例 |
| ② 工具执行 | `tools/engine.py` | 注册、阶段过滤、安全执行 | `ToolCall` | `ToolResult` |
| ③ Agent 循环 | `agent/loop.py` | ReAct 循环编排 | `messages` + `phase` | `AsyncIterator[LLMChunk]` |
| ④ LLM 适配 | `llm/anthropic_provider.py` | Anthropic API 格式转换与流式解析 | `Message[]` + `tools[]` | `AsyncIterator[LLMChunk]` |
| ⑤ API 传输 | `main.py` | HTTP/SSE、会话管理、Hook 注册 | `ChatRequest` | `EventSourceResponse` |

### 层间依赖关系

```
⑤ API 传输层
 ├─ 创建 ③ AgentLoop（注入 ④ LLM + ② ToolEngine + Hooks）
 ├─ 调用 ③ agent.run() 获取流式输出
 └─ 将 LLMChunk 转为 SSE 事件推送给前端

③ Agent 循环层
 ├─ 调用 ④ llm.chat() 获取 LLM 响应
 ├─ 调用 ② tool_engine.execute() 执行工具
 └─ 触发 Hooks（由 ⑤ 注册）

④ LLM 适配层
 └─ 将 ① 的 tool schema 转为 Anthropic 格式

② 工具执行层
 └─ 调用 ① 的 ToolDef 实例执行工具函数
```

每一层只依赖内层，不反向依赖——第一层（工具定义）完全独立，可以单独测试。
