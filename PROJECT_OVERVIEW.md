# Travel Agent Pro — 项目全景图

> **用途**：为 AI 模型提供项目大局观。遇到需要全局理解的问题时先读此文件。
> **维护规则**：每次 commit 时同步更新本文件，确保始终反映最新架构。

---

## 1. 一句话定位

**Travel Agent Pro** 是一个基于 LLM 的智能旅行规划 Agent 系统，采用 7 阶段认知决策流程（模糊意图 → 出发前清单），通过 FastAPI + React 全栈实现，支持 SSE 流式交互、多 LLM 供应商切换、上下文压缩和可观测性追踪。

---

## 2. 技术栈速览

| 层级 | 技术 |
|------|------|
| 后端框架 | Python 3.12+, FastAPI, Uvicorn, async/await |
| 前端框架 | TypeScript, React 19, Vite 6, Leaflet 地图 |
| LLM 提供商 | OpenAI (gpt-4o) + Anthropic (Claude Sonnet 4) 可按阶段切换 |
| 数据持久化 | aiosqlite (会话/消息), JSON 文件 (旅行方案快照) |
| 可观测性 | OpenTelemetry + Jaeger (OTLP gRPC on :4317, UI on :16686) |
| 测试 | pytest + pytest-asyncio (后端), Playwright (E2E) |
| 外部服务 | Tavily (Web 搜索), 小红书 CLI, FlyAI CLI, Google Maps, Amadeus, OpenWeather |

---

## 3. 目录结构总览

