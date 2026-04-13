# Travel Agent Pro — 项目全景图

> **用途**：为 AI 模型提供项目大局观。遇到需要全局理解的问题时先读此文件。
> **维护规则**：每次 commit 时同步更新本文件，确保始终反映最新架构。

---

## 1. 一句话定位

**Travel Agent Pro** 是一个基于 LLM 的智能旅行规划 Agent 系统，生产主路径采用 Phase 1/3/5/7 认知决策流（模糊意图 → 方案设计 → 日程组装 → 出发前清单），通过 FastAPI + React 全栈实现，支持 SSE 流式交互、多 LLM 供应商切换、上下文压缩、评估报告和可观测性追踪。

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
| Agent 智能层配置 | quality_gate, parallel_tool_execution, memory（含 extraction/policy/retrieval/storage 子块，运行时读 `config.memory.*`；顶层 `memory_extraction` 仅向后兼容）, guardrails |

---

## 3. 目录结构总览

```
travel_agent_pro/
├── backend/                    # Python 后端
│   ├── main.py                 # FastAPI 入口 (856 行), API 端点, 会话管理, SSE 流
│   ├── config.py               # 配置加载 (.env + config.yaml), 多 LLM 按阶段切换
│   ├── agent/                  # Agent 循环引擎
│   │   ├── loop.py             # 核心循环: LLM→工具执行→阶段转换→修复，集成自省/强制工具/护栏/读工具批量执行
│   │   ├── compaction.py       # 上下文压缩: token 预算计算、渐进式压缩
│   │   ├── hooks.py            # 钩子系统 (before_llm_call, after_tool_call)
│   │   ├── reflection.py       # ReflectionInjector: 关键阶段自省 prompt 注入
│   │   ├── tool_choice.py      # ToolChoiceDecider: 强制 update_plan_state 调用判定
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
│   ├── memory/                 # 结构化 global/trip 记忆 + episode 归档
│   │   ├── models.py           # MemoryItem/MemoryEvent/TripEpisode + 兼容旧 UserMemory
│   │   ├── store.py            # FileMemoryStore: schema v2 JSON/JSONL, migration locks
│   │   ├── manager.py          # MemoryManager: 加载/保存兼容层 + 上下文组装 facade
│   │   ├── extraction.py       # Candidate extraction prompt/parser
│   │   ├── policy.py           # 风险分类、脱敏、合并与写入策略
│   │   ├── retriever.py        # 阶段相关规则检索
│   │   └── formatter.py        # 紧凑记忆提示格式化
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
│   ├── harness/                # 5 层质量守护
│   │   ├── guardrail.py        # 输入/输出护栏 (中文注入检测6模式、长度5000、搜索结果字段分级校验)
│   │   ├── validator.py        # 硬约束检查 + 增量验证 + Phase 3 lock预算门控
│   │   ├── judge.py            # 软评分 [1-5] (score clamping + 解析失败日志)
│   │   └── feasibility.py      # 可行性门控 (30+目的地成本/天数查表)
│   ├── evals/                  # 评估管线
│   │   ├── models.py           # GoldenCase, EvalExecution, CaseResult, SuiteResult
│   │   ├── runner.py           # YAML加载 + 可注入执行器 + 断言评估 + JSON报告
│   │   ├── failure_report.py   # 失败案例 Markdown 报告生成与保存（taxonomy / overview / 场景详情）
│   │   └── golden_cases/       # 23个黄金测试用例 (easy/medium/hard/infeasible)
│   ├── telemetry/              # 可观测性 + 成本追踪
│   │   ├── setup.py            # OpenTelemetry TracerProvider + OTLP 导出
│   │   ├── attributes.py       # 标准化 span 属性与事件名
│   │   ├── decorators.py       # @trace_agent_loop, @trace_tool_call
│   │   └── stats.py            # SessionStats: token用量/模型定价/工具耗时
│   └── tests/                  # pytest 测试套件 (76+ 个测试文件, 590+ 测试)
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
├── docs/                       # 架构文档与学习笔记（含 docs/failure-analysis.md 失败案例报告）
├── scripts/                    # dev.sh/dev-stop.sh + failure-analysis/（live 场景执行与截图采集）+ demo/（deterministic 录屏）
│   └── demo/                   # demo/seed-memory.json + demo-scripted-session.json + run-all-demos.sh + README + demo-full-flow.spec.ts
├── backend/data/               # 本地运行时持久化：sessions.db、sessions/、users/
├── config.yaml                 # 运行时配置 (LLM/API/智能层开关/阈值)
├── docker-compose.observability.yml # Jaeger 一键启动
├── e2e-test.spec.ts            # Playwright E2E 测试
├── AGENTS.md                   # AI Agent 项目规范
├── CLAUDE.md                   # Claude 特定规范
└── PROJECT_OVERVIEW.md         # 👈 本文件
```

