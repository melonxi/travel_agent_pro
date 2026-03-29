# Travel Agent Pro — 工程设计规格

> 基于纯手写 Agent Loop 的旅行规划系统，覆盖七阶段认知决策流（跳过阶段 6 预订），Python 后端 + TypeScript 前端，支持 OpenAI / Anthropic 多模型切换。

---

## 1. 项目定位

- **目标**：简历展示项目，突出 AI/Agent 工程能力
- **范围**：Phase 0 完整版——单 Agent 覆盖阶段 1-5 + 7，不拆子 Agent，不做真实预订
- **核心价值**：每个模块都是可以在面试中深讲的技术点——Agent Loop、上下文工程、工具设计、状态管理、记忆系统、Harness 验证

---

## 2. 技术栈

| 层 | 选型 | 理由 |
|---|------|------|
| 后端框架 | Python + FastAPI | Agent 生态最成熟，异步原生支持 |
| LLM SDK | openai + anthropic（官方 SDK） | 不依赖 Agent 框架，纯手写 Agent Loop |
| 前端框架 | TypeScript + React | 全栈展示，组件化开发 |
| 前后端通信 | REST + SSE | SSE 做流式输出，REST 做状态查询和用户输入 |
| 地图可视化 | Leaflet | 开源免费，轻量 |
| 数据存储 | JSON 文件 | 零配置，面试官 clone 即跑 |
| 外部 API | Google Maps / Amadeus / OpenWeather / Sherpa | 全真实 API |

---

## 3. 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend (TypeScript + React)                                  │
│  ┌──────────────┐  ┌──────────────────────────────────────┐     │
│  │  Chat Panel   │  │  Visualization Panel                 │     │
│  │  · 对话流      │  │  · 地图路线 (Leaflet)                │     │
│  │  · 流式输出    │  │  · 行程时间线                        │     │
│  │  · 阶段指示器  │  │  · 预算仪表盘                        │     │
│  └──────┬───────┘  └──────────────┬───────────────────────┘     │
│         │  SSE (Agent→用户)        │  状态订阅                   │
│         │  POST (用户→Agent)       │                             │
└─────────┼──────────────────────────┼───────────────────────────┘
          │                          │
┌─────────┼──────────────────────────┼───────────────────────────┐
│  Backend (Python / FastAPI)        │                             │
│         │                          │                             │
│  ┌──────▼──────────────────────────▼──────────────────────┐     │
│  │  API Gateway                                            │     │
│  │  · REST endpoints · SSE streaming · Session management  │     │
│  └──────────────────────┬────────────────────────────────┘     │
│                          │                                       │
│  ┌──────────────────────▼────────────────────────────────┐     │
│  │  Agent Loop (核心引擎)                                  │     │
│  │  感知 → 决策 → 行动 → 反馈                              │     │
│  │  · 钩子机制注入业务逻辑                                  │     │
│  │  · 流式输出                                              │     │
│  │  · 错误恢复（最多 3 次重试）                             │     │
│  └──────────────────────┬────────────────────────────────┘     │
│                          │                                       │
│         ┌────────────────┼────────────────┐                     │
│         ▼                ▼                ▼                     │
│  ┌────────────┐  ┌────────────┐  ┌──────────────┐              │
│  │ Phase       │  │ Tool       │  │ Context      │              │
│  │ Router      │  │ Engine     │  │ Manager      │              │
│  └────────────┘  └────────────┘  └──────────────┘              │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Infrastructure Layer                                     │   │
│  │  LLM Abstraction · State Manager · Memory · Harness      │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  External APIs                                            │   │
│  │  Google Maps · Amadeus · OpenWeather · Exchange Rate      │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

**架构选型：单 Agent + 阶段路由器。** 一个 Agent 贯穿全流程，通过 `plan_state.phase` 驱动行为切换。不同阶段加载不同的系统提示和工具子集。选择这个方案而非 Orchestrator + 子 Agent，是因为 Phase 0 的目标是验证单 Agent 上限，把精力花在每个模块的精度上。

