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
| Embedding Runtime | FastEmbed 0.8.x + ONNX Runtime CPU；Stage 3 语义召回默认模型为 `BAAI/bge-small-zh-v1.5`，本地 cache 位于 `backend/data/embedding_cache`，可用 `scripts/verify-stage3-embedding-runtime.py --local-files-only` 复验 |
| 外部服务 | Tavily、小红书 CLI、FlyAI CLI、Google Maps、Amadeus、OpenWeather |

---

## 3. 目录结构总览

```
travel_agent_pro/
├── backend/                    # Python 后端
│   ├── main.py                 # FastAPI 应用装配入口，负责依赖实例化、lifespan、路由注册和兼容导出
│   ├── run.py                  # RunRecord / IterationProgress（LLM 韧性追踪）
│   ├── config.py               # 配置加载（.env + config.yaml）
│   ├── agent/                  # AgentLoop facade + execution helpers + Phase 5 orchestrator-workers subsystem
│   ├── llm/                    # LLM 抽象：base Protocol / errors / factory / openai_provider / anthropic_provider
│   ├── state/                  # 旅行状态模型：models / manager / intake / plan_writers
│   ├── memory/                 # v3 分层记忆：v3_models / v3_store / archival / episode_slices / manager / extraction / policy / formatter / symbolic_recall / recall_stage3*
│   ├── context/                # 上下文：manager（系统提示/压缩决策）+ soul.md（人格）
│   ├── phase/                  # 阶段路由：router / prompts（skill-card 架构，GLOBAL_RED_FLAGS + PHASE{1,3,5,7}_PROMPT + build_phase3_prompt）/ backtrack
│   ├── tools/                  # 领域工具：base / engine / plan_tools(聚合导出 + Phase 1/3 trip_basics + append_tools + Phase 3 强 schema + Phase 5 daily_plans + 回退工具) / 搜索类 / 规划类 / normalizers
│   ├── storage/                # SQLite 层：database / session_store / message_store / archive_store
│   ├── harness/                # 5 层守护：guardrail / validator / judge / feasibility
│   ├── evals/                  # 评估管线：models / runner / stability / failure_report / golden_cases
│   ├── telemetry/              # 可观测 + 成本：setup / attributes / decorators / stats
│   ├── api/                    # API 层：schemas / trace / routes / orchestration；routes 按 HTTP 资源分组，orchestration 按 agent / chat / memory / session / common 子包承载请求编排
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
  - **并行 Orchestrator-Workers 模式**：Python Orchestrator（纯代码调度器，非 LLM）将骨架拆分为 N 个 DayTask，经 `_compile_day_tasks` 注入跨天约束（forbidden_pois / mobility_envelope / date_role），并行派发 N 个 Day Worker（轻量 LLM Agent，独立上下文），Worker 完成后优先通过 worker-only `submit_day_plan_candidate` 工具把候选 DayPlan 写入 run-scoped JSON artifact（`phase5.parallel.artifact_root/{session_id}/{run_id}/day_N_attempt_M.json`），Orchestrator 收集结果后读取 artifact 候选并做全局验证（POI 去重 / 预算检查 / 天数覆盖 / 时间冲突 / 语义去重 / 交通衔接 / 节奏匹配），error 级问题触发最多 1 轮 re-dispatch（注入 repair_hints 重跑受影响天）。验证后的 final dayplans 不由 Orchestrator 直接写入 `TravelPlanState`；而是作为内部 handoff 交还给 AgentLoop，由 AgentLoop 构造内部 `replace_all_day_plans` 工具调用并走标准 `_execute_tool_batch -> detect_phase_transition` 链路，从而复用 Phase 5 → Phase 7 的现有阶段推进、hook、telemetry 和工具结果事件
  - 并行模式通过 `config.yaml` 的 `phase5.parallel` 段控制（`enabled` / `max_workers` / `worker_timeout_seconds` / `fallback_to_serial`）
  - Worker 共享相同 system prompt prefix → KV-Cache 命中率 ~93.75%（Manus pattern）
  - Worker 只有只读工具和候选提交工具；候选提交只写 staging artifact，不改 `TravelPlanState.daily_plans`，正式写入由 AgentLoop 经 `replace_all_day_plans` 标准工具路径完成
  - 失败率 >50% 自动降级到串行模式
- **Day Worker 提示词止血策略**：Day Worker 具备有限补救 + 保守落地的收敛策略——优先通过 `submit_day_plan_candidate` 结构化提交候选 DayPlan；若模型仍以最终文本输出 JSON，保留 `extract_dayplan_json` 兼容路径并尝试有限次数 JSON 修复，补救失败则保守落地（返回当前已有结果而非无限重试）
- **Day Worker loop 保护机制**：Worker 内部循环具备四重收敛保障——**重复查询抑制**（同 query 滑动窗口去重，避免搜索死循环）、**补救链阈值**（连续补救轮次上限，超限即保守落地）、**后半程强制收口**（迭代过半后强制聚焦已有结果，不再启动新搜索）、**JSON 修复回合**（输出 JSON 解析失败时限定修复轮次，避免在格式修复上无限循环）
- **Worker 失败错误类别**：Worker 失败时输出结构化错误码，便于 Orchestrator 诊断与降级决策——`REPEATED_QUERY_LOOP`（重复查询死循环被抑制）、`RECOVERY_CHAIN_EXHAUSTED`（补救链耗尽仍无法恢复）、`JSON_EMIT_FAILED`（JSON 输出经修复回合仍无法解析）、`TIMEOUT`（Worker 超时）、`LLM_ERROR`（LLM 调用不可恢复错误）、`NEEDS_PHASE3_REPLAN`（locked_pois 全部不可行，需回退 Phase 3 重调骨架）
- **Worker 约束注入**：DayTask 携带 `locked_pois`/`candidate_pois`/`forbidden_pois`/`area_cluster`/`mobility_envelope`/`date_role`/`repair_hints`/`day_budget`/`day_constraints`/`arrival_time`/`departure_time` 等约束字段，由 `_build_constraint_block` 渲染为中文 prompt 硬约束块注入 Worker 上下文
- **shared_prefix 精简与稳定排序**：`build_shared_prefix` 对 trip_brief 做白名单过滤（保留 goal/pace/departure_city/style/must_do/avoid），对 preferences 按 key 字典序排序，只在 prefix 放全局硬约束（soft 约束通过 day_constraints 路径注入 suffix），保证 KV-Cache 命中稳定性
- **DayTask 扩展（4 个新字段）**：`day_budget`（软性日预算提示）、`day_constraints`（天级别非硬约束列表）、`arrival_time`（到达时间 HH:MM）、`departure_time`（出发时间 HH:MM），全部有默认值向后兼容
- **orchestrator `_compile_day_tasks` 扩展**：步骤 5/5b/6 注入 day_budget（总预算/天数取整）、day_constraints（过滤 non-hard）、arrival_time/departure_time（从 selected_transport 提取 + arrival_departure_day 单天支持）
- **`_build_constraint_block` 增强**：arrival/departure/arrival_departure_day 三种 date_role 都有具体时间锚点描述（+ 无时间时的兜底缓冲文案），emoji 图标区分到达/离开/混合日
- **收口 prompt 重写（Task 4）**：`_FORCED_EMIT_PROMPT` 改为事实性描述 + 安全降级（禁止 0,0 假坐标，缺字段用 notes 标注）；`_LATE_EMIT_PROMPT` 改为软提醒（最多 2 个调用后必须提交）；`_JSON_REPAIR_PROMPT` 引导复用对话历史调用 submit 工具而非直接输出 JSON；初始 user message 注入迭代预算声明（MAX_SAME_QUERY/MAX_POI_RECOVERY/max_iterations）
- **Task 3（分级约束）**：locked_pois/candidate_pois/forbidden_pois 用 ⛔/✅/🚫 图标区分层级 + 违反后果声明（locked 违反 = DayPlan 无效；forbidden 违反 = 跨天 POI 重复触发重新分配），repair_hints 加粗聚焦 + "本轮必须逐一解决"指令
- **Task 0（submit schema）**：（activities items 含 location/start_time/end_time/category/cost 的类型约束 + category enum + pattern + additionalProperties: False）+ 5 段式 description（何时调用 / 何时不要 / 提交后语义 / 错误码动作映射），确保 LLM 输出结构合规
- **Task 5（集成测试修复）**：`test_parallel_phase5_integration.py` 的 `MockLLM.chat()` 从 `messages[0]`（system）改为 `messages[1]`（user）读取"第 N 天"——因为 Task 0 已将 `day_suffix`（含"第 N 天"）从 system message 移至 user message，保持 KV-Cache 友好的 system-only shared_prefix；全套 1632 测试通过
- **_DAYPLAN_SCHEMA**：已补充 category enum（shrine/museum/food/transport/activity/shopping/park/viewpoint/experience）+ 常见结构错误示例（location 字符串 / cost 字符串 / end_time ≤ start_time / category 非枚举），以 submit schema 为单一事实源
- **Day Worker 身份（soul.md 已移除）**：Worker 不再注入 soul.md（已删除 `_SOUL_PATH`/`_load_soul`），改为 `_WORKER_ROLE` 模块常量直接内联 Worker 专属身份，包含并发语境、无用户交互声明、完成优于完美、优先级层次、交付唯一路径。这消除了 soul.md 中"一次只问一个问题/提供选项"等对 Worker 不适用的行为指引。

- **Prompt 已迁移**：Phase 5 使用 `optimize_day_route`（路线辅助，不写状态）、`save_day_plan` / `replace_all_day_plans`（状态写入）与 `request_backtrack`（回退）
- 流程：expand（骨架→日期）→ assemble（活动+时间）→ validate（开放/距离/天气/预算）→ commit
- 产出：`daily_plans[]`，每天含完整 Activity 列表
- Phase 5+ 上下文中 trip_brief 注入时排除 `dates`/`total_days`（已由 `plan.dates` 权威提供），避免重复信号
- 运行时上下文必须注入骨架内容、`trip_brief` 字段、偏好和约束
- **并行模式文件**：`agent/phase5/orchestrator.py`（调度器核心）、`agent/phase5/day_worker.py`（单日 Worker 执行引擎）、`agent/phase5/worker_prompt.py`（共享前缀 + 日别后缀模板）、`agent/phase5/candidate_store.py`（run-scoped 候选 DayPlan artifact 写入与读取）；Phase 5 不再在 `agent/` 根目录保留同名兼容文件

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
| Memory System | v3 只保留四类权威记忆：`profile.json`、trip-scoped `working_memory.json`、`episodes.jsonl`、`episode_slices.jsonl`；当前旅行事实始终由 `TravelPlanState` 权威提供，working memory 只服务当前 session/trip，不参与 historical recall。同步 recall 采用 `Stage 0` 硬规则短路 + `Stage 1` recall gate + `Stage 2` retrieval plan + `Stage 3` candidate generation；Stage 3 会返回 `RecallCandidate[]` 与 `evidence_by_id` sidecar。Stage 4 reranker 默认在规则主干之上叠加 lane-fused / semantic / lexical evidence 权重，消费 Stage 3 透传的 `evidence_by_id`；trace 与 stats 会记录 `reranker_selected_ids`、`reranker_per_item_scores`、`reranker_intent_label` 和 `reranker_selection_metrics`。reranker 配置已预留 code-only `intent_weights`、默认关闭的 `dynamic_budget` block，以及默认激活的 `evidence` block（`lane_fused_weight=0.25` / `semantic_score_weight=0.15` / `lexical_score_weight=0.08`，`*_hit_weight` 与 `destination_match_type_weight` 保持 0）；Stage 4 当前已显式解析 intent profile（支持缺省 default 的局部覆盖安全回退）并把 bucket/domain/keyword/destination/recency/applicability/conflict rule signals 与 evidence lane/score signals 写入结构化 `per_item_scores`；最终 source-aware 流程先计算 `rule_score`，再叠加 `evidence_score` 得到 `source_score`，按 profile / slice 分源做 min-max `source_normalized_score` 并加 source prior 生成 `final_score` 后选择候选，所有通过 hard-filter 和 dedupe 并进入归一化的候选，其 `per_item_scores` 会同步暴露归一化后的 `source_normalized_score` / `final_score`；生产端可在 `config.yaml` 把三个 score 权重写回 0 并关闭 `stage3.semantic.enabled` 回到第零期行为；evidence 分数归一化会把单个非正值或全为 0 的已知分数保持为 0，避免 symbolic-only/unfused evidence 被误抬高；`RecallRerankResult.selection_metrics` 在空结果、小候选集和正常 source-aware 路径均带 pairwise similarity placeholder。默认生产行为启用 symbolic + semantic lane（`BAAI/bge-small-zh-v1.5` + FastEmbed + ONNX CPU + 本地 cache，`local_files_only=True`），lexical lane 仍在 feature flag 后面；symbolic lane 继续沿用 `symbolic_recall.py` 的检索顺序与候选语义。Stage 1 LLM gate 现已收紧为只判断 `latest_user_message` 是否语义上需要召回，`previous_user_messages` 仅用于省略/指代/承接消歧，`current_trip_facts` 仅用于识别当前行程事实问题，且不再接收或构建 `memory_summary`，避免库存信号污染是否召回的判断；Gate Window 从最近消息向前回填，最新用户消息完整保留，早期消息整条加入或丢弃。Stage 2 现已收紧为 source-aware query contract：`profile` / `hybrid_history` source 必填 `buckets`，`episode_slice` source 不暴露 `buckets`，`domains` 只允许系统枚举，`destination` 取代开放式 `entities`；Retrieval Plan prompt 同样以 `latest_user_message` 为主，`previous_user_messages` 仅用于承接消歧，`plan_facts` 只用于抽取目的地/当前对象/预算/同行人等检索参数，不重新判断是否 recall；Stage 2 会在 gate 放行后自行构建 `memory_summary` 以规划如何查。query timeout / error 会回退到 stage0-aware heuristic retrieval plan。Stage 3 还挂接了 feature-flagged 的 destination normalization、lexical expansion 与 semantic embedding lane；默认 semantic runtime 为 FastEmbed + `BAAI/bge-small-zh-v1.5`，走 ONNX Runtime CPU，并复用 `backend/data/embedding_cache` 本地 cache，单元测试通过 fake/null provider 避免模型下载。source widening 配置已预留，但首个合并版本仍保持默认禁用，不视为生产行为。长期 profile 不再固定常驻 prompt，而是和 episode slice 一样只在命中时以 recall candidate 注入上下文。profile extraction 会先规范化高价值 domain/key，再写成 recall-ready 的 `MemoryProfileItem`；route-aware gate 只按需触发 `extract_profile_memory` 与 `extract_working_memory` 两条 v3 extractor，后台提取采用 session 级 latest-wins coalescing queue。Phase 7 结束后直接归档 `ArchivedTripEpisode`，并派生新的 slice taxonomy：`itinerary_pattern`、`stay_choice`、`transport_choice`、`budget_signal`、`rejected_option`、`pitfall`。 | system prompt 构建前检索；每轮 chat 追加 user message 后立即后台排队 gate/job |
| Tool Guardrails | 输入/输出护栏，可按规则名禁用 | 工具执行前后 |
| Eval Runner | YAML golden cases + 可注入执行器；支持 pass@k 稳定性评估；测试中的 golden case 路径按文件位置解析，避免 cwd 依赖；memory recall 专项 case 通过 `memory_recall_field` 断言和 `memory_recall` 聚合指标跟踪 false skip / false recall / hit rate / zero-hit rate；另有 deterministic reranker-only eval 固定 Stage 0/1/2 输出与候选集，只验证 Stage 4 reranker 的 selected ids、fallback、final reason 与 per-item reason | 离线/批量评估 |

#### 召回门控三层结构

召回判定分三层：

1. **Layer 1 信号抽取** (`backend/memory/recall_signals.py`)：对用户消息进行 6 类词表匹配（history / style / recommend / fact_scope / fact_field / ack_sys），纯字符串匹配、无决策；style/recommend 词表覆盖"按我偏好/按我习惯/照我的习惯"与"怎么安排/比较好"等自然话术。
2. **Layer 2 规则引擎** (`backend/memory/recall_gate.py::apply_recall_short_circuit`)：按显式优先级 P1–P6 输出三值决策。P1 profile signal → force_recall；若 P1 信号前存在"不要/别/不是/不用"等否定排除语境，则输出 P1N → undecided 交给 LLM；P2 recommend → undecided；P3 纯事实问句 → skip_recall；P4 仅 ACK → skip；P5 空消息 → undecided；P6 兜底 → undecided。
3. **Layer 3 LLM gate** (`decide_memory_recall` tool)：仅处理 Layer 2 放行的 undecided 样本，输出 `intent_type` 精细分类；`mixed_or_ambiguous` 采用保守召回策略，即使模型返回 `needs_recall=false` 也归一化为 recall，防止模糊样本漏召。

当 LLM gate 或 Recall Query Tool 超时/异常/无效输出时，系统会按 Stage 0 signals 生成启发式 `RecallRetrievalPlan`；recommend 信号会保守转为 profile recall fallback，避免"住宿怎么安排比较好"这类个性化推荐在故障路径中被直接跳过。Stage 1 / Stage 2 都显式区分 `latest_user_message` 与 `previous_user_messages`：latest 是主判断/主检索对象，previous 只用于“换个吧”“再轻松点”等承接语的省略和指代消歧。

决策对象携带 `matched_rule` 与 `signals`，便于 trace 与回归。

---

## 5. 核心数据流

```
用户消息 (POST /api/chat/{id})
    ↓
