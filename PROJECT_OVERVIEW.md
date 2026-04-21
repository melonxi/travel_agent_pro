# Travel Agent Pro — 项目全景图

> **用途**：为 AI 模型提供项目大局观。遇到需要全局理解的问题时先读此文件。
> **维护规则**：只反映**当前架构**，不记录变更历史。具体参数、代码行数、UI 文案、实现细节请读代码；改动历史请读 git log。

---

## 1. 一句话定位

**Travel Agent Pro** 是一个基于 LLM 的智能旅行规划 Agent 系统，生产主路径采用 Phase 1/3/5/7 认知决策流（模糊意图 → 方案设计 → 日程组装 → 出发前清单），通过 FastAPI + React 全栈实现，支持 SSE 流式交互、多 LLM 供应商切换、上下文压缩、评估报告和可观测性追踪；Phase 7 结束时冻结 `travel_plan.md` 和 `checklist.md` 两个 markdown 交付物。

---

## 2. 技术栈速览

| 层级 | 技术 |
|------|------|
| 后端 | Python 3.12+, FastAPI, Uvicorn, async/await |
| 前端 | TypeScript, React 19, Vite 6, Leaflet；聊天流支持工具卡、思考态、并行进度和内部系统任务卡 |
| LLM | OpenAI (gpt-4o) + Anthropic (Claude Sonnet 4)，按阶段切换 |
| 持久化 | aiosqlite（会话/消息）、JSON 文件（旅行方案快照） |
| 可观测性 | OpenTelemetry + Jaeger (OTLP gRPC) |
| 测试 | pytest + pytest-asyncio（后端）、Playwright（E2E） |
| 外部服务 | Tavily、小红书 CLI、FlyAI CLI、Google Maps、Amadeus、OpenWeather |

---

## 3. 目录结构总览

```
travel_agent_pro/
├── backend/                    # Python 后端
│   ├── main.py                 # FastAPI 入口，API 端点，会话管理，SSE 流
│   ├── run.py                  # RunRecord / IterationProgress（LLM 韧性追踪）
│   ├── config.py               # 配置加载（.env + config.yaml）
│   ├── agent/                  # Agent 循环：loop / compaction / hooks / internal_tasks / reflection / tool_choice / narration / types / orchestrator / day_worker / worker_prompt
│   ├── llm/                    # LLM 抽象：base Protocol / errors / factory / openai_provider / anthropic_provider
│   ├── state/                  # 旅行状态模型：models / manager / intake / plan_writers
│   ├── memory/                 # v3 分层记忆：profile / working memory / episode slice + 兼容层：models / store / manager / extraction / policy / retriever / formatter
│   ├── context/                # 上下文：manager（系统提示/压缩决策）+ soul.md（人格）
│   ├── phase/                  # 阶段路由：router / prompts（skill-card 架构，GLOBAL_RED_FLAGS + PHASE{1,3,5,7}_PROMPT + build_phase3_prompt）/ backtrack
│   ├── tools/                  # 领域工具：base / engine / plan_tools(聚合导出 + Phase 1/3 trip_basics + append_tools + Phase 3 强 schema + Phase 5 daily_plans + 回退工具) / 搜索类 / 规划类 / normalizers
│   ├── storage/                # SQLite 层：database / session_store / message_store / archive_store
│   ├── harness/                # 5 层守护：guardrail / validator / judge / feasibility
│   ├── evals/                  # 评估管线：models / runner / stability / failure_report / golden_cases
│   ├── telemetry/              # 可观测 + 成本：setup / attributes / decorators / stats
│   ├── api/                    # API 模块：trace 视图构建
│   └── tests/                  # pytest 测试套件（含 test_plan_tools/ Phase 1/3/5 写工具专项）
│
├── frontend/                   # React 前端
│   ├── src/
│   │   ├── App.tsx             # 应用壳：三栏布局，主题，Plan/Trace 标签
│   │   ├── components/         # ChatPanel / TraceViewer / MessageBubble / SessionSidebar / PhaseIndicator / Phase3Workbench / MapView / Timeline / BudgetChart / RoundSummaryBar / MemoryCenter / ThinkingBubble …
│   │   ├── hooks/              # useSSE / useMemory / useTrace
│   │   ├── types/              # plan / session / memory / trace
│   │   └── styles/             # "Solstice" 暗色玻璃设计系统
│   └── vite.config.ts          # /api → localhost:8000 代理
│
├── docs/                       # 架构文档、问题修复记录、事故复盘与学习笔记
├── scripts/                    # dev.sh / dev-stop.sh / eval-stability.py / failure-analysis / demo
├── backend/data/               # 本地持久化：sessions.db / sessions/ / users/
├── config.yaml                 # 运行时配置（LLM / API / 智能层开关 / 阈值）
├── docker-compose.observability.yml
└── e2e-*.spec.ts               # Playwright E2E 套件（主流程 / 重试体验 / 等待体验）
```

---

## 4. 核心架构：Phase 1/3/5/7 认知决策流

```
用户消息 → Phase 1 → Phase 3 → Phase 5 → Phase 7
            需求收集   方案设计   行程组装   出发前查漏
           (目的地)  (4 子步骤)  (日程详排)  (检查清单)
```

### Phase 1 — 灵感与目的地收敛（skill-card）
- 角色：旅行灵感顾问；目标：用最少轮次收敛到一个目的地
- **回复纪律**："先查后说"——每条建议须有工具查询结果支撑；回复≤150字；一次只给2-3个选项
- 工具：`xiaohongshu_search_notes` / `xiaohongshu_read_note` / `xiaohongshu_get_comments`、`web_search`、`quick_travel_search`、`update_trip_basics`（状态写入）
- **工具契约**：小红书搜索采用三工具操作模型（`xiaohongshu_search_notes` 导航层 → `xiaohongshu_read_note` 信息层 → `xiaohongshu_get_comments` 评价层），强调深度使用而非浅尝辄止；状态写入契约明确何时写 `update_trip_basics`/`add_preferences`/`add_constraints`，禁止把推荐写入状态
- **Prompt 已迁移**：Phase 1 改为使用 `update_trip_basics` 收口基础行程信息，并在需要时配合 `add_preferences` / `add_constraints`
- **完成 Gate**：`destination` 非空 → 自动进入 Phase 3
- 产出：`destination`、可选的 `budget`/`travelers`/`dates`