```
travel_agent_pro/
├── backend/                    # Python 后端
│   ├── main.py                 # FastAPI 入口 (856 行), API 端点, 会话管理, SSE 流
│   ├── config.py               # 配置加载 (.env + config.yaml), 多 LLM 按阶段切换
│   ├── agent/                  # Agent 循环引擎
│   │   ├── loop.py             # 核心循环: LLM→工具执行→阶段转换→修复 (568 行)
│   │   ├── compaction.py       # 上下文压缩: token 预算计算、渐进式压缩
│   │   ├── hooks.py            # 钩子系统 (before_llm_call, after_tool_call)
│   │   └── types.py            # Message, ToolCall, ToolResult 数据类
│   ├── llm/                    # LLM 抽象层
│   │   ├── base.py             # LLMProvider Protocol (chat, count_tokens, get_context_window)
│   │   ├── factory.py          # 工厂: provider 字符串 → 具体实例
│   │   ├── openai_provider.py  # OpenAI 实现 (流式 + tiktoken)
│   │   ├── anthropic_provider.py # Anthropic 实现 (非流式回退)
│   │   └── types.py            # LLMChunk, ChunkType 枚举
│   ├── state/                  # 旅行状态模型
│   │   ├── models.py           # TravelPlanState 完整数据类 (350+ 行)
│   │   ├── manager.py          # StateManager: JSON 文件持久化
│   │   └── intake.py           # 自然语言 → 旅行事实提取 (日期/预算/人数)
│   ├── memory/                 # 用户记忆
│   │   ├── models.py           # UserMemory: 偏好、历史、排除项
│   │   └── manager.py          # MemoryManager: 用户维度持久化
│   ├── context/                # 上下文管理
│   │   ├── manager.py          # ContextManager: 系统提示构建、运行时注入、压缩决策 (386 行)
│   │   └── soul.md             # Agent 人格定义 (启动时加载)
│   ├── phase/                  # 阶段路由
│   │   ├── router.py           # PhaseRouter: 阶段推断、转换检测
│   │   ├── prompts.py          # 各阶段详细提示词 (431 行)
│   │   └── backtrack.py        # BacktrackService: 回退至早期阶段
│   ├── tools/                  # 领域工具 (24+ 个)
│   │   ├── base.py             # @tool 装饰器, ToolDef, ToolError
│   │   ├── engine.py           # ToolEngine: 注册/执行/批量调度/阶段过滤
│   │   ├── update_plan_state.py # 核心状态写入工具 (394 行)
│   │   ├── xiaohongshu_search.py # 小红书搜索/阅读/评论
│   │   ├── web_search.py       # Tavily 网页搜索
│   │   ├── search_flights.py   # 航班搜索 (Amadeus/FlyAI)
│   │   ├── search_trains.py    # 火车搜索 (FlyAI)
│   │   ├── search_accommodations.py # 住宿搜索
│   │   ├── get_poi_info.py     # POI 详情
│   │   ├── calculate_route.py  # 路线计算 (Google Maps)
│   │   ├── assemble_day_plan.py # 日程编排
│   │   ├── check_weather.py    # 天气查询
│   │   ├── check_availability.py # 景点可用性
│   │   ├── check_feasibility.py # 行程可行性
│   │   ├── generate_summary.py # 方案摘要
│   │   ├── flyai_client.py     # FlyAI CLI 客户端封装
│   │   └── normalizers.py      # API 响应数据标准化 (15KB)
│   ├── storage/                # 数据库层
│   │   ├── database.py         # SQLite 连接与 schema 初始化
│   │   ├── session_store.py    # 会话 CRUD
│   │   ├── message_store.py    # 消息读写 (按 seq 排序)
│   │   └── archive_store.py    # 快照与归档
│   ├── harness/                # 质量守护
│   │   ├── validator.py        # 硬约束检查 (时间冲突/预算超支/天数超限)
│   │   └── judge.py            # 软评分 (pace/geography/coherence/personalization 各1-5)
│   ├── telemetry/              # 可观测性
│   │   ├── setup.py            # OpenTelemetry TracerProvider + OTLP 导出
│   │   ├── attributes.py       # 标准化 span 属性与事件名
│   │   └── decorators.py       # @trace_agent_loop, @trace_tool_call
│   └── tests/                  # pytest 测试套件 (62 个测试文件)
│
├── frontend/                   # React 前端
│   ├── src/
│   │   ├── main.tsx            # React 19 入口
│   │   ├── App.tsx             # 应用壳: 会话管理, 主题, 三栏布局
│   │   ├── components/
│   │   │   ├── ChatPanel.tsx   # 聊天面板: SSE 流, 工具卡片, 状态变化展示
│   │   │   ├── MessageBubble.tsx # 消息渲染: Markdown, 工具卡, 压缩提示
│   │   │   ├── SessionSidebar.tsx # 会话侧边栏: 列表/新建/删除
│   │   │   ├── SessionItem.tsx # 单条会话: 标题/阶段/时间
│   │   │   ├── PhaseIndicator.tsx # 阶段进度条: 4 步可视化
│   │   │   ├── Phase3Workbench.tsx # Phase3 规划工作台 (旅行画像/候选/骨架/锁定/风险)
│   │   │   ├── MapView.tsx     # Leaflet 地图: 标记点+路线
│   │   │   ├── Timeline.tsx    # 日程时间线
│   │   │   └── BudgetChart.tsx # 预算可视化
│   │   ├── hooks/
│   │   │   └── useSSE.ts       # SSE 流式连接 Hook
│   │   ├── types/
│   │   │   ├── plan.ts         # TravelPlanState 前端类型
│   │   │   └── session.ts      # SessionMeta, SessionMessage
│   │   └── styles/
│   │       └── index.css       # "Solstice" 暗色玻璃设计系统 (1900+ 行)
│   ├── vite.config.ts          # Vite 6: /api → localhost:8000 代理
│   └── package.json            # React 19, Leaflet, react-markdown
│
├── docs/                       # 架构文档与学习笔记
├── scripts/                    # dev.sh (启动) + dev-stop.sh (停止)
├── data/sessions/              # 会话文件 (plan.json 快照)
├── config.yaml                 # 运行时配置 (LLM/API/阈值)
├── docker-compose.observability.yml # Jaeger 一键启动
├── e2e-test.spec.ts            # Playwright E2E 测试
├── AGENTS.md                   # AI Agent 项目规范
├── CLAUDE.md                   # Claude 特定规范
└── PROJECT_OVERVIEW.md         # 👈 本文件
```