[api.routes.chat_routes + api.orchestration.*] 加载会话+方案，追加 user message
    ├─ 同步：`api.orchestration.memory.turn` 编排 `memory_recall` → system prompt
    └─ 异步：提交 session 级 memory job snapshot，由 `api.orchestration.memory.orchestration` 调度提取任务
    ↓
[AgentLoop.run()] 进入迭代循环
    │
    ├─ Phase 5 并行分流检查（agent/phase5/parallel.py::should_enter_parallel_phase5_now / should_enter_parallel_phase5_at_iteration_boundary）
    │   └─ 条件满足 → agent/phase5/orchestrator.py::Phase5Orchestrator.run() → split → spawn workers → collect → validate → write → return
    │
    ├─ 取消检查点（迭代开始 / LLM 流式 chunk 前 / 工具执行前）
    │
    ├─ [agent/execution/llm_turn.py] 单轮 LLM 调用与流式 chunk 解析
    │
    ├─ [Hook: before_llm_call]
    │   ├─ ContextManager.build_system_message() → soul + 阶段提示 + 状态快照
    │   ├─ ReflectionInjector → 关键阶段自省提示
    │   └─ compact_messages_for_prompt() → token 预算内渐进压缩
    │
    ├─ [ToolChoiceDecider] → 当前始终返回 "auto"（split 工具后移除强制）
    ├─ [LLMProvider.chat()] → 流式输出 text_delta + tool_calls
    │   └─ AgentLoop.progress 实时更新，用于异常续写 continuation 判定
    │
    ├─ [ToolGuardrail + ToolEngine.execute/execute_batch] → 顺序/并行调度
    │
    ├─ [agent/execution/phase_transition.py + PhaseRouter] → 统一阶段变化检测 + 转换前质量门控
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
- background internal-task SSE `/api/internal-tasks/{id}/stream`：承载与回答解耦的后台任务，例如 `memory_extraction_gate`、聚合态 `memory_extraction`、按路由发布的 `profile_memory_extraction` / `working_memory_extraction`