### Phase 3 — 框架规划（4 个子步骤，按子步骤动态拼装 prompt）
- **brief** → 建立旅行画像（目标/节奏/约束/必做-避免）；收敛压力：≤2轮完成
- **candidate** → 候选池构建与筛选；"锚定扩展→逐项验证→筛选成短名单"三步走流程；以 trip_brief 为硬锚点，先扩展后验证再筛选；存疑候选项须用 `xiaohongshu_read_note` / `xiaohongshu_get_comments` 深度验证
- **skeleton** → 骨架方案（非逐小时）；"经验采集→骨架生成"两阶段流程；生成骨架前必须先搜索真实攻略提取策略（区域分组、天数分配、体力节奏），再结合 shortlist 和 trip_brief 生成 2-3 套差异方案；日级结构化字段：`area_cluster`（必填）、`locked_pois`（必填）、`candidate_pois`（必填，单天专属候选池）；同一 skeleton 内 POI 在 `locked_pois`/`candidate_pois` 间必须全局唯一；可选 `excluded_pois` / `date_role` / `mobility_envelope` / `fallback_slots`
- **lock** → 锁定交通 + 住宿；大交通确认时间后即锁
- **回复纪律**：回复≤200字、问题集中在回复末尾、结论前置、trip_brief 作为画像硬锚点约束所有后续决策
- **输出协议**：每个子阶段 prompt 开头注入 `⚠️ 输出协议`——正面指令（先工具后文字）、必须调用的工具、严禁行为
- **工具职责对照表**：PHASE3_BASE_PROMPT 中包含 10 行"你想做什么 → 应该调用 → 不要调用"映射表，防止工具混用（如用 `set_trip_brief` 记录骨架选择）
- **通用工具纪律**：小红书三层操作模型（导航层/信息层/评价层）、即使成熟目的地也不跳过 UGC 搜索
- **工具门控**：每个子步骤只暴露该阶段所需的工具子集，并向前开放下一阶段写入工具实现"前瞻容错"（如 brief 阶段可前瞻写入 `set_candidate_pool`/`set_shortlist`，candidate 阶段可前瞻写入 `set_skeleton_plans`/`select_skeleton`），防止 LLM 跳阶时工具不可用导致状态丢失
- **Prompt 已迁移**：brief 用 `set_trip_brief` / `add_preferences` / `add_constraints`，candidate 用 `set_candidate_pool` / `set_shortlist`，skeleton 用 `set_skeleton_plans` / `select_skeleton`，回退用 `request_backtrack`
- **Prompt 拼装**：`build_phase3_prompt(step)` = `PHASE3_BASE_PROMPT` + `PHASE3_STEP_PROMPTS[step]` + `GLOBAL_RED_FLAGS`
- 产出：`trip_brief`、`candidate_pool`、`skeleton_plans`、`selected_skeleton_id`、交通/住宿

### Phase 5 — 日程详排（skill-card，路径规划定位）
- 核心定位：路径规划优化问题——最小化无效移动，最大化体验密度
- **双执行模式**：
  - **串行模式**（默认回退）：AgentLoop 内 LLM 逐日生成，与原有流程一致
  - **并行 Orchestrator-Workers 模式**：Python Orchestrator（纯代码调度器，非 LLM）将骨架拆分为 N 个 DayTask，经 `_compile_day_tasks` 注入跨天约束（forbidden_pois / mobility_envelope / date_role），并行派发 N 个 Day Worker（轻量 LLM Agent，独立上下文），收集结果后做全局验证（POI 去重 / 预算检查 / 天数覆盖 / 时间冲突 / 语义去重 / 交通衔接 / 节奏匹配），error 级问题触发最多 1 轮 re-dispatch（注入 repair_hints 重跑受影响天），最后统一写入 `replace_all_daily_plans`
  - 并行模式通过 `config.yaml` 的 `phase5.parallel` 段控制（`enabled` / `max_workers` / `worker_timeout_seconds` / `fallback_to_serial`）
  - Worker 共享相同 system prompt prefix → KV-Cache 命中率 ~93.75%（Manus pattern）
  - Worker 只有只读工具，写入由 Orchestrator 统一完成
  - 失败率 >50% 自动降级到串行模式
- **Day Worker 提示词止血策略**：Day Worker 具备有限补救 + 保守落地的收敛策略——当 JSON 输出格式异常或工具调用失败时，先尝试有限次数的补救（如 JSON 修复），补救失败则保守落地（返回当前已有结果而非无限重试）
- **Day Worker loop 保护机制**：Worker 内部循环具备四重收敛保障——**重复查询抑制**（同 query 滑动窗口去重，避免搜索死循环）、**补救链阈值**（连续补救轮次上限，超限即保守落地）、**后半程强制收口**（迭代过半后强制聚焦已有结果，不再启动新搜索）、**JSON 修复回合**（输出 JSON 解析失败时限定修复轮次，避免在格式修复上无限循环）
- **Worker 失败错误类别**：Worker 失败时输出结构化错误码，便于 Orchestrator 诊断与降级决策——`REPEATED_QUERY_LOOP`（重复查询死循环被抑制）、`RECOVERY_CHAIN_EXHAUSTED`（补救链耗尽仍无法恢复）、`JSON_EMIT_FAILED`（JSON 输出经修复回合仍无法解析）、`TIMEOUT`（Worker 超时）、`LLM_ERROR`（LLM 调用不可恢复错误）、`NEEDS_PHASE3_REPLAN`（locked_pois 全部不可行，需回退 Phase 3 重调骨架）
- **Worker 约束注入**：DayTask 携带 `locked_pois`/`candidate_pois`/`forbidden_pois`/`area_cluster`/`mobility_envelope`/`date_role`/`repair_hints` 等约束字段，由 `_build_constraint_block` 渲染为中文 prompt 硬约束块注入 Worker 上下文
- **增量生成策略**（串行模式）：按1-2天增量调用 `assemble_day_plan`，非一次性全量
- **Prompt 已迁移**：Phase 5 使用 `optimize_day_route`（路线辅助，不写状态）、`save_day_plan` / `replace_all_day_plans`（状态写入）与 `request_backtrack`（回退）
- 流程：expand（骨架→日期）→ assemble（活动+时间）→ validate（开放/距离/天气/预算）→ commit
- 产出：`daily_plans[]`，每天含完整 Activity 列表
- Phase 5+ 上下文中 trip_brief 注入时排除 `dates`/`total_days`（已由 `plan.dates` 权威提供），避免重复信号
- 运行时上下文必须注入骨架内容、`trip_brief` 字段、偏好和约束
- **并行模式新增文件**：`agent/orchestrator.py`（调度器核心）、`agent/day_worker.py`（单日 Worker 执行引擎）、`agent/worker_prompt.py`（共享前缀 + 日别后缀模板）