---

## 4. 模块设计

### 4.1 Agent Loop（核心引擎）

主循环保持 `LLM 调用 → 判断工具调用 → 执行/返回` 的纯净结构，所有旅行业务逻辑通过钩子注入。

```python
async def agent_loop(messages: list, tools: list) -> AsyncIterator[str]:
    while True:
        response = await llm.chat(messages, tools, stream=True)

        if response.tool_calls:
            for call in response.tool_calls:
                result = await tool_engine.execute(call)
                messages.append(tool_result(call.id, result))
                await self.run_hooks("after_tool_call", call, result)
        else:
            yield response.content
            return
```

**四个关键设计点：**

1. **循环体保持纯净** — 主循环不含业务逻辑，旅行特殊性通过钩子注入（`after_tool_call`、`before_llm_call`）。循环体可以复用于任何 Agent 场景。

2. **流式输出** — LLM 响应通过 SSE 逐 token 推送到前端。三种事件类型：`text_delta`（Agent 说话）、`tool_call`（Agent 调用工具，前端展示"搜索中..."）、`state_update`（plan_state 变更，前端刷新可视化面板）。

3. **工具执行后三个检查点**（通过钩子实现）：
   - **状态变更检查**：`update_plan_state` 执行后，Phase Router 检查是否触发阶段切换
   - **硬约束检查**：每次 plan_state 变更后跑硬约束验证器，冲突时注入修正指令
   - **上下文压缩检查**：token 消耗超 50% 时触发 Context Manager 压缩

4. **错误恢复** — 工具调用失败时，结构化错误信息（含错误码 + 修复建议）写回消息列表，LLM 自行修正参数重试，最多 3 次。超过 3 次视为 API 故障，告知用户。

### 4.2 Phase Router（阶段路由器）

通过三个维度实现同一个 Agent 在不同阶段的"人格切换"：

**维度一：系统提示切换**

每个阶段有独立的提示片段，定义 Agent 在该阶段的角色和行为模式：

| 阶段 | 角色定位 | 行为模式 |
|------|---------|---------|
| 1. 模糊意愿浮现 | 旅行灵感顾问 | 开放式提问，引导用户具象化需求 |
| 2. 目的地选择 | 目的地推荐专家 | 推荐 2-3 候选，附量化数据，最终用户拍板 |
| 3. 天数与节奏 | 行程节奏规划师 | 基于目的地特点给天数建议，输出约束清单 |
| 4. 住宿区域选择 | 住宿区域顾问 | 多变量权衡：交通、安全、性价比、餐饮 |
| 5. 每日行程组装 | 行程组装引擎 | 结构化输出 DayPlan，必须通过硬约束验证 |
| 7. 出发前查漏 | 出行管家 | 生成检查清单，逐项校验 |

**维度二：工具集切换**

不同阶段暴露不同工具子集：

| 阶段 | 可用工具 |
|------|---------|
| 1 | `update_plan_state` |
| 2 | `search_destinations`, `check_travel_feasibility`, `update_plan_state` |
| 3 | `search_flights`, `update_plan_state` |
| 4 | `search_accommodations`, `calculate_route`, `update_plan_state` |
| 5 | `get_poi_info`, `calculate_route`, `assemble_day_plan`, `check_availability`, `update_plan_state` |
| 7 | `check_weather_forecast`, `generate_trip_summary`, `update_plan_state` |

**维度三：控制模式切换**

| 阶段 | 控制模式 | 含义 |
|------|---------|------|
| 1 | conversational | 纯对话，LLM 全权决定回复 |
| 2 | agent_with_guard | Agent 主导，目的地选择需用户确认 |
| 3 | workflow | 约束收集是确定性流程 |
| 4 | conversational | 多变量权衡，需 LLM 推理 |
| 5 | structured | 输出必须可解析为 DayPlan JSON |
| 7 | evaluator | 生成→校验→修正循环 |

