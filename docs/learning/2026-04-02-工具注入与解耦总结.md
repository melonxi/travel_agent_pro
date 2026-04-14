# Travel Agent Pro 工具注入与解耦对话总结

## 背景

本次对话围绕 Travel Agent Pro 当前工具调用链展开，目标是把下面几件事讲清楚：

- Phase 1 到底注入了哪些工具
- 工具是在什么时机、通过什么链路传到 LLM 的 `tools` 参数里
- 传给模型的工具信息包含什么，不包含什么
- OpenAI SDK 对 `tools` 的原生要求是什么
- 当前项目为什么拆成多步组装
- `@tool(...)`、`to_schema()`、provider adapter 各自解决什么问题
- 耦合是怎么产生的，当前代码又是怎么做解耦的

---

## 结论概览

- Phase 1 实际只暴露了一个工具：`update_plan_state`
- 工具不是写在 prompt 里“注入”的，而是在 `agent.run(messages, phase=plan.phase)` 时按当前 phase 动态筛出来，再传给 LLM
- 传给模型的工具元信息核心只有 `name`、`description`、`parameters`
- 当前工具参数不是用 Pydantic `Field(...)` 定义的，而是手写 JSON Schema 风格字典
- OpenAI `chat.completions.create(..., tools=...)` 需要的最终结构是 `{type: "function", function: {...}}`
- 当前项目的五步链路，本质是在分离“工具定义、工具注册、阶段裁剪、内部统一表示、provider 适配”五种职责
- `to_schema()` 是一层很薄的边界，不是强抽象，但它仍然有价值：它限制 provider 只依赖工具的公共导出格式，而不是整个 `ToolDef` 内部结构

---

## 1. Phase 1 注入了哪些工具

Phase 1 只会注入 `update_plan_state`。

原因很简单：所有工具定义里只有 `update_plan_state` 的 `phases` 包含 `1`：

- `update_plan_state`: `phases=[1, 2, 3, 4, 5, 7]`
- 其他工具如 `search_destinations`、`search_flights`、`search_accommodations`、`generate_summary` 等都不包含 `1`

这意味着在 Phase 1，模型能调用的能力只有“把用户新提供的信息写入计划状态”，不能直接搜目的地、机票、酒店，也不能做最终摘要。

---

## 2. 工具真正是在什么时机注入的

工具注入发生在每次请求进入 agent loop 时，不是定义 prompt 的时候。

调用链是：

1. API 收到用户消息
2. 根据 `plan.phase` 构建系统提示词
3. 调用 `agent.run(messages, phase=plan.phase)`
4. `AgentLoop.run()` 内部执行：
   - `tools = self.tool_engine.get_tools_for_phase(phase)`
   - `self.llm.chat(messages, tools=tools, stream=True)`

所以工具是否可见，是由本轮 `run()` 开始时的 `phase` 决定的。

一个重要细节：

- 一次 `agent.run()` 里，工具列表只在开始时取一次
- 如果本轮中途 `update_plan_state` 改变了 `plan.phase`，当前这轮不会热更新工具集
- 下一次请求进来时，才会按新的 phase 重新筛工具

---

## 3. 传给模型的工具信息包含什么

在项目内部，工具最终先会被导出成一个统一结构：

```python
{
    "name": ...,
    "description": ...,
    "parameters": ...,
}
```

这里的含义是：

- `name`: 工具名
- `description`: 工具用途说明
- `parameters`: JSON Schema 风格的参数结构

不包含的内容：

- `phases`
- `_fn`（真正执行的函数）
- 任何运行时对象，比如 `plan`

也就是说，模型看到的是“可调用接口说明”，看不到实现细节。

---

## 4. `field` 这种参数的解释会不会传给模型

会传。

例如 `update_plan_state` 的参数定义中：

```python
"field": {
    "type": "string",
    "description": "要更新的字段名。可选值：..."
}
```

这段 `description` 会作为 `parameters` 的一部分一起传给模型。

但这里要区分两件事：

- 会传给模型：是的
- 是不是强约束：不完全是

因为当前 `field` 只是：

- `type: "string"`
- `description: "...可选值..."`

它没有使用 `enum` 限死取值，所以这更像提示，而不是 schema 级别的硬约束。真正的兜底校验发生在工具执行时，代码会检查 `field` 是否在 `_ALLOWED_FIELDS` 里。

---

## 5. 这里有没有类似 Pydantic `Field(...)` 的机制

当前没有。

本项目工具参数定义不是：

- `BaseModel`
- `Field(...)`
- `model_json_schema()`

而是手写 JSON Schema 风格的 Python 字典，例如：

```python
_PARAMETERS = {
    "type": "object",
    "properties": {...},
    "required": [...],
}
```

所以它在效果上有点像 `Field(description="...")`，但实现方式不是 Pydantic。

---

## 6. OpenAI SDK 里 `tools` 字段要求什么

如果使用的是当前项目这条线上的 `chat.completions.create(...)`，那么 `tools` 每一项应长这样：

```python
{
    "type": "function",
    "function": {
        "name": "tool_name",
        "description": "...",
        "parameters": {...},  # JSON Schema
        # "strict": True,     # 可选
    },
}
```

最关键的要求是：

- `type` 必须是 `"function"`
- `function.name` 必填
- `function.description` 可选但建议传
- `function.parameters` 可选；如果传，就应是 JSON Schema
- `function.strict` 可选

当前项目在 OpenAI provider 里，正是把内部工具格式再包成这套结构后传给 SDK。

---

## 7. 当前项目把工具传到 OpenAI `tools` 的五步链路

### 第一步：定义工具