前端 `ChatPanel` 按 `task.id` 合并生命周期更新，并维护跨流共享的 `task.id -> message.id` 映射；这样同一个后台任务即使在 chat `done` 之后才结束，也会回写到原卡片，而不是再长出一张重复卡。`MessageBubble` 渲染为系统任务卡，和真实工具卡保持视觉与语义区隔。

进入聊天流的内部任务包括：`soft_judge`（工具结果后的行程质量评审）、`quality_gate`（阶段推进检查）、`context_compaction`（上下文整理）、`reflection`（自检提示注入）、`phase5_orchestration`（Phase 5 并行编排）和 `memory_recall`（本轮记忆召回）。进入后台 internal-task 流的任务包括：`memory_extraction_gate`（轻量判断是否值得提取）、`memory_extraction`（聚合态提取结果）以及按 gate 路由出现的 `profile_memory_extraction` / `working_memory_extraction`。任一路由提取失败会让聚合任务以 `warning` / `partial_failure` 结束，已成功写入的另一类记忆不回滚，且本轮 `last_consumed_user_count` 不前进以便后续重试。`save_day_plan` / `replace_all_day_plans` 等真实工具的 `TOOL_RESULT` 会先到达前端并结束工具卡，随后才显示软评审或后台记忆任务，避免用户误以为真实工具仍在执行。