---

## 4. 核心架构：Phase 1/3/5/7 认知决策流

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

### Agent 智能层（可插拔）

| 模块 | 定位 | 触发时机 |
|------|------|---------|
| Evaluator-Optimizer | 阶段转换质量门控：硬约束阻断，Phase 3→5 / 5→7 软评分低于阈值时注入修正建议，评分器不可用时放行 | before_phase_transition hook |
| Reflection | 被动式自省提示，会话级去重 | before_llm_call (步骤切换时) |
| Parallel Tool Exec | 读写分离并行调度 | 工具批量执行时 |
| Forced Tool Choice | 强制结构化输出 | LLM 调用前 |
| Memory System | 结构化 global/trip 双 scope 记忆 + episode 归档；后台候选提取；policy 合并与 payment/membership 域阻断 + 证件/联系方式/邮箱/长数字序列全字段 PII 检测脱敏；三路检索（core profile / trip memory / phase-domain）按 trip_id 隔离；新行程回退时轮转 trip_id；受 `memory.enabled` 门控后阶段相关注入 | 每轮 chat 后后台提取；每次 system prompt 构建前检索 |
| Tool Guardrails | 输入/输出护栏，搜索结果缺 `price` 升级为 error，非关键字段缺失保持 warn，支持 `guardrails.disabled_rules` 关闭单条规则 | 工具执行前后 |
| Eval Runner | YAML golden cases + 可注入执行器；`scripts/failure-analysis/run_and_analyze.py` 可对 live backend 逐条执行 failure-* 场景，采集 SSE 回复 / plan state / tool calls / stats，输出 JSON 与 Markdown 分析报告 | 离线/批量评估 |

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
    │   ├─ ReflectionInjector.check_and_inject() → 在 Phase 3 lock / Phase 5 complete 注入自检提示
    │   └─ compact_messages_for_prompt() → token 预算内渐进压缩
    │
    ├─ [ToolChoiceDecider] → 关键决策点可强制 update_plan_state
    ├─ [LLMProvider.chat()] → 流式输出 text_delta + tool_calls
    │
    ├─ [ToolGuardrail + ToolEngine.execute()/execute_batch()] → 工具护栏 + 顺序/并行调度，yield TOOL_RESULT 事件
    │
    ├─ [PhaseRouter.check_and_apply_transition()] → 异步阶段变化检测 + before_phase_transition 质量门控
    │
    ├─ [Hook: after_tool_call]
    │   ├─ validator.validate_incremental() → update_plan_state 后实时检查当前写入字段
    │   ├─ validator.validate_lock_budget() → selected_transport/accommodation 写入后检查交通+住宿预算占比
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

### 工具读写分类
- `side_effect="read"`：搜索/查询类（默认），可并行执行
- `side_effect="write"`：`update_plan_state`, `assemble_day_plan`, `generate_summary`，顺序执行

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
| 决策 | `tool_choice.py` | 根据阶段和对话内容决定是否强制 `update_plan_state` |
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
├── sessions/
│   └── sess_{12-hex}/
│       ├── plan.json              # TravelPlanState 快照
│       ├── snapshots/             # 回退快照
│       └── tool_results/          # 工具结果缓存
└── users/
    └── {user_id}/
        ├── memory.json            # schema v2 结构化 MemoryItem + legacy 兼容数据
        ├── memory_events.jsonl    # accept/reject 等行为事件
        └── trip_episodes.jsonl    # Phase 7 归档后的 TripEpisode