### Phase 7 — 出发前查漏（skill-card）
- 角色：出发前查漏官；扫描全计划，生成带优先级的检查清单
- 工具：`check_weather`、`check_availability`、`search_travel_services`、`web_search`、`request_backtrack`（仅在发现严重问题需回退时使用）
- **Prompt 已迁移**：Phase 7 直接使用 `request_backtrack`，并在收口时调用 `generate_summary(plan_data, travel_plan_markdown, checklist_markdown)`
- 扫描维度：证件签证、天气、预订确认、交通接驳、应急预案
- **完成 Gate**：所有高优先级项解决 → `generate_summary` 冻结双 markdown 交付物

### GLOBAL_RED_FLAGS
所有阶段 prompt 末尾统一注入跨阶段通用禁令（如"不捏造信息"、"不越阶段边界"等），通过 `PhaseRouter.get_prompt_for_plan()` 和 `build_phase3_prompt()` 自动拼装。**Prompt 已迁移**：通用规则改为引用"状态写入工具"泛指（第一条规则）与 `request_backtrack(to_phase=..., reason=\"...\")`（第二条规则）。

### 阶段转换机制
- `PhaseRouter.infer_phase(plan)` 根据字段填充情况推断当前阶段
- **Phase 3→5 门控**：`_skeleton_days_match()` 校验已选骨架天数与 `dates.total_days`（inclusive 自然日语义）一致，不一致时拒绝进入 Phase 5
- `_hydrate_phase3_brief()` 中 `dates`/`total_days` 使用强制覆盖（非 `setdefault`），防止用户修改日期后 trip_brief 中残留 stale 值
- 自动转换 + 遥测事件记录
- 支持 Backtrack（回退至早期阶段，清除下游数据，轮转 trip_id）

### Agent 智能层（可插拔）

| 模块 | 定位 | 触发时机 |
|------|------|---------|
| Evaluator-Optimizer | 阶段转换质量门控：硬约束阻断，软评分低于阈值时注入修正建议；质量门控过程以 `quality_gate` 内部任务进入聊天流 | before_phase_transition hook |
| Reflection | 被动自省提示，会话级去重 | before_llm_call（步骤切换时） |
| Parallel Tool Exec | 读写分离并行调度，parallel_group ID 透传到 Stats 层 | 工具批量执行时 |
| Tool Choice (always auto) | Phase 切分后总返回 "auto"，依赖提示纪律 | LLM 调用前 |
| Memory System | v3 profile / working memory / episode slice 分层记忆；当前旅行事实由 TravelPlanState 权威提供；query-aware symbolic recall 只在显式历史/偏好查询时触发；回答前同步执行 `memory_recall`，用户消息一进入 chat 就提交后台 `memory_extraction_gate` / `memory_extraction` job，chat 与提取彻底解耦；提取采用 session 级 latest-wins coalescing queue，gate 进一步路由到 split 的 `extract_profile_memory` / `extract_working_memory` 工具，避免连续多条消息堆积重任务 | system prompt 构建前检索；每轮 chat 追加 user message 后立即后台排队 gate/job |
| Tool Guardrails | 输入/输出护栏，可按规则名禁用 | 工具执行前后 |
| Eval Runner | YAML golden cases + 可注入执行器；支持 pass@k 稳定性评估；测试中的 golden case 路径按文件位置解析，避免 cwd 依赖 | 离线/批量评估 |

---

## 5. 核心数据流

```
用户消息 (POST /api/chat/{id})
    ↓
[main.py] 加载会话+方案，追加 user message
    ├─ 同步：`memory_recall` → system prompt
    └─ 异步：提交 session 级 memory job snapshot
    ↓
[AgentLoop.run()] 进入迭代循环
    │
    ├─ Phase 5 并行分流检查（should_use_parallel_phase5）
    │   └─ 条件满足 → Phase5Orchestrator.run() → split → spawn workers → collect → validate → write → return
    │
    ├─ 取消检查点（迭代开始 / LLM 流式 chunk 前 / 工具执行前）
    │
    ├─ [Hook: before_llm_call]
    │   ├─ ContextManager.build_system_message() → soul + 阶段提示 + 状态快照
    │   ├─ ReflectionInjector → 关键阶段自省提示
    │   └─ compact_messages_for_prompt() → token 预算内渐进压缩
    │
    ├─ [ToolChoiceDecider] → 当前始终返回 "auto"（split 工具后移除强制）
    ├─ [LLMProvider.chat()] → 流式输出 text_delta + tool_calls
    │
    ├─ [ToolGuardrail + ToolEngine.execute/execute_batch] → 顺序/并行调度
    │
    ├─ [PhaseRouter] → 阶段变化检测 + 转换前质量门控
    │
    ├─ [Hook: after_tool_call]
    │   ├─ validator.validate_incremental → 实时约束检查
    │   └─ validator.validate_lock_budget → 交通+住宿预算占比检查
    │
    ├─ yield TOOL_RESULT → SSE 工具卡结束
    │
    ├─ [Hook: after_tool_result]
    │   └─ SoftJudge → pace/geography/coherence/personalization 评分，以 `soft_judge` 内部任务展示
    │
    └─ yield LLMChunk → SSE → 前端
        ↓（异常或取消时）
    [RunRecord] → 运行状态 + can_continue 判定 + continuation_context 保存
```