### 文档沉淀约定
- `docs/phase*.md`、`docs/*fix*.md`：专题修复记录与设计说明
- `docs/postmortems/`：事故复盘，记录用户可见故障、根因链路、放大因素与后续动作
- `docs/postmortems/2026-04-19-phase5-parallel-guard-refactor.md`：记录 Phase 5 并行入口守卫重构的主路径等价性、`max_retries` 边界风险与外部 agent runtime 设计参照
- `docs/learning/interview-stress-test/`、`docs/mind/`：学习型架构评审与阶段性洞察，当前包含记忆系统写入语境、稳定性、TripEpisode 职责边界与 working memory 取舍分析
- `docs/learning/2026-04-19-Phase*.md` 与 `docs/learning/assets/phase5-parallel-orchestration/`：面向初学者的 Phase 转换机制、Phase 5 并行 Orchestrator-Workers 生命周期说明和配图
- `docs/superpowers/specs/`、`docs/superpowers/plans/`：规格与实施计划，包含 v3-only memory cutover、Memory Extraction Routing 设计，以及 Phase 3 `candidate_pois` 全局唯一性设计与实施计划（把重复 POI 拦截在 `set_skeleton_plans` 写入边界，而不是留给 Phase 5 并行 worker 事后去重）
- `docs/superpowers/specs/2026-04-23-reranker-stage4-upgrade-v2-design.md`、`docs/superpowers/specs/2026-04-23-stage3-recall-upgrade-v2-design.md`、`docs/superpowers/plans/2026-04-23-stage3-hybrid-recall-upgrade-v2.md`：记录记忆召回 Stage 4 reranker 语义增强边界、Stage 3 hybrid recall 候选生成设计，以及默认 embedding/runtime 决策（`BAAI/bge-small-zh-v1.5` + FastEmbed/ONNX Runtime CPU）；当前 plan 已把 `987b104` 的 runtime spike 作为前置条件，避免执行时重复安装依赖或下载模型。
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
[api.orchestration.chat.stream] → SSE error 事件（error_code / retryable / can_continue / user_message）
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
- **API 编排写后处理**：`backend/api/orchestration/agent/builder.py` 只装配 `AgentLoop`，`agent/tools.py` 负责工具注册，`agent/hooks.py` 注册实时校验、soft judge 和 quality gate hooks；`backend/api/orchestration/chat/stream.py` 负责 SSE 主循环，`chat/events.py` 负责事件序列化，`chat/finalization.py` 负责 run 收尾和保底持久化；`memory/orchestration.py` 组装记忆召回、提取、任务流和 episode 归档，`memory/contracts.py` 承载共享 dataclass 契约，`memory/recall_planning.py` 承载 recall gate fallback 与 retrieval-plan tool/prompt，`memory/extraction.py` / `memory/tasks.py` / `memory/episodes.py` 分别承载记忆提取、后台任务流和归档 episode 派生；`backend/api/routes/internal_task_routes.py` 承载 `/api/internal-tasks/...`，不再混入 `memory_routes.py`；`backend/api/orchestration/session/backtrack.py` 承载关键词回退与 reset trip 判定，`session/persistence.py` 承载消息/会话恢复落盘，`session/deliverables.py` 承载 Phase 7 交付物冻结，`common/telemetry_helpers.py` 与 `common/llm_errors.py` 承载 API 编排通用辅助逻辑。若工具结果显式返回 `previous_value` / `new_value`，`SessionStats.state_changes` 优先记录工具返回的规范化值；`request_backtrack` 只保留 rebuild/transition 语义，不伪造字段 diff
- **Phase 3 工具门控**：brief → candidate → skeleton → lock 逐级放开工具子集，并把 split plan tools 纳入白名单（如 brief 的 `set_trip_brief` / `add_preferences` / `add_constraints`，lock 的交通住宿锁定工具）；每个子阶段向前开放下一阶段写入工具实现"前瞻容错"（brief 可写 `set_candidate_pool`/`set_shortlist`，candidate 可写 `set_skeleton_plans`/`select_skeleton`）；`phase3_step` 由路由推断
- **Phase 3 工具描述**：所有 11 个 Phase 3 工具的 `description` 采用四段式结构——功能说明 / 触发条件 / 禁止行为 / 写入后效果，引导 LLM 正确选择工具
- **Phase 3 状态修复**：`agent/execution/repair_hints.py` 覆盖全部 4 个子阶段的状态修复——brief 检测画像描述未写入、candidate 检测候选池/短名单缺失及跳阶信号、skeleton 检测骨架信号词但状态为空、lock 按类别（交通/住宿/风险/备选）独立检测；`AgentLoop` 统一记录已消费 hint key，每个子阶段允许两次修复尝试（首次 + retry）
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
| `memory_recall` | 本轮结构化记忆召回结果；payload 除 `sources`、`profile_ids`、`working_memory_ids`、`slice_ids`、`matched_reasons` 外，还包含 `gate`、`stage0_decision`、`stage0_reason`、`stage0_matched_rule`、`stage0_signals`、`gate_needs_recall`、`gate_intent_type`、`gate_confidence`、`gate_reason`、`final_recall_decision`、`fallback_used`、`recall_skip_source`、`query_plan`、`query_plan_source`、`query_plan_fallback`、`recall_attempted_but_zero_hit`，以及 reranker 相关的 `candidate_count`、`reranker_selected_ids`、`reranker_final_reason`、`reranker_fallback`、`reranker_per_item_reason`、`reranker_per_item_scores`、`reranker_intent_label`、`reranker_selection_metrics`；其中 `sources` 现只包含 `query_profile`、`working_memory`、`episode_slice` 三类真实来源 |
| `error` | LLM 错误（含 retryable / can_continue） |
| `keepalive` | 心跳 |
| `done` | 流结束（含 run 状态 + can_continue） |