---

## 4. 核心架构：7 阶段认知决策流

```
用户消息 → Phase 1 → Phase 3 → Phase 5 → Phase 7
            需求收集    方案设计    行程组装    出发前查漏
           (目的地)   (4 子步骤)   (日程详排)   (检查清单)
```

### Phase 1 — 灵感与目的地收敛
- **目标**：模糊意图 → 1-3 个候选目的地 → 锁定
- **工具**：`xiaohongshu_search` (UGC), `web_search` (事实), `quick_travel_search` (价格)
- **产出**：`destination` 字段确认

### Phase 3 — 框架规划（4 个子步骤）
- **brief** → 建立旅行画像 (目标/节奏/约束/必做-避免)
- **candidate** → 候选池构建与筛选
- **skeleton** → 2-3 套骨架方案 (非逐小时)
- **lock** → 锁定交通+住宿
- **工具门控**：每个子步骤只暴露该阶段需要的工具子集
- **产出**：`trip_brief`, `candidate_pool`, `skeleton_plans`, `selected_skeleton_id`, 交通/住宿

### Phase 5 — 日程详排
- **流程**：expand(骨架→日期) → assemble(活动+时间) → validate(开放/距离/天气/预算) → commit
- **产出**：`daily_plans[]` 每天含完整 Activity 列表
- **重要**：运行时上下文必须注入完整骨架内容、trip_brief 字段、偏好和约束

### Phase 7 — 出发前查漏 (桩)

### 阶段转换机制
- `PhaseRouter.infer_phase(plan)` 根据字段填充情况推断当前阶段
- 自动转换 + 遥测事件记录
- 支持 Backtrack（回退至早期阶段，清除下游数据）

---

## 5. 核心数据流

```
用户消息 (POST /api/sessions/{id}/chat)
    ↓
[main.py] 加载会话+方案, 组装消息列表
    ↓
[AgentLoop.run()] 进入迭代循环 (max_retries=30)
    │
    ├─ [Hook: before_llm_call]
    │   ├─ ContextManager.build_system_message() → 注入 soul + 阶段提示 + 状态快照
    │   └─ compact_messages_for_prompt() → token 预算内渐进压缩
    │
    ├─ [LLMProvider.chat()] → 流式输出 text_delta + tool_calls
    │
    ├─ [ToolEngine.execute()/execute_batch()] → 顺序/并行调度工具，yield TOOL_RESULT 事件
    │
    ├─ [PhaseRouter.check_and_apply_transition()] → 检测阶段变化
    │
    ├─ [Hook: after_tool_call]
    │   ├─ validator.validate_hard_constraints() → 时间/预算/天数
    │   └─ SoftJudge → pace/geography/coherence/personalization 评分
    │
    └─ yield LLMChunk → SSE 事件流 → 前端实时渲染
```

---

## 6. 上下文压缩机制（关键设计）

### 两层压缩策略
1. **before_llm_call 预压缩**：每次 LLM 调用前检查
   - token 预算公式：`budget = context_window - max_output_tokens - 2000`
   - 4 级渐进阈值：
     - `<60%`：不压缩
     - `60-85%`：温和压缩 (工具结果保留 60%)
     - `85%+`：激进压缩 (工具结果保留 40%)
     - 仍超：历史摘要

2. **阶段转换压缩**：规则驱动，无额外 LLM 调用
   - 格式：`用户: ...` → `决策: field = value` → `工具 {name} 成功: {preview}` → `助手: {text[:200]}…`
   - 优势：-1 轮 LLM 调用延迟，确定性摘要

### 工具结果特定压缩规则
- `web_search`: 摘要 400→600 字符, 片段 200→300 字符, 最多 5→8 结果
- `xiaohongshu_search.search_notes`: 8→12 条, URL 去查询参数
- `xiaohongshu_search.read_note`: 描述 300→400 字符
- `xiaohongshu_search.get_comments`: 8→12 条, 每条 200→260 字符