### Pending system notes
工具执行阶段产生的 SYSTEM 消息（如实时约束检查）不会立刻 append 到消息历史，而是缓存到 session 级缓冲区，在下一次 LLM 调用前统一 flush。目的是保证 `assistant.tool_calls → 全部 tool 答复` 的协议序列原子性；并行 tool_calls 期间任何 SYSTEM 都落在整组 tool 之后、下一次 assistant 之前。缓冲区不落盘。

### Internal task stream
后端用 `agent.internal_tasks.InternalTask` + `ChunkType.INTERNAL_TASK` 表达非用户工具但会消耗时间或影响上下文的运行时任务。当前有两条通道：
- chat SSE `/api/chat/{id}`：承载与当前回答强绑定的任务，例如 `memory_recall`、`soft_judge`、`quality_gate`
- background internal-task SSE `/api/internal-tasks/{id}/stream`：承载与回答解耦的后台任务，例如 `memory_extraction_gate`、`memory_extraction`

前端 `ChatPanel` 按 `task.id` 合并生命周期更新，并维护跨流共享的 `task.id -> message.id` 映射；这样同一个后台任务即使在 chat `done` 之后才结束，也会回写到原卡片，而不是再长出一张重复卡。`MessageBubble` 渲染为系统任务卡，和真实工具卡保持视觉与语义区隔。

进入聊天流的内部任务包括：`soft_judge`（工具结果后的行程质量评审）、`quality_gate`（阶段推进检查）、`context_compaction`（上下文整理）、`reflection`（自检提示注入）、`phase5_orchestration`（Phase 5 并行编排）和 `memory_recall`（本轮记忆召回）。进入后台 internal-task 流的任务包括：`memory_extraction_gate`（轻量判断是否值得提取）和 `memory_extraction`（正式记忆候选提取）。`save_day_plan` / `replace_all_day_plans` 等真实工具的 `TOOL_RESULT` 会先到达前端并结束工具卡，随后才显示软评审或后台记忆任务，避免用户误以为真实工具仍在执行。

### 文档沉淀约定
- `docs/phase*.md`、`docs/*fix*.md`：专题修复记录与设计说明
- `docs/postmortems/`：事故复盘，记录用户可见故障、根因链路、放大因素与后续动作
- `docs/postmortems/2026-04-19-phase5-parallel-guard-refactor.md`：记录 Phase 5 并行入口守卫重构的主路径等价性、`max_retries` 边界风险与外部 agent runtime 设计参照
- `docs/learning/interview-stress-test/`、`docs/mind/`：学习型架构评审与阶段性洞察，当前包含记忆系统写入语境、稳定性、TripEpisode 职责边界与 working memory 取舍分析
- `docs/learning/2026-04-19-Phase*.md` 与 `docs/learning/assets/phase5-parallel-orchestration/`：面向初学者的 Phase 转换机制、Phase 5 并行 Orchestrator-Workers 生命周期说明和配图
- `docs/superpowers/specs/`、`docs/superpowers/plans/`：规格与实施计划，包含待实现的 Memory Storage v3 分层重构规格与实施计划（profile / working memory / episode slice / events）、Memory Extraction Routing 设计（把 combined extraction 拆成 routing gate + profile / working memory 专用 extractor），以及 Phase 3 `candidate_pois` 全局唯一性设计与实施计划（把重复 POI 拦截在 `set_skeleton_plans` 写入边界，而不是留给 Phase 5 并行 worker 事后去重）
- `docs/superpowers/specs/2026-04-19-internal-task-visibility-design.md`：内部耗时任务可见性设计，定义 `internal_task` SSE、系统任务卡片、soft judge / quality gate / memory / compaction / reflection / Phase 5 orchestration 的统一聊天流展示模型
- `docs/agent-tool-design-guide.md`：Agent 工具设计评审准则，新增或重塑工具前应对照其命名、schema、返回值、错误反馈与评估清单

---

## 6. 上下文压缩机制

### 两层压缩策略
1. **before_llm_call 预压缩**：按 `context_window - max_output_tokens` 预留出预算，超出部分按渐进阈值压缩——先压工具结果（信息密度低），最后才对历史做摘要。
2. **阶段转换交接**：前进切换时不再注入历史摘要，而是注入一条确定性的 handoff note，交代当前阶段、已完成事项、当前唯一目标、禁止重复事项，以及"开场白协议"（要求下一次回复先用 1-2 句自然语言承上启下，禁止 `[Phase N 启动]`/`前置条件检查：✓` 式机器感 checklist 开场）；前进与回退都会保留触发转换的原始用户消息，避免新阶段变成 assistant-only 历史并丢失当前任务。
3. **Phase 3 子阶段切换**（brief→candidate→skeleton→lock）：也会触发 system message 重建（无 handoff note / backtrack notice），确保 runtime context 随子阶段即时刷新。Runtime context 中：Phase 7 展开 daily_plans 每日活动（出发前查漏需要），Phase 3 skeleton 子阶段展开骨架紧凑摘要（id/name/tradeoffs/每天 theme），preferences/constraints 自 Phase 1 起即注入。

### 工具结果特定压缩
`web_search` / `xiaohongshu_search_notes` / `xiaohongshu_read_note` / `xiaohongshu_get_comments` 按工具类型裁剪摘要长度与条数，保留信息骨架；具体阈值见 `backend/agent/compaction.py`。

---

## 7. LLM 抽象与多供应商

