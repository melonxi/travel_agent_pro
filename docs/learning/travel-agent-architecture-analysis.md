# Travel Agent Pro 全阶段架构分析报告

> 分析日期：2026-03-30
> 文档版本：v1.0
> 分析范围：后端 Agent 系统全流程（阶段 1→2→3→4→5→7）

---

## 目录

1. [系统总览](#1-系统总览)
2. [阶段总览表](#2-阶段总览表)
3. [系统消息构建机制](#3-系统消息构建机制)
4. [各阶段详细分析](#4-各阶段详细分析)
5. [工具系统详解](#5-工具系统详解)
6. [阶段转换机制](#6-阶段转换机制)
7. [回溯机制](#7-回溯机制)
8. [质量保障系统](#8-质量保障系统)
9. [上下文压缩机制](#9-上下文压缩机制)
10. [事实提取预处理机制](#10-事实提取预处理机制)
11. [用户记忆系统](#11-用户记忆系统)
12. [核心流程图](#12-核心流程图)
13. [关键文件索引](#13-关键文件索引)

---

## 1. 系统总览

Travel Agent Pro 是一个多阶段旅行规划 Agent，采用 **状态驱动的阶段路由** 架构。系统的核心设计哲学是：

- **状态决定阶段**：不是硬编码的流程，而是根据 `TravelPlanState` 中的字段填充情况自动推断当前阶段
- **阶段决定能力**：每个阶段有独立的 Prompt、工具集和控制模式
- **工具驱动状态变更**：所有状态变更必须通过 `update_plan_state` 工具完成，确保变更可追踪
- **钩子系统串联逻辑**：阶段转换、约束验证、质量评估都通过钩子（Hooks）在工具调用前后触发

### 技术栈

| 层级 | 技术 |
|------|------|
| 后端框架 | FastAPI + SSE (Server-Sent Events) |
| LLM 提供商 | OpenAI / Anthropic（可切换） |
| HTTP 客户端 | httpx（异步） |
| 外部 API | Google Maps/Places, Amadeus, OpenWeather |
| 前端 | React 19 + TypeScript + Vite + Leaflet 地图 |
| 数据持久化 | JSON 文件系统（`data/sessions/`、`data/users/`） |

### 核心模块关系

```
┌─────────────────────────────────────────────────────────┐
│                      main.py (FastAPI)                   │
│  ┌──────────┐  ┌─────────────┐  ┌────────────────────┐  │
│  │ 回溯检测  │  │ 事实提取     │  │ 上下文构建          │  │
│  │ (正则)    │  │ (intake.py) │  │ (ContextManager)   │  │
│  └────┬─────┘  └──────┬──────┘  └─────────┬──────────┘  │
│       │               │                    │             │
│  ┌────▼─────────────────────────────────────▼──────────┐ │
│  │              AgentLoop (loop.py)                     │ │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │ │
│  │  │ LLM 调用  │  │ 工具执行  │  │ HookManager      │   │ │
│  │  │ (流式)    │  │ (Engine) │  │ before/after     │   │ │
│  │  └──────────┘  └──────────┘  └──────────────────┘   │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ PhaseRouter  │  │ StateManager │  │ MemoryManager │  │
│  │ (阶段推断)   │  │ (状态持久化)  │  │ (用户画像)    │  │
│  └──────────────┘  └──────────────┘  └───────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## 2. 阶段总览表

系统定义了 **6 个阶段**（编号 1/2/3/4/5/7，跳过 6），每个阶段有独立的角色定位、控制模式和工具集：

| 阶段 | 名称 | 角色定位 | 控制模式 | 可用工具（运行时实际生效） | 目标产出 | 前置条件 |
|------|------|---------|----------|---------|---------|---------|
| **1** | 灵感探索 | 旅行灵感顾问 | `conversational` | `update_plan_state` | 用户偏好列表 | 无（初始状态） |
| **2** | 目的地选择 | 目的地推荐专家 | `agent_with_guard` | `search_destinations`, `check_feasibility`, `update_plan_state` | 确定目的地 | 用户已表达偏好 |
| **3** | 天数与节奏 | 行程节奏规划师 | `workflow` | `search_flights`, `search_accommodations`, `get_poi_info`, `update_plan_state` | 确定日期 | 目的地已确定 |
| **4** | 住宿区域 | 住宿区域顾问 | `conversational` | `search_flights`, `search_accommodations`, `get_poi_info`, `calculate_route`, `assemble_day_plan`, `check_availability`, `update_plan_state` | 确定住宿 | 日期已确定 |
| **5** | 行程组装 | 行程组装引擎 | `structured` | `get_poi_info`, `calculate_route`, `assemble_day_plan`, `check_availability`, `check_weather`, `update_plan_state` | 完整日程 | 住宿已确定 |
| **7** | 出发前查漏 | 查漏清单生成器 | `evaluator` | `check_weather`, `generate_summary`, `update_plan_state` | 出行清单 | 所有天日程完成 |

### 控制模式含义

| 控制模式 | 含义 | 行为特征 |
|---------|------|---------|
| `conversational` | 对话式 | 以开放式提问引导用户，不主动推进 |
| `agent_with_guard` | 带护栏的 Agent | 可主动使用工具搜索，但决策权归用户 |
| `workflow` | 工作流式 | 按步骤收集信息，输出结构化结果 |
| `structured` | 结构化 | 严格遵循约束，工具密集调用 |
| `evaluator` | 评估式 | 生成检查清单，逐项验证 |

---

## 3. 系统消息构建机制

每次用户发送消息时，系统会重新构建系统消息（System Message），由 `ContextManager` 负责：

### 组成结构

```
┌─────────────────────────────────────────┐
│  ① Soul（Agent 身份 / soul.md）         │
├─────────────────────────────────────────┤
│  ② 当前阶段指引（Phase Prompt）          │
├─────────────────────────────────────────┤
│  ③ 当前规划状态（Runtime Context）       │
│    - 阶段编号                            │
│    - 目的地（若有）                      │
│    - 日期范围（若有）                    │
│    - 预算 + 已分配金额                   │
│    - 住宿区域（若有）                    │
│    - 已规划天数 / 总天数                 │
│    - 最近回溯事件（若有）                │
├─────────────────────────────────────────┤
│  ④ 用户画像（User Profile）              │
│    - 显式偏好                            │
│    - 出行历史 + 满意度                   │
│    - 永久排除项                          │
└─────────────────────────────────────────┘
```

### Soul（Agent 身份）

来源文件：`backend/context/soul.md`

```markdown
你是一个专业的旅行规划 Agent，帮助用户完成从模糊意愿到出发前查漏的全流程规划。

核心行为约束：
- 不替用户做情感决策（目的地最终由用户拍板）
- 所有涉及支付的操作必须用户确认
- 行程建议必须附带时间/距离/成本的量化数据
- 回溯时说明原因和影响范围，不要静默重排
- 所有事实性信息必须来自工具返回，不允许从记忆中回忆

交互风格：
- 一次只问一个问题
- 给出建议时提供 2-3 个选项
- 使用具体数据支撑建议
- 保持友好但专业的语气
```

---

## 4. 各阶段详细分析

### 4.1 阶段 1：灵感探索

**角色**：旅行灵感顾问
**控制模式**：`conversational`（纯对话式）

#### Prompt 全文

```
你现在是旅行灵感顾问。用户可能只有模糊的想法（"想去海边""想放松"）。
你的任务是通过开放式提问帮用户具象化需求，不要急于给出目的地建议。
关注：出行动机、同行人、时间窗口、预算范围。
一次只问一个问题，保持耐心和热情。
```

#### 可用工具

| 工具名 | 用途 |
|--------|------|
| `update_plan_state` | 记录用户提到的偏好信息 |

#### 核心任务

- 通过开放式提问引导用户表达模糊意愿
- 收集四大维度：出行动机、同行人、时间窗口、预算范围
- 将收集到的信息通过 `update_plan_state` 写入 `preferences` 字段

#### 转出条件

当 `plan.preferences` 列表非空时（用户已表达至少一个偏好），系统自动推断进入阶段 2。

#### 设计要点

- 控制模式是 `conversational`，意味着 Agent 不应主动搜索或推荐
- "一次只问一个问题"是关键指令，防止信息轰炸
- 这是唯一一个没有外部 API 工具的阶段

---

### 4.2 阶段 2：目的地选择

**角色**：目的地推荐专家
**控制模式**：`agent_with_guard`（带护栏的 Agent）

#### Prompt 全文

```
你现在是目的地推荐专家。基于用户的意愿，推荐 2-3 个目的地候选。
每个候选必须附带：季节适宜度、预算估算、签证要求、与用户偏好的匹配度。
最终目的地由用户拍板，你只提供信息和建议，不替用户做决定。
如果用户已经明确了目的地，确认后直接进入下一步。
```

#### 可用工具

| 工具名 | 参数 | 外部 API | 用途 |
|--------|------|----------|------|
| `search_destinations` | `query`, `preferences` | Google Places | 搜索匹配的目的地候选 |
| `check_feasibility` | `destination`, `travel_date` | OpenWeather | 检查天气和可行性 |
| `update_plan_state` | `field`, `value` | 无 | 记录确定的目的地 |

#### 核心任务

- 基于阶段 1 收集的偏好搜索目的地
- 为每个候选提供多维度评估（季节、预算、签证、匹配度）
- 等待用户拍板后将 `destination` 写入状态

#### 转出条件

当 `plan.destination` 非空时（用户确认了目的地），系统自动推断进入阶段 3。

#### 设计要点

- `agent_with_guard` 模式：Agent 可以主动调用工具搜索，但"护栏"体现在 Prompt 中——"不替用户做决定"
- 有一个快速路径：如果用户一开始就说"我要去东京"，可以跳过搜索直接确认

---

### 4.3 阶段 3：天数与节奏

**角色**：行程节奏规划师
**控制模式**：`workflow`（工作流式）

#### Prompt 全文

```
你现在是行程节奏规划师。目的地已确定，需要确定出行日期和整体节奏。
基于目的地特点和用户偏好（每天景点数、步行耐受度），给出天数建议。
需要确认：具体出发和返回日期、每日可用时间、必去景点列表。
输出为结构化的约束清单。
```

#### 可用工具

| 工具名 | 参数 | 外部 API | 用途 |
|--------|------|----------|------|
| `search_flights` | `origin`, `destination`, `date`, `max_results` | Amadeus | 搜索航班 |
| `update_plan_state` | `field`, `value` | 无 | 记录日期和约束 |

#### 核心任务

- 建议合理的出行天数
- 确认具体日期范围
- 搜索航班信息辅助决策
- 收集用户的节奏偏好（每天几个景点、步行耐受度等）
- 输出结构化的约束清单

#### 转出条件

当 `plan.dates` 非空时（日期已确定），系统自动推断进入阶段 4。

#### 设计要点

- `workflow` 模式要求按步骤推进，产出结构化结果
- 航班搜索是可选的，有些旅行不需要飞行

---

### 4.4 阶段 4：住宿区域

**角色**：住宿区域顾问
**控制模式**：`conversational`（对话式）

#### Prompt 全文

```
你现在是住宿区域顾问。根据行程安排推荐住宿区域。
综合考虑：到主要景点的交通便利度、区域安全性、性价比、周边餐饮选择。
推荐 2-3 个区域候选，附带每个区域的优劣分析和推荐住宿类型。
```

#### 可用工具

| 工具名 | 参数 | 外部 API | 用途 |
|--------|------|----------|------|
| `search_accommodations` | `destination`, `check_in`, `check_out`, `budget_per_night`, `area`, `requirements` | Google Places | 搜索住宿 |
| `calculate_route` | `origin_lat/lng`, `dest_lat/lng`, `mode` | Google Directions | 计算住宿到景点的距离 |
| `update_plan_state` | `field`, `value` | 无 | 记录住宿信息 |

#### 核心任务

- 推荐 2-3 个住宿区域候选
- 每个区域附带优劣分析
- 可用路线计算工具辅助分析交通便利度
- 确认住宿区域后写入状态

#### 转出条件

当 `plan.accommodation` 非空时（住宿已确定），系统自动推断进入阶段 5。

#### 设计要点

- 回到 `conversational` 模式，因为住宿选择涉及个人偏好
- `calculate_route` 工具在这里首次可用，用于计算住宿到景点的通勤距离

---

### 4.5 阶段 5：行程组装

**角色**：行程组装引擎
**控制模式**：`structured`（结构化）

#### Prompt 全文

```
你现在是行程组装引擎。把景点、餐厅、交通组装成按天的具体行程。
每个活动必须有：开始时间、结束时间、地点、交通方式和耗时、预估费用。
必须通过硬约束验证：时间不冲突、交通可达、营业时间内、预算不超限。
每天的行程应有主题感，地理上尽量集中以减少交通时间。
使用 assemble_day_plan 工具来生成优化的单日行程。
```

#### 可用工具

| 工具名 | 参数 | 外部 API | 用途 |
|--------|------|----------|------|
| `get_poi_info` | `query`, `location` | Google Places | 搜索景点详情 |
| `calculate_route` | 坐标对 + `mode` | Google Directions | 计算景点间路线 |
| `assemble_day_plan` | `pois`, `start_time`, `end_time`, `max_walk_km` | 无（本地算法） | 贪心算法排序景点 |
| `check_availability` | `place_name`, `date` | Google Places | 查询景点是否开放 |
| `update_plan_state` | `field`, `value` | 无 | 写入日程 |

#### 核心任务

- 搜索景点详细信息（坐标、类型）
- 检查景点在特定日期是否开放
- 用 `assemble_day_plan` 的贪心最近邻算法优化景点顺序
- 计算景点间的具体路线和时间
- 组装每天的完整日程（时间、地点、交通、费用）
- 每天日程需通过硬约束验证

#### `assemble_day_plan` 算法详解

```
输入: POIs 列表（含坐标和游览时长）
算法: 贪心最近邻
  1. 取第一个 POI 作为起点
  2. 从剩余 POI 中找到距离最近的（Haversine 公式）
  3. 加入有序列表，重复直到所有 POI 排完
  4. 累计总步行距离
  5. 估算总时间 = Σ游览时长 + 总距离 × 0.25h/km
输出: 排序后的 POIs + 总距离 + 估计时长
```

#### 转出条件

当 `len(plan.daily_plans) >= plan.dates.total_days` 时（所有天的日程都已规划），系统自动推断进入阶段 7。

#### 设计要点

- 这是工具调用最密集的阶段，拥有 5 个工具
- `structured` 模式要求严格遵循约束
- 硬约束验证在 `update_plan_state` 的 `after_tool_call` 钩子中触发
- 软约束评分在 `assemble_day_plan` 完成后触发

---

### 4.6 阶段 7：出发前查漏

**角色**：出发前查漏清单生成器
**控制模式**：`evaluator`（评估式）

#### Prompt 全文

```
你现在是出发前查漏清单生成器。针对已确认的行程，生成完整的出行检查清单。
包含：证件准备、货币兑换、天气对应衣物、已规划项目的注意事项、紧急联系方式、目的地实用贴士。
使用 check_weather 获取最新天气，使用 generate_summary 生成出行摘要。
逐项检查，确保没有遗漏。
```

#### 可用工具

| 工具名 | 参数 | 外部 API | 用途 |
|--------|------|----------|------|
| `check_weather` | `city`, `date` | OpenWeather | 获取天气预报 |
| `generate_summary` | `plan_data` | 无（本地） | 生成行程摘要 |
| `update_plan_state` | `field`, `value` | 无 | 补充信息 |

#### 核心任务

- 获取目的地天气预报
- 生成完整的出行检查清单
- 生成行程摘要
- 清单涵盖：证件、货币、衣物、注意事项、紧急联系方式

#### 设计要点

- `evaluator` 模式：逐项检查，确保无遗漏
- 这是最终阶段，没有后续转换
- 注意阶段编号为 7，跳过了 6（可能是预留给"预订确认"阶段）

---

## 5. 工具系统详解

### 5.1 工具架构

工具系统由三层组成：

```
┌──────────────────────────────────────────┐
│  ToolDef（工具定义）                      │
│  - name, description, phases, parameters │
│  - _fn（异步可调用函数）                  │
│  - @tool 装饰器生成                      │
├──────────────────────────────────────────┤
│  ToolEngine（工具引擎）                   │
│  - register()   注册工具                 │
│  - get_tools_for_phase()  按阶段过滤     │
│  - execute()    执行工具调用              │
├──────────────────────────────────────────┤
│  ToolCall / ToolResult（调用和结果类型）   │
│  - ToolCall: id, name, arguments         │
│  - ToolResult: tool_call_id, status,     │
│    data, error, error_code, suggestion   │
└──────────────────────────────────────────┘
```

### 5.2 工具注册方式

每个工具使用 **工厂函数模式** 创建，通过闭包绑定依赖（如 API Key 或 Plan State）：

```python
# 绑定外部依赖的工具
def make_search_destinations_tool(api_keys: ApiKeysConfig):
    @tool(name="search_destinations", phases=[2], ...)
    async def search_destinations(query, preferences):
        # 使用闭包中的 api_keys
        ...
    return search_destinations

# 绑定状态的工具
def make_update_plan_state_tool(plan: TravelPlanState):
    @tool(name="update_plan_state", phases=[1,2,3,4,5,7], ...)
    async def update_plan_state(field, value):
        # 直接修改闭包中的 plan 对象
        ...
    return update_plan_state
```

### 5.3 工具阶段门控

`ToolEngine.get_tools_for_phase(phase)` 只返回 `phase in tool.phases` 的工具 Schema，其余工具对 LLM 不可见：

**重要说明**：系统中存在两套工具-阶段映射。`PHASE_TOOL_NAMES`（在 `prompts.py` 中）定义了每个阶段"配置级"的工具列表，而各工具文件中的 `phases` 参数是 `ToolEngine` 运行时实际使用的过滤条件。当前代码中 `PhaseRouter.get_tool_names()` **从未被调用**，`PHASE_TOOL_NAMES` 实际上是死代码。以下表格基于各工具文件中的 `phases` 参数（即运行时实际生效的规则）：

| 工具名 | 阶段 1 | 阶段 2 | 阶段 3 | 阶段 4 | 阶段 5 | 阶段 7 |
|--------|:------:|:------:|:------:|:------:|:------:|:------:|
| update_plan_state | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| search_destinations | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| check_feasibility | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| search_flights | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ |
| search_accommodations | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ |
| get_poi_info | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ |
| calculate_route | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ |
| assemble_day_plan | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ |
| check_availability | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ |
| check_weather | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| generate_summary | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |

> 注意：`PHASE_TOOL_NAMES` 中的配置与上表存在差异（例如 `search_flights` 在 `PHASE_TOOL_NAMES` 中只列在阶段 3，但工具自身声明 `phases=[3, 4]`）。这是代码中的一个设计不一致点，`PHASE_TOOL_NAMES` 作为死代码可能已经过时。

### 5.4 工具错误处理

工具执行有三级错误处理：

| 错误类型 | 处理方式 | 示例 |
|---------|---------|------|
| `ToolError` | 返回结构化错误（error_code + suggestion） | API Key 未配置 → `NO_API_KEY` |
| 未知工具 | 返回 `UNKNOWN_TOOL` + 可用工具列表 | LLM 生成了不存在的工具名 |
| 未捕获异常 | 返回 `INTERNAL_ERROR` + 异常信息 | 网络超时等 |

### 5.5 全部工具参数详表

| 工具名 | 必需参数 | 可选参数 | 返回值关键字段 |
|--------|---------|---------|--------------|
| `update_plan_state` | `field`, `value` | — | `updated_field`, `new_value` |
| `search_destinations` | `query` | `preferences[]` | `destinations[]` (name, address, rating, location) |
| `check_feasibility` | `destination`, `travel_date` | — | `weather` (temp, description), `feasible` |
| `search_flights` | `origin`, `destination`, `date` | `max_results` (5) | `flights[]` (id, price, currency, segments) |
| `search_accommodations` | `destination`, `check_in`, `check_out` | `budget_per_night`, `area`, `requirements[]` | `accommodations[]` (name, address, rating, price_level) |
| `get_poi_info` | `query` | `location` | `pois[]` (name, address, rating, location, types) |
| `calculate_route` | `origin_lat/lng`, `dest_lat/lng` | `mode` (transit) | `distance`, `duration`, `steps[]` |
| `assemble_day_plan` | `pois[]` | `start_time` (09:00), `end_time` (21:00), `max_walk_km` (10) | `ordered_pois[]`, `total_distance_km`, `estimated_hours` |
| `check_availability` | `place_name`, `date` | — | `likely_open`, `hours` |
| `check_weather` | `city`, `date` | — | `forecast` (temp, temp_min/max, description, humidity, wind) |
| `generate_summary` | `plan_data` | — | `summary` (文本), `total_days`, `total_budget` |

---

## 6. 阶段转换机制

### 6.1 核心推断逻辑

阶段推断由 `PhaseRouter.infer_phase()` 执行，采用**状态字段瀑布式判断**：

```python
def infer_phase(self, plan: TravelPlanState) -> int:
    if not plan.destination:
        if plan.preferences:
            return 2      # 有偏好但没目的地 → 推荐阶段
        return 1          # 啥都没有 → 灵感探索
    if not plan.dates:
        return 3          # 有目的地没日期 → 日期规划
    if not plan.accommodation:
        return 4          # 有日期没住宿 → 住宿选择
    if len(plan.daily_plans) < plan.dates.total_days:
        return 5          # 日程未完成 → 行程组装
    return 7              # 全部完成 → 出发前查漏
```

### 6.2 转换判定流程图

```
                    ┌─────────────┐
                    │ destination? │
                    └──────┬──────┘
                     No    │    Yes
              ┌────────────┤
              ▼            ▼
        ┌──────────┐  ┌────────┐
        │preferences│  │ dates? │
        │ 非空?     │  └───┬────┘
        └──┬───────┘   No  │  Yes
        No │  Yes      ┌───┘
        ┌──┘  ┌──┐     ▼
        ▼     ▼  │  ┌──────────────┐
     阶段1  阶段2│  │accommodation?│
              └──┘  └──────┬───────┘
                     No    │    Yes
                     ┌─────┘
                     ▼
                  阶段4
                     │
                     ▼
              ┌──────────────┐
              │daily_plans   │
              │数量 < 总天数? │
              └──────┬───────┘
               Yes   │    No
               ┌─────┘
               ▼
            阶段5
               │
               ▼
            阶段7
```

### 6.3 触发时机

阶段转换在 **两个时间点** 检查：

| 触发点 | 位置 | 说明 |
|-------|------|------|
| 用户消息预处理 | `main.py` chat 端点 | 事实提取后检查 `apply_trip_facts` → `check_and_apply_transition` |
| 工具调用后钩子 | `on_tool_call` hook | `update_plan_state` 执行后检查 `check_and_apply_transition` |

### 6.4 转换生效方式

```python
def check_and_apply_transition(self, plan: TravelPlanState) -> bool:
    inferred = self.infer_phase(plan)
    if inferred != plan.phase:
        plan.phase = inferred    # 直接修改 plan 对象
        return True
    return False
```

转换是**即时生效**的——`plan.phase` 被更新后，下一次系统消息构建会使用新阶段的 Prompt 和工具集。

---

## 7. 回溯机制

### 7.1 回溯触发方式

系统支持两种回溯方式：

| 方式 | 触发方式 | 处理位置 |
|------|---------|---------|
| **显式回溯** | 调用 `POST /api/backtrack/{session_id}` | `backtrack()` 端点 |
| **隐式回溯** | 用户消息包含特定关键词 | `_detect_backtrack()` 函数 |

### 7.2 隐式回溯关键词

| 目标阶段 | 触发关键词 |
|---------|-----------|
| 阶段 1 | "重新开始", "从头来", "换个需求" |
| 阶段 2 | "换个目的地", "不想去这里", "不去了", "换地方" |
| 阶段 3 | "改日期", "换时间", "日期不对" |
| 阶段 4 | "换住宿", "不住这", "换个区域" |

**安全约束**：只能回退到比当前阶段更早的阶段（`target_phase < plan.phase`）。

### 7.3 回溯执行流程

```
用户消息包含"换个目的地"
         │
         ▼
  _detect_backtrack() → 目标阶段 2
         │
         ▼
  save_snapshot(plan) → 保存当前状态快照
         │
         ▼
  prepare_backtrack()
    ├── 记录 BacktrackEvent (from→to, reason, snapshot_path)
    ├── plan.clear_downstream(from_phase=2) → 清除下游字段
    └── plan.phase = 2
         │
         ▼
  _build_agent(plan) → 重建 Agent（新的工具集和钩子）
```

### 7.4 下游字段清除规则

```python
_PHASE_DOWNSTREAM = {
    3: ["accommodation", "daily_plans"],   # 回退到阶段3：清除住宿和日程
    4: ["daily_plans"],                     # 回退到阶段4：只清除日程
}
```

清除逻辑：遍历 `_PHASE_DOWNSTREAM` 中所有 `phase >= from_phase` 的条目并清除对应字段。因此：

| 回退目标 | 清除字段 | 说明 |
|---------|---------|------|
| 阶段 1 或 2 | `accommodation` + `daily_plans` | 3>=1, 4>=1，两条规则都触发 |
| 阶段 3 | `accommodation` + `daily_plans` | 3>=3, 4>=3，两条规则都触发 |
| 阶段 4 | `daily_plans` | 只有 4>=4 触发 |

> 注意：`destination`、`dates`、`preferences`、`constraints` 等字段不在清除规则中，回溯时不会被自动清除。如果需要清除这些字段，需要手动处理。

---

## 8. 质量保障系统

### 8.1 双层约束架构

```
┌────────────────────────────────────┐
│  硬约束验证 (validator.py)          │
│  ─ 阻塞性：必须修正               │
│  ─ 触发时机：update_plan_state 后  │
│  ─ 三项检查：时间冲突/预算超限/天数 │
├────────────────────────────────────┤
│  软约束评分 (judge.py)             │
│  ─ 建议性：用于优化               │
│  ─ 触发时机：assemble_day_plan 后  │
│  ─ 四维评分：节奏/地理/连贯/个性化  │
└────────────────────────────────────┘
```

### 8.2 硬约束验证详情

来源：`backend/harness/validator.py`

| 检查项 | 规则 | 错误消息示例 |
|--------|------|-------------|
| 时间冲突 | `prev.end_time + travel_time > curr.start_time` | "Day 1: 金阁寺→清水寺 时间冲突（结束14:00，交通需30min，但15:00开始，间隔仅60min）" |
| 预算超限 | `Σ(activity.cost) > plan.budget.total` | "总费用 ¥8500 超出预算 ¥5000" |
| 天数超限 | `len(daily_plans) > dates.total_days` | "天数超限：规划了 6 天行程，但只有 5 天可用" |

硬约束错误会以 `⚠️ 硬约束冲突，必须修正` 的系统消息注入对话，强制 LLM 在下一轮修正。

### 8.3 软约束评分详情

来源：`backend/harness/judge.py`

触发条件：`assemble_day_plan` 或 `generate_summary` 工具执行后。

评分维度（每项 1-5 分）：

| 维度 | 中文名 | 评估内容 |
|------|--------|---------|
| `pace` | 节奏舒适度 | 每天活动量是否均衡？有没有过紧或过松的天？ |
| `geography` | 地理效率 | 同一天的景点是否地理集中？有没有不必要的来回跑？ |
| `coherence` | 体验连贯性 | 每天的主题感是否清晰？过渡是否自然？ |
| `personalization` | 个性化程度 | 是否体现了用户的偏好？ |

评分过程：
1. 构建评估 Prompt（含完整行程数据 + 用户偏好）
2. 发送给 LLM 评估
3. 解析 JSON 响应得到 `SoftScore`
4. 将评分和建议以 `💡 行程质量评估（X.X/5）` 的系统消息注入对话

---

## 9. 上下文压缩机制

### 9.1 触发条件

来源：`backend/context/manager.py`

当消息列表的估算 token 数超过压缩阈值时触发压缩。阈值计算涉及两级配置：

```python
# main.py 中的阈值计算
threshold = int(config.llm.max_tokens * config.context_compression_threshold)

# context/manager.py 中的判断
def should_compress(self, messages, max_tokens):
    estimated = sum(len(m.content or "") // 3 for m in messages)
    return estimated > max_tokens * 0.5
```

实际触发条件是：`estimated_tokens > max_tokens × context_compression_threshold × 0.5`

其中 `context_compression_threshold` 是 `config.yaml` 中的配置项（默认 0.5），token 估算使用 `字符数 / 3` 的粗略方式。

### 9.2 偏好信号保护

以下关键词出现在用户消息中时，该消息被标记为 `must_keep`，不会被压缩：

```
不要、不想、不坐、不住、不去、不吃、必须、一定要、
偏好、喜欢、讨厌、预算、上限、最多、至少、过敏、素食、忌口
```

### 9.3 压缩算法

```
1. 分类所有消息为 must_keep 和 compressible
2. 如果 compressible 消息 ≤ 2 条，不压缩
3. 将 compressible 消息摘要为一条系统消息（最后 10 条）
4. 重建消息列表：[系统消息] + [must_keep] + [摘要] + [最后 4 条消息]
```

### 9.4 设计意义

- **偏好永不丢失**：即使对话很长，用户表达的偏好和禁忌始终保留
- **最近上下文保留**：最后 4 条消息始终保留，确保对话连贯
- **中间过程可压缩**：工具调用的中间结果可以被摘要掉

---

## 10. 事实提取预处理机制

### 10.1 机制概述

来源：`backend/state/intake.py`

在用户消息进入 Agent Loop 之前，系统会尝试从消息文本中自动提取结构化信息（目的地、日期、预算），并直接写入 `TravelPlanState`。这是一个**独立于工具系统的"快捷通道"**——不需要 LLM 判断是否调用工具，而是通过正则表达式直接提取。

### 10.2 提取能力

#### 目的地提取

```python
# 支持的模式
"去东京"  "到巴厘岛"  "飞往伦敦"  "前往大阪"
"目的地是巴黎"  "目的地为罗马"

# 过滤规则
- 去除动词后缀："去东京玩" → "东京"
- 去除数字后缀："去东京5天" → "东京"
- 含选择词时返回 None："去东京或大阪" → None
- 最短 2 个字符
```

#### 日期提取

```python
# 支持的格式
"2024-07-15 至 2024-07-20"     # ISO 日期对
"2024/07/15 至 2024/07/20"     # 斜杠分隔

# 节假日 + 天数模式
"五一5天"  →  (5/1 ~ 5/6)
"国庆7天"  →  (10/1 ~ 10/8)
"元旦3天"  →  (1/1 ~ 1/4)

# 智能年份处理：如果节假日已过，自动使用下一年
```

#### 预算提取

```python
# 支持的格式
"预算5000"  "预算¥5000"  "人均预算$500"
"花费1万元"  "预算2千"  "预算3k"
"$2000"  "€1500"

# 单位转换
"万" → ×10000    "千" → ×1000    "k" → ×1000

# 币种识别
CNY: ¥, ￥, 元, 人民币（默认）
USD: $, us$, 美元, usd
EUR: €, 欧元, eur
JPY: 日元, yen, jpy
```

### 10.3 处理流程

```
用户消息 "去东京，五一5天，预算1万元"
    │
    ▼
extract_trip_facts()
    ├── _extract_destination() → "东京"
    ├── parse_dates_value()    → DateRange(2026-05-01, 2026-05-06)
    └── parse_budget_value()   → Budget(10000, "CNY")
    │
    ▼
apply_trip_facts(plan, message)
    ├── plan.destination = "东京"
    ├── plan.dates = DateRange(...)
    └── plan.budget = Budget(...)
    │
    ▼
check_and_apply_transition(plan) → 可能直接跳到阶段 3 或更后
```

### 10.4 设计意义

- **减少工具调用延迟**：简单的结构化信息不需要等 LLM 识别和调用 `update_plan_state`
- **可能跳阶段**：如果用户一句话提供了目的地+日期，可以从阶段 1 直接跳到阶段 4
- **与工具系统并行**：提取结果可能被后续的 `update_plan_state` 工具调用覆盖

---

## 11. 用户记忆系统

### 10.1 数据模型

来源：`backend/memory/models.py`

```python
UserMemory:
  user_id: str
  explicit_preferences: dict    # 用户明确表达的偏好
  implicit_preferences: dict    # 从行为推断的偏好
  trip_history: [TripSummary]   # 历史出行记录
  rejections: [Rejection]       # 排除项（永久/临时）

TripSummary:
  destination: str
  dates: str
  satisfaction: int | None      # 1-5 满意度
  notes: str

Rejection:
  item: str                     # 被排除的项目
  reason: str                   # 排除原因
  permanent: bool               # 是否永久排除
  context: str                  # 适用范围（如特定目的地）
```

### 10.2 记忆注入方式

每次对话时，`MemoryManager.generate_summary()` 将记忆格式化为文本，注入系统消息的"用户画像"部分：

```
偏好：美食: 喜欢日料, 住宿: 偏好民宿
出行历史：东京(2024-03-15~20, 满意度4/5); 巴厘岛(2024-08-01~07)
永久排除：青旅(噪音太大), 自由行登山(恐高)
```

### 10.3 持久化

- 存储路径：`data/users/{user_id}/memory.json`
- 跨会话持久：同一 `user_id` 的不同 session 共享记忆
- 默认用户：`default_user`

---

## 12. 核心流程图

### 12.1 完整请求处理流程

```
用户发送消息
     │
     ▼
┌────────────────────┐
│ 1. 回溯检测         │ ←── 正则匹配 _BACKTRACK_PATTERNS
│    (隐式回溯)       │
└─────────┬──────────┘
     匹配? ──Yes──► 保存快照 → prepare_backtrack → 重建Agent
     │No
     ▼
┌────────────────────┐
│ 2. 事实提取         │ ←── extract_trip_facts（目的地/日期/预算）
│    (intake.py)      │
└─────────┬──────────┘
     有更新? ──Yes──► apply_trip_facts → check_and_apply_transition
     │No
     ▼
┌────────────────────┐
│ 3. 构建系统消息      │ ←── Soul + Phase Prompt + Runtime Context + User Profile
│    (ContextManager) │
└─────────┬──────────┘
     │
     ▼
┌────────────────────┐
│ 4. 追加用户消息      │
└─────────┬──────────┘
     │
     ▼
┌───────────────────────────────────────────────┐
│ 5. Agent Loop（最多 20 次迭代）               │
│                                                │
│   ┌─────────────────┐                         │
│   │ before_llm_call  │ ←── 上下文压缩检查      │
│   └────────┬────────┘                         │
│            ▼                                   │
│   ┌─────────────────┐                         │
│   │ LLM 流式调用     │ ←── 生成文本 + 工具调用  │
│   └────────┬────────┘                         │
│            │                                   │
│     有工具调用?                                │
│     │No        │Yes                           │
│     ▼          ▼                               │
│   返回文本   ┌─────────────────┐              │
│   结束循环   │ 执行工具调用      │              │
│             └────────┬────────┘              │
│                      ▼                        │
│             ┌─────────────────┐              │
│             │ after_tool_call  │              │
│             │  ├─ 阶段转换检查  │              │
│             │  ├─ 硬约束验证    │              │
│             │  └─ 软约束评分    │              │
│             └────────┬────────┘              │
│                      │                        │
│                 回到循环顶部                    │
└───────────────────────────────────────────────┘
     │
     ▼
┌────────────────────┐
│ 6. 保存状态         │ ←── state_mgr.save(plan)
│ 7. 发送状态更新事件  │ ←── SSE: state_update
└────────────────────┘
```

### 12.2 阶段生命周期流程

```
         ┌─────────┐
         │ 新会话   │
         │ phase=1  │
         └────┬────┘
              │ 用户表达偏好
              │ preferences 非空
              ▼
         ┌─────────┐
    ┌────│ 阶段 2   │◄──── "换个目的地"
    │    │ 目的地选择│
    │    └────┬────┘
    │         │ destination 确定
    │         ▼
    │    ┌─────────┐
    │ ┌──│ 阶段 3   │◄──── "改日期"
    │ │  │ 天数节奏  │
    │ │  └────┬────┘
    │ │       │ dates 确定
    │ │       ▼
    │ │  ┌─────────┐
    │ │ ─│ 阶段 4   │◄──── "换住宿"
    │ │  │ 住宿区域  │
    │ │  └────┬────┘
    │ │       │ accommodation 确定
    │ │       ▼
    │ │  ┌─────────┐
    │ └──│ 阶段 5   │
    │    │ 行程组装  │
    │    └────┬────┘
    │         │ daily_plans 全部完成
    │         ▼
    │    ┌─────────┐
    └────│ 阶段 7   │
         │ 出发查漏  │
         └─────────┘

  ◄──── 表示回溯路径
```

### 12.3 Agent Loop 内部数据流

```
Messages: [System, User₁, Asst₁, User₂, ...]
              │
              ▼
         ┌─────────┐
         │   LLM    │ ←── tools schema (按阶段过滤)
         └────┬────┘
              │
       ┌──────┴──────┐
       │             │
   text_delta    tool_call
       │             │
   yield →        ToolEngine
   前端显示       .execute(tc)
                     │
                     ▼
               ┌──────────┐
               │ ToolResult│ ──► append Message(role=TOOL)
               └──────────┘
                     │
                     ▼
               HookManager
               .run("after_tool_call")
                     │
              ┌──────┼──────┐
              │      │      │
        阶段转换  硬约束  软约束
        检查     验证    评分
```

---

## 13. 关键文件索引

### 核心逻辑文件

| 文件路径 | 职责 | 关键函数/类 |
|---------|------|------------|
| `backend/main.py` | FastAPI 应用入口、API 端点、Agent 组装 | `create_app()`, `_build_agent()`, `_detect_backtrack()` |
| `backend/agent/loop.py` | Agent 主循环（LLM↔工具交互） | `AgentLoop.run()` |
| `backend/agent/hooks.py` | 事件钩子管理器 | `HookManager.register()`, `.run()` |
| `backend/agent/types.py` | 消息和工具调用类型 | `Message`, `ToolCall`, `ToolResult`, `Role` |
| `backend/phase/router.py` | 阶段推断和转换 | `PhaseRouter.infer_phase()`, `.check_and_apply_transition()`, `.prepare_backtrack()` |
| `backend/phase/prompts.py` | 阶段 Prompt 和工具/模式配置 | `PHASE_PROMPTS`, `PHASE_TOOL_NAMES`, `PHASE_CONTROL_MODE` |
| `backend/state/models.py` | 状态数据模型 | `TravelPlanState`, `DayPlan`, `Activity`, `BacktrackEvent` |
| `backend/state/manager.py` | 状态持久化 | `StateManager.save()`, `.load()`, `.save_snapshot()` |
| `backend/state/intake.py` | 用户消息中的事实提取 | `extract_trip_facts()`, `apply_trip_facts()` |
| `backend/context/manager.py` | 系统消息构建、上下文压缩 | `ContextManager.build_system_message()`, `.should_compress()` |

### 工具文件

| 文件路径 | 工具名 | 外部 API |
|---------|--------|----------|
| `backend/tools/base.py` | （基础设施） | — |
| `backend/tools/engine.py` | （工具引擎） | — |
| `backend/tools/update_plan_state.py` | `update_plan_state` | 无 |
| `backend/tools/search_destinations.py` | `search_destinations` | Google Places |
| `backend/tools/check_feasibility.py` | `check_feasibility` | OpenWeather |
| `backend/tools/search_flights.py` | `search_flights` | Amadeus |
| `backend/tools/search_accommodations.py` | `search_accommodations` | Google Places |
| `backend/tools/get_poi_info.py` | `get_poi_info` | Google Places |
| `backend/tools/calculate_route.py` | `calculate_route` | Google Directions |
| `backend/tools/assemble_day_plan.py` | `assemble_day_plan` | 无（本地算法） |
| `backend/tools/check_availability.py` | `check_availability` | Google Places |
| `backend/tools/check_weather.py` | `check_weather` | OpenWeather |
| `backend/tools/generate_summary.py` | `generate_summary` | 无（本地格式化） |

### 辅助模块

| 文件路径 | 职责 |
|---------|------|
| `backend/harness/validator.py` | 硬约束验证（时间/预算/天数） |
| `backend/harness/judge.py` | 软约束评分（节奏/地理/连贯/个性化） |
| `backend/memory/manager.py` | 用户记忆管理 |
| `backend/memory/models.py` | 用户记忆数据模型 |
| `backend/llm/base.py` | LLM Provider 协议 |
| `backend/llm/factory.py` | LLM Provider 工厂 |
| `backend/llm/openai_provider.py` | OpenAI 实现 |
| `backend/llm/anthropic_provider.py` | Anthropic 实现 |
| `backend/config.py` | 配置加载（yaml + 环境变量） |
| `backend/context/soul.md` | Agent 身份定义 |

### 前端文件

| 文件路径 | 职责 |
|---------|------|
| `frontend/src/App.tsx` | 主应用组件（会话管理、布局） |
| `frontend/src/components/ChatPanel.tsx` | 聊天面板（消息收发、SSE 流处理） |
| `frontend/src/components/PhaseIndicator.tsx` | 阶段进度指示器 |
| `frontend/src/components/MapView.tsx` | Leaflet 地图（显示活动位置） |
| `frontend/src/components/Timeline.tsx` | 时间线（按天显示日程） |
| `frontend/src/components/BudgetChart.tsx` | 预算进度条 |
| `frontend/src/components/MessageBubble.tsx` | 消息气泡（用户/助手/工具） |
| `frontend/src/hooks/useSSE.ts` | SSE 流式连接 Hook |
| `frontend/src/types/plan.ts` | TypeScript 类型定义 |

---

## 附录 A：已知设计不一致点

### A.1 `PHASE_TOOL_NAMES` vs 工具 `phases` 声明

`prompts.py` 中的 `PHASE_TOOL_NAMES` 和各工具文件中的 `phases` 参数存在不一致。`PHASE_TOOL_NAMES` 从未被运行时调用（`PhaseRouter.get_tool_names()` 是死代码），实际工具过滤由 `ToolEngine.get_tools_for_phase()` 基于各工具文件中的 `phases` 声明执行。建议统一为单一来源。

### A.2 `update_plan_state` 的 `_ALLOWED_FIELDS` 不含 `daily_plans`

`update_plan_state` 工具通过白名单 `_ALLOWED_FIELDS` 限制可更新字段，但 `daily_plans` 不在其中。这意味着阶段 5（行程组装）生成的日程无法直接通过 `update_plan_state` 写入。实际上日程数据可能需要通过其他路径（如直接在闭包中修改 plan 对象）写入。

### A.3 `max_retries` 参数未使用

`AgentLoop.__init__` 接受 `max_retries` 参数（默认 3），但 `run()` 方法中只使用了硬编码的 `range(20)` 作为循环上限，`max_retries` 从未被引用。

---

## 附录 B：状态字段与阶段映射总表

| 状态字段 | 类型 | 哪个阶段产出 | 影响哪个阶段转换 | 回溯时是否清除 |
|---------|------|-------------|----------------|--------------|
| `preferences` | `list[Preference]` | 阶段 1 | 1→2 | 否 |
| `destination` | `str \| None` | 阶段 2 | 2→3 | 否（需手动清除） |
| `destination_candidates` | `list[dict]` | 阶段 2 | 无 | 否 |
| `dates` | `DateRange \| None` | 阶段 3 | 3→4 | 否 |
| `budget` | `Budget \| None` | 阶段 1-3 | 无（硬约束用） | 否 |
| `travelers` | `Travelers \| None` | 阶段 1-3 | 无 | 否 |
| `accommodation` | `Accommodation \| None` | 阶段 4 | 4→5 | 回退到阶段 3 时清除 |
| `daily_plans` | `list[DayPlan]` | 阶段 5 | 5→7 | 回退到阶段 3 或 4 时清除 |
| `constraints` | `list[Constraint]` | 任意阶段 | 无（验证用） | 永不清除 |
| `backtrack_history` | `list[BacktrackEvent]` | 回溯时 | 无 | 永不清除 |