### 关键组件

- **ChatPanel** — SSE 流消费，工具卡渲染，staleness 检测，memory chip、轮次摘要、停止/继续/重发交互
- **TraceViewer** — 分阶段分组的 Trace 视图，按 significance 分级展示，连续 thinking 自动折叠
- **Phase3Workbench** — 旅行画像/候选池/骨架/锁定/风险 五卡片
- **ThinkingBubble** — stage-aware 等待气泡，展示 narration hint
- **MemoryCenter** — 右滑抽屉；只展示/管理 v3 `profile`、当前 session/trip 的 `working-memory`、`episodes` 与 `episode-slices`；卡片按 header(domain/confidence/time) + body(content/source/applicability) + actions 分层，不再混合本轮召回追踪
- **MemoryTracePanel** — 右侧面板第三个 Tab（Plan / Trace / Memory），纯只读地展示本轮 `memory_recall` 结构化命中（profile / working / slice 分组、reranker 分数）与本轮记忆提取工具调用的输入输出；数据源复用 `useTrace` 的 `iterations[].memory_recall` 与 `tool_calls`，不调用新后端接口
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

Phase 7 会把完成会话归档为 v3 `ArchivedTripEpisode`，并同步派生 `EpisodeSlice` 写入 `users/{user_id}/memory/episode_slices.jsonl`；slice 写入按 id 幂等，重复归档不会产生重复切片。