```python
class LLMProvider(Protocol):
    async def chat(messages, tools, stream) → AsyncIterator[LLMChunk]
    async def count_tokens(messages) → int
    async def get_context_window() → int | None
```

`config.yaml` 支持按阶段覆写 provider 与 model（例如 Phase 1/2 用 Anthropic，Phase 5 用 OpenAI）。

### LLM 错误归一化与韧性

三层韧性架构：**错误归一化 → 停止生成 → 安全继续**

```
LLM API 异常
    ↓
[Provider._classify_error] → LLMError(code, retryable, provider, status_code)
    ├─ TRANSIENT / RATE_LIMITED → 自动重试；已 yield 数据后不再重试
    ├─ BAD_REQUEST / STREAM_INTERRUPTED / PROTOCOL_ERROR → 不重试，通知用户
    └─ 裸 APIError → classify_opaque_api_error 兜底（状态码/关键词/保守 TRANSIENT）
    ↓
[main.py] → SSE error 事件（error_code / retryable / can_continue / user_message）
    ↓
[RunRecord] → 基于 IterationProgress 判定 can_continue
    ↓
[前端] → 停止按钮 / 继续按钮 / 未完成消息标注
```

**关键安全机制**：流式 generator 已 yield 数据后禁止重试，避免重复输出。

---

## 8. 工具系统

- `@tool` 装饰器声明名称、描述、可用阶段、参数 schema
- `ToolEngine` 按阶段/子步骤过滤可用工具后传给 LLM；运行时通过 `make_all_plan_tools(plan)` 一次性注册 17 个 plan-writing tools；`PLAN_WRITER_TOOL_NAMES` 同时驱动 `AgentLoop` 的 state-write 判定，确保所有 writer 都会触发 `check_and_apply_transition`
- 错误处理：`ToolError` 带 `error_code` + `suggestion` 反馈给 LLM；`ToolEngine.execute()` 在函数调用前基于 schema `required` 字段做预校验，缺参直接返回 `INVALID_ARGUMENTS`（而非走 Python TypeError 路径），确保空参数调用被拦截
- `ToolGuardrail` 在写入前拦截提示注入、空字段、日期回溯与非法预算；`update_trip_basics.budget` 支持数值、对象与可解析的数值字符串，非正数/非数字会被拒绝
- **读写分类**：`side_effect="read"`（搜索/查询、`search_travel_services`、`optimize_day_route` 等）并行执行；`side_effect="write"`（17 个 plan-writing tools，以及 `generate_summary`）顺序执行
- **状态写入分层**：`backend/state/plan_writers.py` 提供共享的纯函数 mutation layer；`tools.plan_tools.*` 负责参数 schema、输入规范化与错误边界，再委托到共享 writer 完成落盘
- **运行时写后处理**：`backend/main.py` 把 `PLAN_WRITER_TOOL_NAMES` 统一视作状态写工具；所有 writers 都会触发 `validate_incremental` / `validate_lock_budget`、SSE `state_update`、accept memory 事件、以及 backtrack rebuild。若工具结果显式返回 `previous_value` / `new_value`，`SessionStats.state_changes` 优先记录工具返回的规范化值；`request_backtrack` 只保留 rebuild/transition 语义，不伪造字段 diff
- **Phase 3 工具门控**：brief → candidate → skeleton → lock 逐级放开工具子集，并把 split plan tools 纳入白名单（如 brief 的 `set_trip_brief` / `add_preferences` / `add_constraints`，lock 的交通住宿锁定工具）；每个子阶段向前开放下一阶段写入工具实现"前瞻容错"（brief 可写 `set_candidate_pool`/`set_shortlist`，candidate 可写 `set_skeleton_plans`/`select_skeleton`）；`phase3_step` 由路由推断
- **Phase 3 工具描述**：所有 11 个 Phase 3 工具的 `description` 采用四段式结构——功能说明 / 触发条件 / 禁止行为 / 写入后效果，引导 LLM 正确选择工具
- **Phase 3 状态修复**：`AgentLoop` 覆盖全部 4 个子阶段的状态修复——brief 检测画像描述未写入、candidate 检测候选池/短名单缺失及跳阶信号、skeleton 检测骨架信号词但状态为空、lock 按类别（交通/住宿/风险/备选）独立检测；每个子阶段允许两次修复尝试（首次 + retry）
- **重复搜索拦截**：`AgentLoop._should_skip_redundant_update` 检测 `web_search`/`xiaohongshu_search_notes`/`quick_travel_search` 的同 query/keyword 重复调用（≥2 次），维护最近 20 条搜索记录滑动窗口，跳过时返回 `REDUNDANT_SEARCH` 错误码

### 工具清单

| 类别 | 工具 |
|------|------|
| 状态 | `tools.plan_tools`（统一聚合导出 `PLAN_WRITER_TOOL_NAMES` 与 `make_all_plan_tools(plan)`，运行时批量注册 17 个状态写工具）、`tools.plan_tools.trip_basics`（Phase 1/3 基础行程写工具；更新 destination/dates/travelers/budget/departure_city，并在写入前复用 intake parser 做可解析性校验）、`tools.plan_tools.append_tools`（Phase 1/3/5 追加型写工具；追加 preferences/constraints；其中 preferences 兼容 `{key, value}` 与 legacy loose dict 展开语义，并在 wrapper 层做输入类型校验）、`tools.plan_tools.phase3_tools`（Phase 3 强 schema 写工具工厂；对骨架 id/name 做规范化，days 每天强制 `area_cluster`/`locked_pois`/`candidate_pois` 三必填字段，拒绝空 days 数组，并在单个 skeleton 写入边界内校验 `locked_pois`/`candidate_pois` 的 POI 全局唯一性；同时兼容 legacy 选择态、冲突检测与歧义回退）、`tools.plan_tools.daily_plans`（Phase 5 逐日行程写工具；`save_day_plan` 负责新增/替换单日，`replace_all_day_plans` 负责完整覆盖全量日程，并校验 day/date/activity/notes schema；写入后即时调用 `validate_day_conflicts` 检测时间冲突，在返回 dict 中包含 `conflicts` 和 `has_severe_conflicts` 字段，形成模型自修复闭环）、`tools.plan_tools.backtrack`（`request_backtrack` 阶段回退写工具，薄封装 `state.plan_writers.execute_backtrack`） |
| 搜索 | `xiaohongshu_search_notes`、`xiaohongshu_read_note`、`xiaohongshu_get_comments`、`web_search`、`quick_travel_search` |
| 交通 | `search_flights`（Amadeus + FlyAI 双源融合）、`search_trains`、`calculate_route` |
| 住宿 | `search_accommodations` |
| POI | `get_poi_info`（双源降级）、`check_availability` |
| 行程 | `assemble_day_plan`、`check_feasibility` |
| 辅助 | `check_weather`、`search_travel_services`、`generate_summary` |