**阶段推断逻辑：**

基于 `plan_state` 字段填充程度做确定性推断，不依赖 LLM 判断：

- `destination` 为空 → 阶段 1 或 2（看是否有 preferences）
- `destination` 有值，`dates` 为空 → 阶段 3
- `dates` 有值，`accommodation` 为空 → 阶段 4
- `accommodation` 有值，`daily_plans` 未排满 → 阶段 5
- `daily_plans` 排满 → 阶段 7

**回溯协调：**

回溯时保存当前状态快照，清除目标阶段之后的下游产出物（保留约束和偏好），记录回溯事件，切换阶段并重新加载提示和工具集。

### 4.3 Tool Engine（工具引擎）

管理工具的注册、调度、参数校验和执行。

**工具注册：** 每个工具通过 `@tool` 装饰器注册，描述遵循 ACI 原则（做什么 / 什么时候用 / 什么时候不用）。描述层和代码层形成双重保险——代码控制工具可见性，描述引导 LLM 判断。

**参数校验：** 在工具实现内部完成，抛出结构化 `ToolError`（含错误码 + 修复建议）。

**工具结果标准化：** 所有工具返回统一格式，包含 `status`、`data`、`metadata`（含 `source` 数据溯源字段，防幻觉）。

**完整工具清单（10 个）：**

| 工具 | 输入 | 输出 | 外部 API |
|------|------|------|----------|
| `search_destinations` | 关键词/偏好 | 目的地候选列表 | Google Places |
| `check_travel_feasibility` | 目的地+日期 | 签证/季节/安全评估 | Sherpa + OpenWeather |
| `search_flights` | 出发地/目的地/日期 | 航班列表 | Amadeus |
| `search_accommodations` | 城市/区域/日期/预算 | 住宿列表 | Booking.com API |
| `get_poi_info` | POI 名称或 ID | 详情（时间/票价/评分） | Google Places |
| `calculate_route` | 起点/终点/方式 | 路线/时间/距离 | Google Maps Directions |
| `assemble_day_plan` | 景点列表+约束 | 单日行程 | 内部逻辑 |
| `check_availability` | 资源名+日期 | 可用性 | Google Places |
| `check_weather_forecast` | 城市/日期 | 天气预报 | OpenWeather |
| `generate_trip_summary` | plan_state | 出行摘要卡片 | 内部逻辑 |
| `update_plan_state` | 字段+值 | 更新后的 state | 内部逻辑 |

`assemble_day_plan` 是计算密集型工具——接收景点列表和约束条件，用算法排出最优单日行程，不让 LLM 做排列组合。

### 4.4 Context Manager（上下文管理器）

管理四层上下文的拼装和生命周期。

**四层上下文结构（按拼装顺序）：**

1. **常驻层**（≈800-1200 tokens）— SOUL.md 身份定义 + 工具定义。每次会话不变，Prompt Cache 命中率最高。**放在 messages 最前面**以最大化缓存命中。

2. **阶段层**（≈300-500 tokens）— 当前阶段的 Phase Prompt + Skill 指引。阶段切换时才变。

3. **运行时注入层**（≈200-600 tokens）— plan_state 摘要、用户画像摘要、最近回溯原因。每轮动态拼入。

4. **对话历史**（长度不可控）— 用户消息 + Agent 回复 + 工具调用结果。持续增长，是压缩的主要目标。

**压缩策略：**

当对话历史 token 超过上下文窗口 50% 时触发。压缩分三步：

1. **分类**：将消息分为 must_keep（状态变更、用户偏好表达、回溯记录）、compressible（一般对话）、droppable（中间搜索细节）
2. **可恢复压缩**：大体量工具结果只保留摘要，原始数据写入文件，需要时通过工具重新读取
3. **LLM 摘要**：对 compressible 部分做摘要，保留最近 3 轮完整对话

