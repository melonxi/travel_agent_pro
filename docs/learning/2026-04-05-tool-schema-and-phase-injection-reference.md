# Travel Agent Pro 工具 Schema 与阶段注入参考

## 背景

这份文档回答两个当前实现层面的问题：

- 工具传给模型时，实际包含哪些字段
- 各个阶段分别会向模型注入哪些工具

结论基于当前代码：

- 工具注册入口在 `backend/main.py`
- 阶段过滤逻辑在 `backend/tools/engine.py`
- 工具内部统一 schema 在 `backend/tools/base.py`
- provider 适配在 `backend/llm/openai_provider.py` 和 `backend/llm/anthropic_provider.py`

## 1. 工具传给模型时包含哪些字段

### 1.1 项目内部统一工具 schema

`ToolDef.to_schema()` 当前只导出 3 个顶层字段：

| 字段 | 是否传给模型 | 说明 |
| --- | --- | --- |
| `name` | 是 | 工具名 |
| `description` | 是 | 工具用途说明和使用时机说明 |
| `parameters` | 是 | JSON Schema 风格参数定义 |
| `phases` | 否 | 仅服务端用于按阶段过滤 |
| `_fn` | 否 | 工具实际执行函数，不暴露给模型 |
| 返回值 schema | 否 | 当前实现不会把出参 schema 传给模型 |

内部统一格式如下：

```python
{
    "name": self.name,
    "description": self.description,
    "parameters": self.parameters,
}
```

### 1.2 `parameters` 里是否包含参数解释

包含。

但“参数解释”不是单独的顶层字段，而是放在 `parameters` 这个 JSON Schema 里，常见结构如下：

| 位置 | 是否会传给模型 | 说明 |
| --- | --- | --- |
| `parameters.type` | 是 | 通常为 `"object"` |
| `parameters.properties` | 是 | 参数定义集合 |
| `parameters.required` | 是 | 必填参数列表 |
| `parameters.properties.<field>.type` | 是 | 参数类型 |
| `parameters.properties.<field>.description` | 是 | 参数解释 |
| `parameters.properties.<field>.enum` | 是 | 枚举约束，若定义 |
| `parameters.properties.<field>.default` | 是 | 默认值，若定义 |

也就是说，模型不仅能看到参数名，还能看到每个参数的解释、枚举和默认值。

例如：

```python
"query": {
    "type": "string",
    "description": "搜索关键词，如 '东京迪士尼门票价格 2026' 或 '日本签证最新政策'",
}
```

### 1.3 provider 最终如何传给模型

#### OpenAI

OpenAI provider 会把内部 schema 转成：

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

也就是说，OpenAI 侧实际传递的是：

| OpenAI 字段 | 来源 |
| --- | --- |
| `function.name` | 工具 `name` |
| `function.description` | 工具 `description` |
| `function.parameters` | 工具 `parameters` |

#### Anthropic

Anthropic provider 会把内部 schema 转成：

```python
{
    "name": t["name"],
    "description": t["description"],
    "input_schema": t["parameters"],
}
```

也就是说，Anthropic 侧实际传递的是：

| Anthropic 字段 | 来源 |
| --- | --- |
| `name` | 工具 `name` |
| `description` | 工具 `description` |
| `input_schema` | 工具 `parameters` |

## 2. 工具注入链路

当前实现是先注册全部工具，再按阶段过滤。

| 步骤 | 位置 | 作用 |
| --- | --- | --- |
| 工具定义 | `backend/tools/*.py` | 定义 `name`、`description`、`phases`、`parameters` |
| 工具注册 | `backend/main.py` | 把工具注册进 `ToolEngine` |
| 阶段过滤 | `backend/tools/engine.py` | 通过 `phase in t.phases` 过滤当前阶段工具 |
| 注入模型 | `backend/agent/loop.py` | `self.tool_engine.get_tools_for_phase(current_phase)` |
| provider 转换 | `backend/llm/*.py` | 转成 OpenAI/Anthropic 的目标格式 |

## 3. 当前注册的 15 个工具

当前 `_build_agent()` 一共注册了 15 个工具：