使用 `@tool(...)` 把函数和元信息绑在一起，生成 `ToolDef`：

- `name`
- `description`
- `phases`
- `parameters`
- `_fn`

### 第二步：注册工具

在 `_build_agent(plan)` 中，把所有工具注册到 `ToolEngine`。

这一步只是在建立“工具目录”，还没有按 phase 过滤，也还没有传给 LLM。

### 第三步：按 phase 过滤

在 `AgentLoop.run(..., phase=...)` 里调用：

```python
self.tool_engine.get_tools_for_phase(phase)
```

这里只保留当前阶段可见的工具。

### 第四步：转成项目内部统一 schema

`ToolDef.to_schema()` 导出：

```python
{
    "name": ...,
    "description": ...,
    "parameters": ...,
}
```

这是项目内部的中间表示。

### 第五步：provider 适配

到 OpenAI provider 才做最终转换：

```python
{
    "type": "function",
    "function": {
        "name": t["name"],
        "description": t["description"],
        "parameters": t["parameters"],
    },
}
```

Anthropic provider 则会把同一个内部结构转换成：

```python
{
    "name": t["name"],
    "description": t["description"],
    "input_schema": t["parameters"],
}
```

---

## 8. 为什么拆成这五步

这五步不是为了“流程感”，而是在隔离不同职责：

1. 定义工具
2. 收集工具
3. 按阶段裁剪工具
4. 统一成项目内部格式
5. 适配具体 SDK

这样每种变化都能局部收敛：

- 业务工具逻辑变了：改工具函数
- 工具元信息变了：改 `@tool(...)`
- phase 策略变了：改 `get_tools_for_phase`
- 内部 schema 变了：改 `to_schema()`
- OpenAI / Anthropic SDK 变了：改 provider adapter

---

## 9. 第一步 `@tool(...)` 到底是不是必要

不是强必要。

不用装饰器，也可以手写 `ToolDef(...)`，同样能组成工具列表。

装饰器真正的价值不是“没有它就组不成列表”，而是：

- 在定义函数的地方就把元信息绑定好
- 减少手动样板代码
- 降低“函数实现”和“工具元信息”不同步的风险

更准确地说：

- 第一步是在做 declaration
- 第二步是在做 registration
- 第三步是在做 selection

所以第一步的主要意义是“绑定元信息”，而“方便组成列表、方便按 phase 过滤”是这个绑定带来的直接收益。

---

## 10. 第四步 `to_schema()` 看起来为什么薄，但仍然有意义

这是本次对话里最容易产生误解的点。

`to_schema()` 的确很薄，它不是一个特别重的抽象层。即使删掉它，系统也不一定会立刻变糟。

但它仍然在做一件具体事情：

- 把 `ToolDef` 的完整内部结构
- 收窄成 provider 只该看到的最小公开表示

`ToolDef` 内部有：

- `name`
- `description`
- `phases`
- `parameters`
- `_fn`

而 provider 真正需要知道的只有：

- `name`
- `description`
- `parameters`

所以 `to_schema()` 的价值不是“完全解耦工具层和 provider”，而是：

**避免 provider 直接依赖 `ToolDef` 的全部内部字段和内部形状。**

这是一种“弱边界”，不是“强隔离”。

---

## 11. 耦合是怎么产生的

耦合的根源不是“出现了依赖”，而是：

**某一层的变化，会把修改需求扩散到本来不该关心的其他层。**

举例：

如果工具定义直接长成 OpenAI 需要的样子：

```python
{
    "type": "function",
    "function": {...}
}
```

那么工具层就已经知道 OpenAI 的 SDK 协议了。

后果是：

- 如果改到 Anthropic
- 或 OpenAI SDK 结构升级
- 你就得回头改工具层本身

这就是 provider 协议向上游扩散造成的耦合。

---

## 12. 当前项目是怎么解耦的

当前项目不是把依赖消灭掉，而是把依赖限制在边界上。

具体做法是：

1. 工具层只产出项目内部通用表示：

```python
{
    "name": ...,
    "description": ...,
    "parameters": ...,
}
```

2. 各 provider 各自负责把这个通用表示翻译成外部 SDK 需要的格式

所以：

- 工具层只回答“工具是什么”
- provider 层只回答“这个 SDK 需要什么结构”

这样一来，如果 OpenAI 的 SDK 变了，理论上主要改 `openai_provider.py`；如果 Anthropic 变了，主要改 `anthropic_provider.py`；工具定义层和 phase 过滤层不必跟着一起震荡。

这就是当前项目里的“解耦”。

---

## 13. 一句话总结

这套设计的核心不是“把流程搞复杂”，而是把下面三件事分开：

- 工具是什么
- 当前阶段允许用哪些工具
- 某家 LLM SDK 需要什么工具格式

其中：

- `@tool(...)` 负责把函数和元信息绑定起来
- `ToolEngine` 负责收集和筛选
- `to_schema()` 负责导出最小公共表示
- provider adapter 负责翻译成 OpenAI / Anthropic 的协议格式

最简洁的抽象可以写成：

- declaration：定义工具
- registration：注册工具
- selection：按阶段选择工具
- export：导出内部统一 schema
- adaptation：适配外部 provider SDK

---

## 14. 本次对话最重要的认识变化

本次讨论最终澄清了三个容易混淆的问题：

1. `@tool(...)` 不是必需能力，而是声明工具的方便写法
2. `to_schema()` 不是强解耦，但它是一个有价值的公开边界
3. 真正关键的解耦点，不在于“完全没有依赖”，而在于“变化只在边界层收敛，不向全链路扩散”