**保留优先级（压缩时不可丢弃，按优先级降序）：**

1. plan_state 完整内容
2. 用户明确表达的偏好和约束
3. 排除记录（为什么否决了某个选项）
4. 回溯历史
5. 已确认的信息

**plan_state 是真相来源**——即使对话历史被完全压缩，只要 plan_state 在，Agent 就能恢复到正确状态。

### 4.5 LLM Abstraction（多模型抽象层）

统一接口抹平 OpenAI 和 Anthropic SDK 的差异，上层代码只跟标准化类型打交道。

**核心接口：**

```python
class LLMProvider(Protocol):
    async def chat(self, messages, tools, stream=True) -> AsyncIterator[LLMChunk]: ...
    async def count_tokens(self, messages) -> int: ...
```

**需要抹平的差异：**

| 差异点 | OpenAI | Anthropic | 统一为 |
|--------|--------|-----------|--------|
| 系统提示 | `role: "system"` 消息 | `system` 参数独立传入 | 抽象层自动拆分 |
| 工具调用格式 | `tool_calls` | `tool_use` content block | 统一 `ToolCall` 对象 |
| 流式输出 | `chunk.choices[0].delta` | `content_block_delta` | 统一 `LLMChunk` |
| 工具结果回传 | `role: "tool"` | `role: "user"` + `tool_result` | 抽象层自动转换 |
| token 计数 | `tiktoken` | `anthropic.count_tokens()` | 各自实现，接口统一 |

**统一输出类型：**

```python
@dataclass
class LLMChunk:
    type: Literal["text_delta", "tool_call_start", "tool_call_delta", "done"]
    content: str | None = None
    tool_call: ToolCall | None = None
```

**模型配置：**

通过 `config.yaml` 配置默认模型，支持按阶段覆盖（高级用法）。例如阶段 1-2 用 Claude（长对话、同理心强），阶段 5 用 GPT-4o（结构化输出稳定）。

此模块刻意做薄——只做格式转换，不做 retry / rate limiting / fallback。

### 4.6 Harness（验证基础设施）

两层验证架构：硬约束用代码评分器（100% 确定性），软约束用 LLM Judge（概率性打分）。

**硬约束验证器（5 条规则）：**

1. **时间不冲突**：相邻活动间隔 ≥ 交通耗时。错误信息带具体数据（"需要 25min 但间隔只有 10min"）
2. **营业时间匹配**：到达时间在营业时间窗口内
3. **单日步行距离**：不超过用户偏好上限
4. **总预算不超限**：所有已知费用 ≤ 总预算
5. **日期合法性**：规划天数 ≤ 实际行程天数

硬约束失败 → 冲突信息以 `⚠️ 必须修正` 的措辞注入消息列表，LLM 视为强制指令。

**软约束评估器（LLM Judge，4 个维度）：**

1. **节奏舒适度**：每天活动量是否均衡
2. **地理效率**：同一天景点是否地理聚集
3. **体验连贯性**：每天有没有主题感
4. **个性化**：是否贴合用户偏好

Judge 使用独立的 LLM 调用（独立上下文，不共享主 Agent 对话历史），只看行程数据和用户画像，避免"自己评自己"的偏见。

软约束低于 3 分 → 建议以 `💡 建议改进` 的措辞注入，LLM 有自由裁量权。

**验证触发时机：**

- 硬约束：`daily_plans` 变更时、阶段切换前、回溯后（代码执行，零成本，频繁跑）
- 软约束：单日行程组装完成、全部行程完成、用户主动请求（LLM 调用，有成本，关键节点跑）

### 4.7 State & Memory（状态持久化与记忆系统）

**Plan State — 单次规划状态：**