| 工具名 | phases |
| --- | --- |
| `update_plan_state` | `1, 2, 3, 4, 5, 7` |
| `search_destinations` | `1, 2` |
| `check_feasibility` | `2` |
| `search_flights` | `3, 4` |
| `search_accommodations` | `3, 4` |
| `get_poi_info` | `3, 4, 5` |
| `calculate_route` | `4, 5` |
| `assemble_day_plan` | `4, 5` |
| `check_availability` | `4, 5` |
| `check_weather` | `5, 7` |
| `generate_summary` | `7` |
| `quick_travel_search` | `2, 3` |
| `search_travel_services` | `7` |
| `web_search` | `1, 2, 3` |
| `xiaohongshu_search` | `1, 2, 3, 4, 5, 7` |

## 4. 各阶段分别注入哪些工具

### 4.1 阶段总表

| 阶段 | 当前注入工具 |
| --- | --- |
| Phase 1 | `update_plan_state`, `search_destinations`, `web_search`, `xiaohongshu_search` |
| Phase 2 | `update_plan_state`, `search_destinations`, `check_feasibility`, `quick_travel_search`, `web_search`, `xiaohongshu_search` |
| Phase 3 | `update_plan_state`, `search_flights`, `search_accommodations`, `get_poi_info`, `quick_travel_search`, `web_search`, `xiaohongshu_search` |
| Phase 4 | `update_plan_state`, `search_flights`, `search_accommodations`, `get_poi_info`, `calculate_route`, `assemble_day_plan`, `check_availability`, `xiaohongshu_search` |
| Phase 5 | `update_plan_state`, `get_poi_info`, `calculate_route`, `assemble_day_plan`, `check_availability`, `check_weather`, `xiaohongshu_search` |
| Phase 6 | 当前实现不存在这个阶段，因此没有注入工具 |
| Phase 7 | `update_plan_state`, `check_weather`, `generate_summary`, `search_travel_services`, `xiaohongshu_search` |

### 4.2 逐阶段说明

#### Phase 1

目标偏向信息收集和需求澄清，因此只有：

- `update_plan_state`
- `search_destinations`
- `web_search`
- `xiaohongshu_search`

#### Phase 2

目标偏向目的地推荐和可行性验证，因此在 Phase 1 基础上增加：

- `search_destinations`
- `check_feasibility`
- `quick_travel_search`

#### Phase 3

目标偏向出行方案搜索，因此注入：

- `search_flights`
- `search_accommodations`
- `get_poi_info`
- `quick_travel_search`
- `web_search`
- `xiaohongshu_search`
- `update_plan_state`

#### Phase 4

目标偏向住宿和行程拼装，因此注入：

- `search_flights`
- `search_accommodations`
- `get_poi_info`
- `calculate_route`
- `assemble_day_plan`
- `check_availability`
- `xiaohongshu_search`
- `update_plan_state`

#### Phase 5

目标偏向细化日程与临近出发确认，因此注入：

- `get_poi_info`
- `calculate_route`
- `assemble_day_plan`
- `check_availability`
- `check_weather`
- `xiaohongshu_search`
- `update_plan_state`

#### Phase 7

目标偏向收尾和成稿，因此注入：

- `generate_summary`
- `search_travel_services`
- `check_weather`
- `xiaohongshu_search`
- `update_plan_state`

## 5. 关于 Phase 6

当前实现里没有 phase 6。

`PhaseRouter.infer_phase()` 只会返回：

- `1`
- `2`
- `3`
- `4`
- `5`
- `7`

因此，`phase 6` 不是“存在但没配工具”，而是整个路由链路里当前就没有这个阶段。

## 6. 最简结论

- 传给模型的工具核心只有 `name`、`description`、`parameters`
- 参数解释会传，位置在 `parameters.properties.<field>.description`
- `phases`、执行函数 `_fn`、返回值 schema 不会传给模型
- 当前实现只存在 `Phase 1/2/3/4/5/7`
- Phase 1 当前注入 4 个工具：`update_plan_state`、`search_destinations`、`web_search`、`xiaohongshu_search`