#### 17 个状态写工具

| 阶段 | 工具 |
|------|------|
| Phase 1 / 共用基础 | `update_trip_basics`、`add_preferences`、`add_constraints` |
| Phase 3 brief | `set_trip_brief` |
| Phase 3 candidate | `set_candidate_pool`、`set_shortlist` |
| Phase 3 skeleton | `set_skeleton_plans`、`select_skeleton` |
| Phase 3 lock | `set_transport_options`、`select_transport`、`set_accommodation_options`、`set_accommodation`、`set_risks`、`set_alternatives` |
| Phase 5 | `save_day_plan`、`replace_all_day_plans` |
| 跨阶段 | `request_backtrack` |

---

## 9. 前端架构

### 三栏布局
```
┌──────────────┬──────────────────────┬──────────────────────────┐
│ SessionSidebar│    ChatPanel         │      RightPanel          │
│ 会话列表       │  聊天 + 工具卡片      │ Phase3Workbench / Map /  │
│ + 记忆入口     │  SSE 流式渲染         │ Timeline / BudgetChart   │
└──────────────┴──────────────────────┴──────────────────────────┘
```

### SSE 事件类型

| 事件 | 含义 |
|------|------|
| `text_delta` | 助手文本增量 |
| `tool_call` / `tool_result` | 工具调用与结果 |
| `phase_transition` | 阶段/Phase 3 子步骤切换信号（可先于 `state_update` 到达） |
| `agent_status` | ThinkingBubble 状态（thinking/summarizing/compacting + hint 旁白） |
| `state_update` | 完整 TravelPlanState（含 `deliverables` 冻结元数据） |
| `context_compression` | 压缩通知 |
| `internal_task` | 内部任务生命周期更新（chat 流与后台 internal-task 流共用 payload） |
| `memory_recall` | 本轮命中的结构化记忆召回结果（`sources`、`profile_ids`、`working_memory_ids`、`slice_ids`、`matched_reasons`） |
| `error` | LLM 错误（含 retryable / can_continue） |
| `keepalive` | 心跳 |
| `done` | 流结束（含 run 状态 + can_continue） |

### 关键组件

- **ChatPanel** — SSE 流消费，工具卡渲染，staleness 检测，memory chip、轮次摘要、停止/继续/重发交互
- **TraceViewer** — 分阶段分组的 Trace 视图，按 significance 分级展示，连续 thinking 自动折叠
- **Phase3Workbench** — 旅行画像/候选池/骨架/锁定/风险 五卡片
- **ThinkingBubble** — stage-aware 等待气泡，展示 narration hint
- **MemoryCenter** — 右滑抽屉；当前同时读取 v3 `profile` / `working-memory` / `episode-slices` 与遗留 v2 pending/episode 数据
- **MapView / Timeline / BudgetChart** — 地图、时间线、预算可视化
- **useSSE / useMemory / useTrace** — SSE 连接、记忆 CRUD、Trace 拉取三个 Hook

### 设计系统 "Solstice"
暗色玻璃质感 + 琥珀色暖光点缀。

---

## 10. 数据持久化

### SQLite Schema
```sql
sessions       → session_id, user_id, title, phase, status, last_run_*
messages       → id, session_id, role, content, tool_calls, tool_call_id, seq
plan_snapshots → id, session_id, phase, plan_json, created_at
archives       → id, session_id, plan_json, summary, created_at
```

`TravelPlanState.deliverables` 保存 Phase 7 冻结后的元数据，指向 `travel_plan.md` 与 `checklist.md` 的文件名。

### 文件系统
```
backend/data/
├── sessions.db
├── sessions/sess_*/          # plan.json + snapshots/ + tool_results/ + deliverables/
└── users/{user_id}/          # memory/（profile.json、sessions/*/working_memory.json、episode_slices.jsonl） + memory.json + memory_events.jsonl + trip_episodes.jsonl
```

---

## 11. API 端点

```
GET    /health
POST   /api/sessions                        创建
GET    /api/sessions                        列表
GET    /api/sessions/{id}                   元数据
DELETE /api/sessions/{id}                   软删除
POST   /api/chat/{id}                       发送消息（SSE）
POST   /api/chat/{id}/cancel                取消生成
POST   /api/chat/{id}/continue              安全继续
GET    /api/internal-tasks/{id}/stream      后台 internal task 流（SSE）
GET    /api/sessions/{id}/plan              获取方案
GET    /api/sessions/{session_id}/deliverables/{filename}  下载 frozen markdown 交付物
GET    /api/messages/{id}                   消息历史
POST   /api/backtrack/{id}                  回退阶段
GET    /api/sessions/{id}/trace             Trace 视图
GET    /api/sessions/{id}/stats             成本与延迟
GET    /api/memory/{user_id}                v2 记忆项（deprecated）
GET    /api/memory/{user_id}/profile        v3 长期画像
GET    /api/memory/{user_id}/episode-slices v3 历史切片
GET    /api/memory/{user_id}/sessions/{session_id}/working-memory v3 会话工作记忆
POST   /api/memory/{user_id}/confirm        兼容确认入口（legacy pending / v3 profile）
POST   /api/memory/{user_id}/reject         兼容拒绝入口（legacy pending / v3 profile）
POST   /api/memory/{user_id}/events         追加事件
GET    /api/memory/{user_id}/episodes       v2 旅行 episode（deprecated）
DELETE /api/memory/{user_id}/{item_id}      兼容删除入口（legacy / v3 profile）
```