```python
@dataclass
class TravelPlanState:
    session_id: str
    phase: int
    destination: str | None
    destination_candidates: list[dict]  # 含排除原因
    dates: DateRange | None
    travelers: Travelers | None
    budget: Budget | None
    accommodation: Accommodation | None
    daily_plans: list[DayPlan]
    constraints: list[Constraint]
    preferences: list[Preference]
    backtrack_history: list[BacktrackEvent]
    created_at: str
    last_updated: str
    version: int  # 乐观锁
```

存储为 JSON 文件，回溯前自动保存全量快照。

**文件结构：**

```
data/
  sessions/
    {session_id}/
      plan.json
      snapshots/
        {timestamp}.json
      tool_results/
        flight-search-001.json
        hotel-search-001.json
  users/
    {user_id}/
      memory.json
      history/
        session_001.json
```

**User Memory — 跨会话记忆：**

```python
@dataclass
class UserMemory:
    user_id: str
    explicit_preferences: dict   # 用户主动说的偏好
    implicit_preferences: dict   # 从历史行为推断
    trip_history: list[TripSummary]
    rejections: list[Rejection]  # 排除记录（含 permanent 标记）
```

`rejections` 单独建模是关键设计——区分永久排除（"不坐红眼航班"，写入偏好）和场景排除（"新宿太远"，只在相关目的地生效），避免 Agent 重复推荐被否决的选项。

**读写时机：**

- 会话开始：加载 memory，生成摘要注入上下文
- 会话结束：从本次规划提取新偏好，更新 memory

---

## 5. 前端设计

**对话式 + 可视化面板混合布局：**

- **左侧 Chat Panel**：对话流（流式输出）、阶段指示器（当前处于七阶段中的哪个）、工具调用状态提示（"正在搜索航班..."）
- **右侧 Visualization Panel**：地图路线（Leaflet）、行程时间线、预算仪表盘。随 `plan_state` 变更实时刷新。

前端不是本项目重点，够用即可。核心交互是对话，可视化面板是对 plan_state 的实时渲染。

**前后端通信：**

- 用户输入 → `POST /api/chat/{session_id}`
- Agent 回复 → SSE `GET /api/chat/{session_id}/stream`，三种事件：`text_delta`、`tool_call`、`state_update`
- 状态查询 → `GET /api/plan/{session_id}`

---

## 6. 数据流示例

以"用户说想去日本5天"为例，展示完整数据流：

```
用户: "我想去日本玩5天"
  │
  ▼ POST /api/chat
Agent Loop 启动
  │
  ▼ 构建 messages
  [SOUL.md + Phase 1 Prompt + 空 plan_state + 用户消息]
  │
  ▼ 调用 LLM
  LLM 判断: 用户有明确目的地意向，调用 update_plan_state
  │
  ▼ 执行工具
  update_plan_state({destination: "Japan", dates: {5天}})
  │
  ▼ after_tool_call 钩子触发
  Phase Router: destination 有值 + dates 有值 → 切换到阶段 3?
  不，dates 还不是具体日期，只是天数，需要确认出发日期 → 保持阶段 2
  │
  ▼ 工具结果写回 messages，继续 LLM 调用
  LLM: "日本是个很棒的选择！请问你计划什么时候出发？
        有偏好的地区吗——东京、京都、大阪、还是想走多城？"
  │
  ▼ SSE 流式输出到前端
  前端 Chat Panel 逐字显示回复
  前端 Visualization Panel 更新阶段指示器为"阶段 2: 目的地选择"
```

---

## 7. 项目目录结构