---

## 7. LLM 抽象与多供应商

```python
# Protocol 定义
class LLMProvider(Protocol):
    async def chat(messages, tools, stream) → AsyncIterator[LLMChunk]
    async def count_tokens(messages) → int
    async def get_context_window() → int | None

# 按阶段切换 (config.yaml)
llm_overrides:
  phase_1_2:
    provider: "anthropic"
    model: "claude-sonnet-4-20250514"
  phase_5:
    provider: "openai"
    model: "gpt-4o"
```

---

## 8. 工具系统

### 注册与执行
- `@tool` 装饰器：声明名称、描述、可用阶段、参数 schema
- `ToolEngine`：按阶段+子步骤过滤可用工具，传递给 LLM
- 错误处理：`ToolError` 带 `error_code` + `suggestion` 反馈给 LLM

### Phase 3 工具门控
```
brief     → update_plan_state, web_search, xiaohongshu_search
candidate → + quick_travel_search, get_poi_info
skeleton  → + calculate_route, assemble_day_plan, check_availability
lock      → + search_flights, search_trains, search_accommodations
```

### 工具清单 (24+)
| 类别 | 工具 | 说明 |
|------|------|------|
| 状态 | `update_plan_state` | 核心状态写入 (394 行), 冗余检测 |
| 搜索 | `xiaohongshu_search`, `web_search`, `quick_travel_search` | 信息获取 |
| 交通 | `search_flights`, `search_trains`, `calculate_route` | 路线规划 |
| 住宿 | `search_accommodations` | 酒店搜索 |
| POI | `get_poi_info`, `check_availability` | 景点信息 |
| 行程 | `assemble_day_plan`, `check_feasibility` | 日程编排 |
| 辅助 | `check_weather`, `generate_summary` | 验证与输出 |

---

## 9. 前端架构

### 三栏布局
```
┌─────────────┬──────────────────────┬──────────────────────────┐
│ SessionSidebar│    ChatPanel         │      RightPanel          │
│ 会话列表      │  聊天 + 工具卡片      │ Phase3Workbench / Map /  │
│ + 新建/删除   │  SSE 流式渲染         │ Timeline / BudgetChart   │
└─────────────┴──────────────────────┴──────────────────────────┘
```

### SSE 流式协议
```
POST /api/chat/{sessionId}  →  ReadableStream (NDJSON)

事件类型:
  text_delta          → 助手文本增量
  tool_call           → 工具调用开始 (名称 + 参数)
  tool_result         → 工具结果 (success/error/skipped + data)
  state_update        → 方案状态变化 (完整 TravelPlanState)
  context_compression → 上下文压缩通知
  done                → 流结束
```

### 关键组件
- **ChatPanel**: 消息列表 + 工具卡片 + 状态变化芯片 + 自动滚动
- **Phase3Workbench**: 旅行画像 / 候选池 / 骨架方案 / 锁定区 / 风险 (5 卡片)
- **MapView**: Leaflet 地图, 活动标记 + 路线连线, 明暗主题
- **Timeline**: 逐日活动时间线
- **BudgetChart**: 预算进度条 + 按日分布
- **useSSE**: 自定义 Hook, ReadableStream 解析 NDJSON

### 设计系统 "Solstice"
暗色玻璃质感 + 琥珀色暖光点缀, 1900+ 行 CSS

---

## 10. 数据持久化

### SQLite Schema (4 表)
```sql
sessions     → session_id, user_id, title, phase, status, created_at, updated_at
messages     → id, session_id, role, content, tool_calls(JSON), tool_call_id, seq
plan_snapshots → id, session_id, phase, plan_json, created_at
archives     → id, session_id, plan_json, summary, created_at
```

### 文件系统
```
backend/data/
├── sessions.db                    # SQLite 主库
└── sessions/
    └── sess_{12-hex}/
        ├── plan.json              # TravelPlanState 快照
        ├── snapshots/             # 回退快照
        └── tool_results/          # 工具结果缓存
```