---

## 12. 质量守护（Harness）—— 5 层架构

1. **输入护栏 Guardrail** — 中文注入检测、消息长度限制、搜索结果字段分级校验、工具结果异常检测；预算负值检测扩展到 `update_trip_basics`
2. **硬约束验证器 Validator** — 时间冲突/预算超支/天数超限；所有 `PLAN_WRITER_TOOL_NAMES` 成功写入后都会走 `validate_incremental` 注入实时约束反馈；涉及交通/住宿锁定的写入还会触发 `validate_lock_budget` 检查预算占比
3. **软评分 Judge** — pace / geography / coherence / personalization 四维评分，在 `assemble_day_plan`、`generate_summary` 后触发；`generate_summary` 由 Phase 7 以 `travel_plan_markdown` 和 `checklist_markdown` 提交最终交付物
4. **可行性门控 Feasibility Gate** — Phase 1→3 转换时基于目的地查表做规则式判断
5. **成本与延迟追踪** — `SessionStats` 记录 token 用量与模型定价；`ToolCallRecord` 承载 `state_changes`、`parallel_group`、`validation_errors`、`judge_scores`、`suggestion`；`MemoryHitRecord` 记录命中记忆；Trace 视图输出 `error_code` 和 `suggestion` 字段用于错误诊断

**最近迁移（Task 14-15）**：
- `agent/loop.py`: 4 个 state repair 消息更新为指引 split 工具（`set_trip_brief` / `set_candidate_pool` / `select_skeleton` / `append_day_plan`）；candidate 阶段 repair hint 现在也对部分写入失败（`candidate_pool` 存在但 `shortlist` 缺失）触发
- `agent/tool_choice.py`: 简化为恒返 "auto"，依赖 prompt 纪律，让模型在 17 个状态写工具中自行选择
- `agent/reflection.py`: Phase 5 自检末尾更新为 `replace_all_day_plans(days=[完整天数列表])` 用于跨多天全局修正、`save_day_plan(mode="create", day=缺失天数, date=对应日期, activities=活动列表)` 用于填充缺失天数、`save_day_plan(mode="replace_existing", day=目标天数, date=对应日期, activities=活动列表)` 用于修正已有单日，`request_backtrack` 保留用于上游重决策
- `context/manager.py`: 系统提示泛化"状态写入工具"措辞；backtrack 规则更新为 `request_backtrack(to_phase=..., reason="...")`；压缩渲染新增对 `PLAN_WRITER_TOOL_NAMES` 的"决策"行标记
- `harness/guardrail.py`: `invalid_budget` 规则现在统一处理 `update_trip_basics` 中 dict/string/number 格式的预算，拒绝所有负数或零值（包括 "-500" / "-1万" / -1000 / 0）；`_extract_numeric_budget` 对 dict 格式递归处理 total，支持 `{"total": "-500"}` / `{"total": "10000"}` 等字符串 total 路径

---

## 13. 可观测性

Jaeger (OTLP) 采集以下 span：

```
agent.loop           完整循环
tool.execute         每次工具调用
llm.chat             LLM 请求/响应
phase.transition     阶段变化 + 快照
context.compression  压缩决策
```

---

## 14. 开发命令

```bash
# 全栈
npm run dev:all                # 并行启动后端(:8000) + 前端(:5173)
npm run dev:stop

# 后端
cd backend && source .venv/bin/activate
uvicorn main:app --reload --port 8000
pytest
pytest --cov

# 前端
cd frontend && npm run dev
cd frontend && npm run build

# E2E
npx playwright test e2e-test.spec.ts

# 可观测性
docker compose -f docker-compose.observability.yml up -d
# Jaeger UI: http://localhost:16686
```

---

## 15. 配置体系