### 文件系统
```
backend/data/
├── sessions.db
├── sessions/sess_*/          # plan.json + snapshots/ + tool_results/ + deliverables/
└── users/{user_id}/
   └── memory/
      ├── profile.json
      ├── events.jsonl
      ├── episodes.jsonl
      ├── episode_slices.jsonl
      └── sessions/{session_id}/trips/{trip_id}/working_memory.json
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
GET    /api/memory/{user_id}/profile        v3 长期画像
GET    /api/memory/{user_id}/episode-slices v3 历史切片
GET    /api/memory/{user_id}/sessions/{session_id}/working-memory v3 会话工作记忆
POST   /api/memory/{user_id}/profile/{item_id}/confirm  确认长期画像项
POST   /api/memory/{user_id}/profile/{item_id}/reject   拒绝长期画像项
DELETE /api/memory/{user_id}/profile/{item_id}          删除长期画像项
GET    /api/memory/{user_id}/episodes       v3 历史旅行 episodes
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
| Memory System | v3 结构化长期画像（profile）+ 当前 session/trip 的 working memory + historical episodes / episode slices；当前旅行事实由 `TravelPlanState` 权威提供，working memory 不参与 historical recall。同步 recall 采用 `Stage 0` 硬规则短路 + `Stage 1` recall gate + `Stage 2` retrieval plan + `Stage 3` candidate generation；Stage 3 会返回 `RecallCandidate[]` 与 `evidence_by_id` sidecar。Stage 4 reranker 默认在规则主干之上叠加 lane-fused / semantic / lexical evidence 权重（默认 `lane_fused_weight=0.25` / `semantic_score_weight=0.15` / `lexical_score_weight=0.08`），消费 Stage 3 透传的 `evidence_by_id`；trace 与 stats 会记录 `reranker_selected_ids`、`reranker_per_item_scores`、`reranker_intent_label` 和 `reranker_selection_metrics`。Stage 1 / Stage 2 已统一 latest-first 信息控制：最新用户消息是主判断/主检索对象，前序用户消息只用于省略、指代和承接消歧；Stage 1 不再读取 `memory_summary`，Gate Window 完整保留最新用户消息并只整条回填更早消息；Stage 2 保持 source-aware schema 与 parser：`profile` / `hybrid_history` source 必填 `buckets`，`episode_slice` source 仅使用 `domains`、`destination`、`keywords`、`top_k`，`plan_facts` 只用于检索参数提取，`memory_summary` 只在 gate 放行后由 Stage 2 自行构建。长期 profile 不再固定常驻注入，只有 query recall 命中后才以 candidate 进入上下文；当前有效来源只保留 `query_profile`、`working_memory`、`episode_slice`。profile extraction 持久化前会先规范化高价值画像 domain/key，并把 `applicability`、`recall_hints`、`source_refs` 一起写成 recall-ready metadata；若新的偏好假设得到既有 profile 证据反复印证，会升级为 stable preference 后再写入长期画像；Stage 4 已从“阈值截断 top-N”改为 rule-based weighted reranker：按 retrieval source 应用不同配额，对 profile / slice 分池打分后归一化合并，并综合 bucket prior、domain/keyword overlap、destination、recency decay、applicability 与当前 user message 的局部 conflict penalty；当前 reranker config 已预留 code-only `intent_weights`、默认激活的 evidence 配置（`lane_fused_weight=0.25` / `semantic_score_weight=0.15` / `lexical_score_weight=0.08`，`*_hit_weight` 保持 0）与默认关闭的 dynamic budget 配置，Stage 4 已将 intent label/profile 解析与 rule-signal 计算拆成独立 helper，intent profile 解析支持局部覆盖并安全回退默认配置，并把 rule signals、evidence lane/score signals 与 hard-filter reason 写入 `RecallRerankResult.per_item_scores`；最终 scoring 明确为 `source_score = rule_score + evidence_score`，分 source min-max 归一化后加 profile/slice source prior 得到 `final_score` 再进入 source budget selection，且所有通过 hard-filter 和 dedupe 并进入归一化的候选都会在 `per_item_scores` 中使用归一化后的 score detail，同时所有返回路径统一填充 pairwise similarity selection metrics placeholder；Stage 3 产出的 `evidence_by_id` 已从 manager 透传到 Stage 4 reranker 入口，缺失 evidence key 会按空证据处理，fused/lexical/semantic evidence 分数会按候选池内有效值归一化后写入详情，单个非正值或全 0 已知分数保持为 0，并按默认激活的 evidence 权重进入 `source_score`（生产端可通过 `config.yaml` 把三个 score 权重写回 0 回到第零期 rule-only 行为）；同组 profile 候选与精确重复的 episode slice 会去重，冲突项和全弱相关候选会被丢弃，最终把选中结果、fallback telemetry 与 per-item reason 一起暴露给 SSE / trace / stats；`backend/evals/reranker.py` 通过固定 `RecallRetrievalPlan` 与 `RecallCandidate` 集合提供 reranker-only 上限评估，避免 Stage 0/1/2 live 抖动污染判断。query tool 超时或异常时不再注入空泛 default plan，而是直接产出 stage0-aware heuristic retrieval plan，并保留 `query_plan_fallback` 遥测；gate 超时、异常或 invalid payload 时会先用 heuristic 判定是否仍应召回，命中历史/画像线索则以 `gate_*_heuristic_recall` 继续走 manager heuristic fallback，否则以 `no_recall_applied` 跳过，并在 telemetry 标记 `recall_skip_source=gate_failure_no_heuristic`。 |
| Tool Guardrails | 确定性规则校验，不依赖 LLM，可按规则名禁用 |
| Trace Data Pipeline | "丰富 Stats 层，Trace 只做读取"：钩子 post-hoc 写入 `ToolCallRecord`；`build_trace` 纯读取消费 |
| Memory Recall SSE | `memory_recall` 事件透传到前端，payload 含 `gate`、`sources`、`profile_ids`、`working_memory_ids`、`slice_ids`、`matched_reasons`、`stage0_decision`、`stage0_reason`、`stage0_matched_rule`、`stage0_signals`、`gate_needs_recall`、`gate_intent_type`、`gate_confidence`、`gate_reason`、`final_recall_decision`、`fallback_used`、`recall_skip_source`、`query_plan`、`query_plan_source`、`query_plan_fallback`、`recall_attempted_but_zero_hit`，以及 `candidate_count`、`reranker_selected_ids`、`reranker_final_reason`、`reranker_fallback`、`reranker_per_item_reason`、`reranker_per_item_scores`、`reranker_intent_label`、`reranker_selection_metrics`；其中 `sources` 只保留 `query_profile`、`working_memory`、`episode_slice`，`profile_ids` 表示本轮最终命中的 profile recall item ids；真实召回命中仍只进入 `SessionStats.memory_hits`，而 recall gate / reranker 摘要单独进入 `SessionStats.recall_telemetry` / `/api/sessions/{id}/trace.iterations[].memory_recall`，这样零命中时仍保留可见性且不污染 `memory_hit_count` |
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
- **Memory 集成测试策略**：`backend/tests/test_memory_integration.py` 以公开 chat 流程和 schema-conformant fake tool payload 验证 Phase 7 episode 归档与 EpisodeSlice 派生写入幂等、chat 与 memory extraction 解耦、route-aware split extractor forced tool call 语义、profile-only / working-only / no-route 三类路由写入与跳过行为、split internal-task 生命周期、timeout/warning/partial-failure 语义，以及 session 级 each-turn memory extraction 排队行为；需要观测后台调度器时通过 `app.state` 暴露的测试钩子读取，而不是依赖路由闭包捕获细节
- **记忆/遥测测试整理**：遗留的 `test_memory.py` 与 `test_telemetry_integration.py` 已并入 `test_memory_manager.py`、`test_telemetry_setup.py`；`test_memory_manager.py` 中只验证 episode slice `top_k` 语义的用例会显式关闭 Stage 3 semantic lane，避免语义候选干扰该用例的排序边界；记忆文档同步补充了 `memory_recall` / `memory_hits` 的可观测性现状与 `TripEpisode` 仍未进入主召回链路的限制
- **Phase 7 交付物契约草案**：仓库内已新增 dual-deliverables 设计/计划文档，以及 `backend/tests/test_state_models.py`、`backend/tests/test_state_manager.py` 中针对 `plan.deliverables` 与 deliverable 文件读写/清理的待实现测试；当前主干实现尚未支持这些能力
- **评估管线**：golden cases（YAML）+ 断言评估 + 离线 runner；断言类型包含 `phase_reached`/`state_field_set`/`tool_called`/`tool_not_called`/`contains_text`/`not_contains_text`/`budget_within`/`memory_recall_field`（其中 `not_contains_text` 用于回归"机器感 checklist"类文案违规，`memory_recall_field` 用于断言 `last_memory_recall` 字段）；`memory_recall` tagged cases 会聚合 false skip / false recall / hit rate / zero-hit rate；`scripts/eval-stability.py` 生成 pass@k 稳定性报告（JSON + Markdown）；`scripts/failure-analysis/` 对 live backend 执行失败场景并产出分析报告
- **E2E 测试**：Playwright 三套专项配置——主流程（含 deterministic mock 的阶段切换）、重试体验（继续/重发/停止/不可恢复错误）、等待体验（ThinkingBubble 与工具耗时提示）；demo spec 基于 `demo-scripted-session.json` 稳定回放 Phase 1 → Phase 3 → Phase 5 → backtrack；Prompt 行为回归集中于 `e2e-phase1-no-offtopic.spec.ts`（验证 Phase 1 不主动追问非目的地字段）；本地 Playwright 运行态目录 `.playwright-mcp/` 与 `.playwright-cli/` 均作为开发机产物忽略，不进入版本库
- **运行**：`cd backend && pytest` / `npx playwright test`

---

*最后更新：见 `git log --oneline -1`*