```
travel_agent_pro/
├── docs/                          # 文档
│   └── travel_agent_framework.md  # 技术框架设计
│
├── backend/                       # Python 后端
│   ├── main.py                    # FastAPI 入口
│   ├── agent/
│   │   ├── loop.py                # Agent Loop 核心循环
│   │   ├── hooks.py               # 钩子注册和管理
│   │   └── types.py               # 消息、工具调用等类型定义
│   ├── phase/
│   │   ├── router.py              # Phase Router 阶段路由
│   │   ├── prompts.py             # 各阶段系统提示
│   │   └── control.py             # 控制模式定义
│   ├── tools/
│   │   ├── engine.py              # Tool Engine 工具引擎
│   │   ├── registry.py            # 工具注册装饰器
│   │   ├── base.py                # 工具基类和 ToolError
│   │   ├── search_destinations.py
│   │   ├── check_feasibility.py
│   │   ├── search_flights.py
│   │   ├── search_accommodations.py
│   │   ├── get_poi_info.py
│   │   ├── calculate_route.py
│   │   ├── assemble_day_plan.py
│   │   ├── check_availability.py
│   │   ├── check_weather.py
│   │   ├── generate_summary.py
│   │   └── update_plan_state.py
│   ├── context/
│   │   ├── manager.py             # Context Manager 上下文管理
│   │   ├── compression.py         # 压缩策略
│   │   └── prompts/
│   │       └── soul.md            # SOUL.md 常驻提示
│   ├── llm/
│   │   ├── base.py                # LLMProvider 接口定义
│   │   ├── openai_provider.py     # OpenAI 实现
│   │   ├── anthropic_provider.py  # Anthropic 实现
│   │   └── types.py               # LLMChunk, ToolCall 等统一类型
│   ├── state/
│   │   ├── manager.py             # State Manager 状态管理
│   │   ├── models.py              # TravelPlanState 数据模型
│   │   └── snapshot.py            # 快照管理
│   ├── memory/
│   │   ├── manager.py             # Memory Manager 记忆管理
│   │   └── models.py              # UserMemory 数据模型
│   ├── harness/
│   │   ├── validator.py           # 硬约束验证器
│   │   ├── judge.py               # 软约束 LLM Judge
│   │   └── rules.py               # 验证规则定义
│   └── config.py                  # 配置加载
│
├── frontend/                      # TypeScript 前端
│   ├── src/
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── ChatPanel.tsx      # 对话面板
│   │   │   ├── MapView.tsx        # 地图可视化
│   │   │   ├── Timeline.tsx       # 行程时间线
│   │   │   ├── BudgetChart.tsx    # 预算仪表盘
│   │   │   └── PhaseIndicator.tsx # 阶段指示器
│   │   ├── hooks/
│   │   │   └── useSSE.ts          # SSE 流式订阅
│   │   └── types/
│   │       └── plan.ts            # 前端类型定义
│   └── package.json
│
├── data/                          # 运行时数据（gitignore）
│   ├── sessions/
│   └── users/
│
├── config.yaml                    # 项目配置
└── README.md
```

---

## 8. 简历亮点总结

| 技术点 | 可讲的深度 |
|--------|-----------|
| 纯手写 Agent Loop | 不依赖框架，理解 Agent 的本质是感知→决策→行动循环 |
| 钩子机制 | 业务逻辑与循环体解耦，通用引擎 + 领域插件 |
| 阶段路由器 | Workflow + Agent 混合控制，不同阶段不同控制模式 |
| 上下文工程 | 四层上下文、压缩策略、Prompt Cache 友好的拼装顺序 |
| 工具设计 | ACI 原则、结构化错误恢复、数据溯源防幻觉 |
| 多模型抽象 | 统一接口抹平 SDK 差异，按阶段选模型 |
| 回溯机制 | 回溯作为一等公民，快照 + 下游重算 |
| Harness 验证 | 硬约束代码验证 + 软约束 LLM Judge，分层验证 |
| 状态管理 | plan_state 作为单一真相来源，对话历史可丢弃 |
| 记忆系统 | 跨会话偏好学习，排除记录防重复推荐 |

---

## 9. 不做的事情（YAGNI）

- 不做真实预订和支付（阶段 6）
- 不拆多 Agent（Phase 0 验证单 Agent 上限）
- 不做向量数据库（JSON 文件足够）
- 不做 CI/CD（简历项目不需要）
- 不做用户认证（单用户即可）
- 不做国际化
- 不做移动端适配