```
backend/.env   敏感凭证（python-dotenv 加载）
config.yaml    运行时配置（LLM 覆盖 / 阈值 / 功能开关 / phase5.parallel 并行模式），支持 ${ENV_VAR} 引用
优先级：环境变量 > YAML > 代码默认值
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
| 确定性阶段交接 note | 用结构化职责交接替代历史摘要，降低跨阶段语义污染 |
| 回退快照 | 每次阶段转换存档，支持历史回溯 |
| Hook 系统 | 软评分/验证/压缩与核心循环解耦 |
| Evaluator-Optimizer | 硬约束错误阻断；软评分低于阈值注入修改建议；评分器异常不阻断主流程 |
| Reflection 自省 | 被动 system message 注入，零额外 LLM 调用，会话级幂等 |
| 并行工具执行 | 读写分离：搜索类并行，状态更新顺序 |
| Tool Choice (always auto) | split 工具后移除强制逻辑，全依赖 prompt 纪律与 State Repair |
| Memory System | v3 结构化长期画像（profile）+ 会话 working memory + episode slice；当前旅行事实由 TravelPlanState 权威提供；query-aware symbolic recall 只在显式历史/偏好查询时触发；遗留 v2 memory/item/episode API 仅保留兼容；memory extraction 现已分拆为 profile / working memory 专用工具，仍由 gate 决定路由 |
| Tool Guardrails | 确定性规则校验，不依赖 LLM，可按规则名禁用 |
| Trace Data Pipeline | "丰富 Stats 层，Trace 只做读取"：钩子 post-hoc 写入 `ToolCallRecord`；`build_trace` 纯读取消费 |
| Memory Recall SSE | `memory_recall` 事件透传到前端，payload 含 `sources/profile_ids/working_memory_ids/slice_ids/matched_reasons`，驱动 SessionSidebar 高亮与 ChatPanel memory chip |
| LLM 韧性三层架构 | 错误归一化 → 停止生成（cancel_event + 取消检查点 + RunRecord）→ 安全继续（continuation_context + continue endpoint） |
| 工具白名单前瞻容错 | 每个 Phase 3 子阶段向前开放下一阶段写入工具，防止 LLM 跳阶时工具不可用导致状态丢失和死循环 |
| 四段式工具描述 | 功能说明 / 触发条件 / 禁止行为 / 写入后效果结构化模板，引导 LLM 正确选择工具而非混用 |
| 子阶段输出协议 | 每个 Phase 3 子步骤开头注入"先工具后文字"正面指令，防止 LLM 只在正文描述方案而不写入状态 |
| 重复搜索拦截 | 同 query 滑动窗口去重，阻断搜索死循环 |
| 小红书三层工具模型 | `xiaohongshu_search_notes`（导航）→ `xiaohongshu_read_note`（信息）→ `xiaohongshu_get_comments`（评价），用单一职责工具替代 `operation` 参数，降低模型漏传 discriminator 的概率 |
| `DateRange.total_days` inclusive 语义 | 覆盖 start 到 end 的自然日数量（+1），与骨架生成器语义一致 |
| Phase 5 并行 Orchestrator-Workers | 纯 Python 调度器 + N 个轻量 LLM Day Worker：上下文隔离解决 token 膨胀（N 天共享前缀），并发执行解决串行延迟（asyncio.gather + Semaphore），全局验证解决跨天一致性；失败率 >50% 自动降级到串行；Worker 实时通过 `on_progress(day, kind, payload)` 同步回调向 Orchestrator 报告 `iter_start` / `tool_start`，经 `asyncio.Queue` 唤醒主收集循环，合并进 `worker_statuses[idx]` 并广播 `parallel_progress` SSE（workers[] 含 theme/iteration/current_tool/activity_count/error 字段） |
| Phase 3→5 骨架天数门控 | 已选骨架天数必须与 total_days 一致才允许进入 Phase 5 |
| trip_brief 权威字段强制覆盖 | dates/total_days 在 hydrate 时直接赋值，防止 stale |
| plan_writer 增量持久化 | 每次 plan_writer 工具成功后立即 `state_mgr.save(plan)` 并同步更新 session meta（phase/title），finally 保底保存 plan 与 messages（含 logger.warning 日志），并把仍处于 running 的 run 标记为 cancelled，防止 SSE 中断丢失状态、消息或三源不一致 |
| Phase 5 当前风险与并行化草案 | 已记录一次“只承诺不动手”后静默 DONE 的复盘；并补充了 Orchestrator-Workers 并行化方案文档，作为后续治理 token 膨胀与串行延迟的设计输入 |

---

## 17. 测试体系

- **后端单元测试**：覆盖 Agent 循环（含白名单前瞻容错、跨阶段状态修复、重复搜索拦截）、LLM 供应商（含错误归一化+重试）、状态管理、阶段路由、工具执行、存储（含 run 追踪）、压缩、验证、遥测、护栏、可行性、评估管线、Trace 通道、RunRecord/IterationProgress；plan tools 回归集中位于 `backend/tests/test_plan_tools/`（含 `test_phase3_tools.py`、`test_trip_basics.py` 与 `test_daily_plans.py`），`infer_phase3_step_from_state` 对污染的 `skeleton_plans` 具备 reader-side 过滤防御
- **测试基线语义**：未知 Anthropic 异常遵循共享 `classify_opaque_api_error` 逻辑，默认归类为 `LLM_TRANSIENT_ERROR`；`validate_lock_budget` 的占比提示基于 `DateRange.total_days` 的 inclusive 天数语义；并行 `add_constraints` 用 `items` 作为增量状态写入参数，约束提示通过 `_pending_system_notes` 在下一轮 LLM 调用前 flush
- **Memory 集成测试策略**：`backend/tests/test_memory_integration.py` 以公开 chat 流程验证 Phase 7 episode 归档幂等、chat 与 memory extraction 解耦、forced tool call 语义、timeout/warning 语义，以及 session 级 each-turn memory extraction 排队行为；需要观测后台调度器时通过 `app.state` 暴露的测试钩子读取，而不是依赖路由闭包捕获细节
- **记忆/遥测测试整理**：遗留的 `test_memory.py` 与 `test_telemetry_integration.py` 已并入 `test_memory_manager.py`、`test_telemetry_setup.py`；记忆文档同步补充了 `memory_recall` / `memory_hits` 的可观测性现状与 `TripEpisode` 仍未进入主召回链路的限制
- **Phase 7 交付物契约草案**：仓库内已新增 dual-deliverables 设计/计划文档，以及 `backend/tests/test_state_models.py`、`backend/tests/test_state_manager.py` 中针对 `plan.deliverables` 与 deliverable 文件读写/清理的待实现测试；当前主干实现尚未支持这些能力
- **评估管线**：golden cases（YAML）+ 断言评估 + 离线 runner；断言类型包含 `phase_reached`/`state_field_set`/`tool_called`/`tool_not_called`/`contains_text`/`not_contains_text`/`budget_within`（其中 `not_contains_text` 用于回归"机器感 checklist"类文案违规）；`scripts/eval-stability.py` 生成 pass@k 稳定性报告（JSON + Markdown）；`scripts/failure-analysis/` 对 live backend 执行失败场景并产出分析报告
- **E2E 测试**：Playwright 三套专项配置——主流程（含 deterministic mock 的阶段切换）、重试体验（继续/重发/停止/不可恢复错误）、等待体验（ThinkingBubble 与工具耗时提示）；demo spec 基于 `demo-scripted-session.json` 稳定回放 Phase 1 → Phase 3 → Phase 5 → backtrack；Prompt 行为回归集中于 `e2e-phase1-no-offtopic.spec.ts`（验证 Phase 1 不主动追问非目的地字段）
- **运行**：`cd backend && pytest` / `npx playwright test`

---

*最后更新：见 `git log --oneline -1`*