---

## 11. API 端点

```
GET  /health                              → 健康检查
POST /api/sessions                        → 创建新会话
GET  /api/sessions                        → 列出所有会话
GET  /api/sessions/{id}                   → 会话元数据
DELETE /api/sessions/{id}                 → 软删除会话
POST /api/sessions/{id}/chat              → 发送消息 (SSE 流式响应)
GET  /api/sessions/{id}/plan (或 /api/plan/{id}) → 获取旅行方案
GET  /api/messages/{id}                   → 获取会话消息历史
POST /api/sessions/{id}/backtrack         → 回退到指定阶段
```

---

## 12. 质量守护 (Harness)

### 硬约束验证器 (自动)
- 时间冲突：活动结束 + 交通时间 > 下一活动开始
- 预算超支：活动总花费 > 总预算
- 天数超限：计划天数 > 可用天数

### 软评分 (LLM 判断)
- `pace` (1-5): 节奏合理性
- `geography` (1-5): 地理连贯性
- `coherence` (1-5): 逻辑一致性
- `personalization` (1-5): 个性化程度
- 在 `assemble_day_plan`, `generate_summary` 工具之后触发

---

## 13. 可观测性

```yaml
# docker-compose.observability.yml
jaeger:
  ports: ["4317:4317", "16686:16686"]

# Span 覆盖
agent.loop      → 完整循环追踪
tool.execute    → 每个工具调用
llm.chat        → LLM 请求/响应
phase.transition → 阶段变化 + 方案快照
context.compression → 压缩决策
```

---

## 14. 开发命令

```bash
# 全栈启动
npm run dev:all                    # 并行启动后端(:8000)+前端(:5173)
npm run dev:stop                   # 优雅停止所有进程

# 后端
cd backend && source .venv/bin/activate
uvicorn main:app --reload --port 8000
pytest                             # 运行测试
pytest --cov                       # 带覆盖率

# 前端
cd frontend && npm run dev         # Vite 开发服务器
cd frontend && npm run build       # 类型检查 + 构建

# E2E
npx playwright test e2e-test.spec.ts

# 可观测性
docker compose -f docker-compose.observability.yml up -d
# 然后访问 http://localhost:16686 查看 Jaeger UI
```

---

## 15. 配置体系

```
backend/.env          → 敏感凭证 (API keys, 通过 python-dotenv 加载)
config.yaml           → 运行时配置 (LLM 模型/阶段覆盖/阈值/功能开关)
                        支持 ${ENV_VAR} 引用环境变量
优先级: 环境变量 > YAML > 代码默认值
```

---

## 16. 关键设计决策速查

| 决策 | 理由 |
|------|------|
| SSE 流式 | 工具执行/压缩耗时长，需实时反馈 |
| Async SQLite | 非阻塞持久化，紧贴事件循环 |
| 内存会话缓存 | 亚秒级会话恢复 |
| Protocol-based LLM | 运行时可检查，按阶段热切换供应商 |
| 阶段子步骤工具门控 | 避免 LLM 调用不属于当前阶段的工具 |
| 两级上下文压缩 | 先压工具结果（信息密度低），再压历史 |
| 规则驱动阶段转换摘要 | 去掉额外 LLM 调用，降延迟降成本 |
| 回退快照 | 每次阶段转换存档，支持历史回溯 |
| Hook 系统 | 软评分/验证/压缩与核心循环解耦 |

---

## 17. 测试体系

- **后端单元测试**：62 个文件，覆盖 Agent 循环、LLM 供应商、状态管理、阶段路由、工具执行、存储、压缩、验证、遥测、API
- **E2E 测试**：Playwright, Phase 1 目的地推荐流程 (3 分钟超时)
- **运行**：`cd backend && pytest` / `npx playwright test`

---

*最后更新：2026-04-10 | 当前 HEAD: 见 `git log --oneline -1`*