```

---

## 11. API 端点

```
GET  /health                              → 健康检查
POST /api/sessions                        → 创建新会话
GET  /api/sessions                        → 列出所有会话
GET  /api/sessions/{id}                   → 会话元数据
DELETE /api/sessions/{id}                 → 软删除会话
POST /api/chat/{id}                       → 发送消息 (SSE 流式响应)
GET  /api/sessions/{id}/plan (或 /api/plan/{id}) → 获取旅行方案
GET  /api/messages/{id}                   → 获取会话消息历史
POST /api/backtrack/{id}                  → 回退到指定阶段
GET  /api/memory/{user_id}                → 获取结构化记忆项
POST /api/memory/{user_id}/confirm        → 确认 pending 记忆
POST /api/memory/{user_id}/reject         → 拒绝 pending 记忆
POST /api/memory/{user_id}/events         → 追加记忆事件
GET  /api/memory/{user_id}/episodes       → 获取旅行 episode
DELETE /api/memory/{user_id}/{item_id}    → 标记记忆为 obsolete
```

---

## 12. 质量守护 (Harness) — 5 层架构

### Layer 1: 输入护栏 (Guardrail)
- 中文提示注入检测 (6 种正则模式)
- 消息长度限制 (5000 字符)
- 必填字段结构校验；搜索结果缺 `price` 为 error，缺 `airline`/`location`/到达时间等非关键字段为 warn
- 工具结果异常检测

### Layer 2: 硬约束验证器 (Validator)
- 时间冲突：活动结束 + 交通时间 > 下一活动开始
- 预算超支：活动总花费 > 总预算
- 天数超限：计划天数 > 可用天数
- Null 安全守卫：`_time_to_minutes` 返回 `int | None`
- `update_plan_state` 后实时运行 `validate_incremental(plan, field, value)`，只检查当前写入字段相关约束并注入 `[实时约束检查]` system message，不阻断工具执行
- Phase 3 lock 写入 `selected_transport` / `accommodation` 后运行 `validate_lock_budget`：交通+住宿达到预算 80% 时提示剩余活动餐饮空间，超过 100% 时注入错误反馈

### Layer 3: 软评分 (Judge)
- `pace` (1-5): 节奏合理性
- `geography` (1-5): 地理连贯性
- `coherence` (1-5): 逻辑一致性
- `personalization` (1-5): 个性化程度
- Score clamping [1,5] + 解析失败 logger.warning
- 在 `assemble_day_plan`, `generate_summary` 工具之后触发

### Layer 4: 可行性门控 (Feasibility Gate)
- Phase 1→3 转换时触发
- 30+ 目的地最低日消费查表 (`_MIN_DAILY_COST`)
- 目的地最少天数查表 (`_MIN_DAYS`)
- 基于 `DateRange.start/end` 计算旅行天数，避免阶段转换时日期字段别名不一致导致崩溃
- 规则式判断，不消耗 LLM 调用

### Layer 5: 成本与延迟追踪 (Cost Tracker)
- `SessionStats`: 每会话 token 用量、模型定价估算
- OpenAI / Anthropic 流式 USAGE chunk 提取，并由 `AgentLoop` 透传到 API 层记录
- 工具调用 `duration_ms` 监控 (`time.monotonic`)，随 TOOL_RESULT 写入会话级统计
- API 端点: `GET /api/sessions/{id}/stats`

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
| Evaluator-Optimizer | 阶段转换前质量门控，硬约束错误阻断；软评分低于阈值时按 `max_retries` 注入修改建议，评分器异常不阻断主流程 |
| Reflection 自省 | 被动 system message 注入，零额外 LLM 调用，会话级幂等 |
| 并行工具执行 | 读写分离，搜索类并行，状态更新顺序 |
| Forced Tool Choice | 关键决策点强制工具调用，渐进替代 State Repair |
| Memory System | schema v2 结构化记忆（global/trip 双 scope）；每轮 chat 后按 trigger 后台提取候选；policy 全字段 PII 检测脱敏（payment/membership 域直接阻断、邮箱正则、9-18 位数字序列、证件/联系方式短语检测 + `_redact_for_storage` 递归脱敏）；合并/确认流程；system prompt 前按 `memory.enabled` 三路检索（core profile / trip memory / phase-domain，硬编码 core_limit=10, phase_limit=8）按 trip_id 隔离；新行程回退 obsolete 旧 trip memory 并轮转 trip_id；Phase 7 幂等归档 episode（JSONL 独立存储，不参与 prompt 注入检索）|
| Tool Guardrails | 确定性规则校验 + 中文注入检测 + 工具结果字段分级校验，不依赖 LLM，可按规则名禁用 |

---

## 17. 测试体系

- **后端单元测试**：78+ 个文件、610+ 测试，覆盖 Agent 循环、LLM 供应商、状态管理、阶段路由、工具执行、存储、压缩、验证、遥测、护栏、可行性、评估管线
- **评估管线**：23 个黄金测试用例 (YAML)，6 种断言类型，离线评估 runner
- **E2E 测试**：Playwright，根目录 `e2e-test.spec.ts` 覆盖 live Phase 1 主流程；根目录 `playwright.config.ts` 在显式指定脚本时只运行对应的 `scripts/failure-analysis/capture_screenshots.ts` 或 `scripts/demo/demo-full-flow.spec.ts`，并忽略 `.worktrees/`；其中 demo spec 基于 `demo-scripted-session.json` mock `/api/sessions`、`/api/plan`、`/api/messages`、`/api/chat`，稳定回放 Phase 1 → Phase 3（显式选择住宿候选）→ Phase 5 → backtrack，只需要 frontend dev server，并把截图/视频写入 `screenshots/demos/`；failure-analysis raw results 写入 `scripts/failure-analysis/results/`，该目录为本地生成产物，不提交到 git
- **运行**：`cd backend && pytest` / `npx playwright test`

---

*最后更新：2026-04-13 | 当前 HEAD: 见 `git log --oneline -1`*
