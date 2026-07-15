# Agent 可靠性审查与修复行动计划(2026-07-14)

## 背景与方法

针对"Phase 2/3 的 agent + 工具 + harness 设计能否可靠完成规划任务"做了一次代码级审查:三个独立深审(Phase 2 工具链路、Phase 3 并行编排链路、harness 校验层)+ 工具层数据源逐个核实。所有结论都有 file:line 证据,均基于当前 main 分支代码。

## 总体结论

- **架构方向正确**:领域状态机(Phase 1-4)、`TravelPlanState` 单一权威状态、plan writer 统一写通道、Phase 3 worker 只提交候选的写入边界——这些骨架决策与任务匹配,不需要推翻。
- **工具层扎实**:数据源真实(Google Maps/Directions、OpenWeather、Tavily、flyai CLI、小红书),无 mock 兜底,错误处理诚实。
- **但"可靠完成"不成立**:每条链路上都存在"踩中即无法自我恢复"的断点,且多数失败是静默的——用户和 LLM 都得不到可行动的错误信息。
- 修复面收敛在下述具体点上,不是架构性重写。

---

## P0:必须立刻修(主链路可用性,踩中即死循环或成果全丢)

### P0-1 Phase 3 降级串行不可达,失败进入跨回合空转重跑

- **问题**:并行失败率 >50% 时,orchestrator 仅输出"切换到串行模式"文案后返回空结果,本回合没有任何串行执行;下一回合进入条件不变(phase==3 且 daily_plans 为空),再次全量重跑并行。串行降级路径在代码上不可达。
- **为什么必须修**:任何一天连续失败后,系统不是降级而是无限重跑,用户每发一条消息烧一整轮并行 token,永远得不到结果。
- **证据**:`backend/agent/phase3/orchestrator.py:1048-1072`(yield 提示后 return 空)、`backend/agent/loop.py:257-259`(handoff 空即 DONE)、`backend/agent/phase3/parallel.py:22-34`(重入条件不变)。
- **修法**:降级分支不 return,改为在同一 run 内注入串行 Phase 3 上下文继续执行;或至少在 session 上打降级标记,下一回合 guard 检查该标记走串行。同时给"连续 N 次并行失败"设熔断。

### P0-2 部分成功 = 全部丢弃,且旧候选永不复用

- **问题**:某天重试后仍失败 → `final_dayplans` 缺天 → `replace_all_day_plans` 强制全量覆盖抛 `INCOMPLETE_DAILY_PLANS` → 已成功的 N-1 天全部丢弃。下一回合换新 `run_id`,磁盘上的旧候选不复用,从零重跑。
- **为什么必须修**:与 P0-1 叠加成"永远差一天、永远全部重来"的最坏循环;成本和延迟不可控。
- **证据**:`backend/tools/plan_tools/daily_plans.py:230-247`(强制完整覆盖)、`backend/agent/loop.py:296-320`(整批失败只输出一条文本)、`backend/agent/phase3/orchestrator.py:820`(新 run_id)、`orchestrator.py:614-627`(coverage_gap 仅 warning 不触发重派)。
- **修法**:缺天时降级为逐天 `save_day_plan` 部分落盘 + 明示用户缺哪几天;或 orchestrator 汇总时按 session(而非 run_id)复用已通过校验的候选,只重跑缺失天。

### P0-3 超时配置矛盾:300s run 超时包住整个并行编排

- **问题**:`run_timeout_seconds: 300` 包裹整个 agent.run(含编排、重试、重派),而单 worker 允许 1200s × 60 轮。多天行程 + 单天串行重试极易超时,commit 永远执行不到,成果全丢,回到 P0-1 循环。
- **证据**:`config.yaml:24` + `backend/api/orchestration/chat/stream.py:106`(run 超时包裹)、`config.py:216-222`(worker 超时/迭代上限)。
- **修法**:并行编排路径豁免 run 级超时或单独设编排预算(如 worker_timeout × 天数上限);worker 迭代上限 60 轮过高,建议压到 15-20 并配合 P0-2 的部分落盘。

### P0-4 forced_emit 模式封死唯一合法提交通道

- **问题**:worker 触发强制收口后,所有工具调用一律 skip——包括 `submit_day_plan_candidate`。而 prompt 同时要求"必须调用 submit 工具、不要输出裸 JSON"。worker 只有违反自己的系统指令才能成功,否则必然以 `RECOVERY_CHAIN_EXHAUSTED` 失败,直接推高失败率喂给 P0-1/P0-2。
- **证据**:`backend/agent/phase3/day_worker.py:1180-1214`(skip 无豁免名单)、`backend/agent/phase3/worker_prompt.py:112-123`(禁止裸 JSON)、`day_worker.py:400-409`(forced emit prompt 要求立即提交)。
- **修法**:一行级修复——forced_emit 的 skip 名单豁免 `submit_day_plan_candidate`。

### P0-5 Phase 2→3 骨架天数 gate 静默卡死

- **问题**:骨架天数 ≠ 行程天数时,`infer_phase` 永远返回 2 且不产生任何反馈;LLM 收不到错误、prompt 还指示它"推进到 Phase 3",只能向用户空喊"已进入下一阶段"而系统纹丝不动。写入侧(`select_skeleton`/`set_skeleton_plans`)也不校验天数。
- **证据**:`backend/phase/router.py:88`(gate 返回 2)、`router.py:122-124`(视为无转换,无 feedback)、`backend/tools/plan_tools/phase2_tools.py:262-331, 367-400`(写入不校验天数)。
- **修法**:双管齐下——`select_skeleton` 写入时校验 `len(days) == dates.total_days` 并返回可修复的 ToolError;gate 阻断时注入 runtime notice 说明差几天。

### P0-6 硬预算 gate 漏算交通 + 住宿

- **问题**:3→4 硬约束的预算检查只汇总活动 cost;已锁定机票/酒店成本只出现在从不阻断的软校验里。预算 1 万、交通住宿 9 千、活动 8 千的行程可通过 gate,交付一份实际 1.7 万的"1 万预算"行程。
- **证据**:`backend/harness/validator.py:67-68, 359-362`(仅活动成本)、`validator.py:352-387`(hard constraints 不含 lock budget)、`backend/api/orchestration/agent/hooks.py:623-624`(lock budget 仅软信号)。
- **修法**:`validate_hard_constraints` 的预算项改为 活动 + selected_transport + accommodation 总额;超支即阻断并报差额。

---

## P1:尽快修(正确性与用户可感体验)

### P1-1 brief 子阶段被架空,"trip_brief 硬锚点"无代码保障

- **问题**:`_hydrate_phase3_brief` 自动注入 destination 使 `trip_brief` 恒非空,子阶段推进条件退化为"有没有 dates"。用户 Phase 1 顺口给了日期 → brief 整段跳过,goal/pace 从未收集;而候选筛选、骨架密度、精排节奏全靠 pace/goal 驱动 → 粗排从"按画像定制"退化为"目的地通用生成"。
- **证据**:`backend/phase/router.py:23-31`(hydrate 在 phase>=2 生效)、`backend/state/models.py:433-434`(推进条件)。全链路无一处校验 trip_brief 含 goal/pace。
- **修法**:推进到 candidate 的条件改为显式检查 `trip_brief` 含 goal + pace(而非仅非空);hydrate 限定 `plan.phase >= 3`。需先确认这不是有意设计(git blame `router.py:24`)。

### P1-2 只锁住宿即进 Phase 3,交通搜索成死角,唯一逃生通道核爆全部状态

- **问题**:Phase 3 推进条件不含 `selected_transport`;用户说"住宿定新宿,交通再看看"→ 写入住宿即自动进 Phase 3,而机票/火车搜索是 `phases=[2]` 专属工具。想补搜只能 `request_backtrack(to_phase=2)`,但 `clear_downstream(2)` 清空 dates/trip_brief/候选/骨架/住宿全部。
- **证据**:`backend/phase/router.py:85`(推进条件)、`tools/search_flights.py:157`、`tools/search_trains.py:51`(phases=[2])、`backend/state/models.py:378-395, 490-495`(全清)。
- **修法**:任选其一——① 推进条件加入 selected_transport(或显式"跳过交通"标记);② 把大交通搜索工具开放到 Phase 3。推荐 ②,同时配合 P1-3。

### P1-3 变卦机制粒度过粗:回退核爆式、冻结不可逆

- **问题**:跨阶段回退把目标阶段之后所有字段重置为默认(为改一个骨架决策丢掉全部 Phase 2/3 成果);Phase 2 内无子阶段回退(lock 阶段改画像无工具可用);交付物冻结 once-only,冻结后没有任何解冻/重生成路径。
- **证据**:`backend/state/models.py:378-396, 490-495`(clear_downstream)、`backend/tools/plan_tools/backtrack.py:48-53`(to_phase 必须 < 当前)、`backend/tools/engine.py:94-132`(skeleton/lock 步缺上游写工具)、`backend/api/orchestration/session/deliverables.py:15-16`(冻结即 raise)。
- **修法**:分三级——① 轻量:Phase 2 各 step 开放上游写工具(改画像/候选不必回退);② 中量:`clear_downstream` 改为按依赖选择性清除(改骨架保留 dates/trip_brief/候选池);③ 冻结改为版本化(`deliverables_v2`)而非 once-only,允许 backtrack 4→3 后重新生成。

### P1-4 `check_availability` 的 date 参数是装饰,指定日期开放判断不存在

- **问题**:实际调 Places `open_now`(查询时刻是否营业),`date` 只被回显;`likely_open` 语义误导,节假日/指定日期闭馆查不出。骨架和精排都依赖这个工具做"日期可行性检查"。
- **证据**:`backend/tools/check_availability.py:83`(open_now)、`:16`(date 仅回显)。
- **修法**:用返回的 `weekday_text` + 目标日期星期几做真实判断;`likely_open` 改名 `open_now_at_query_time` 或移除;工具描述如实声明"无法判断节假日临时闭馆",引导 LLM 用 web_search 验证关键景点。

### P1-5 guardrail 输出校验 error 级分支是死代码

- **问题**:`validate_output` 对缺 price 等关键字段返回 `level="error"`,但消费方只处理 `level == "warn"`,error 级发现被原样丢弃。叠加 Google Places 住宿无价格(`price_per_night=None`),无价数据静默流入规划,预算核算失真。
- **证据**:`backend/harness/guardrail.py:160-169` vs `backend/agent/execution/tool_invocation.py:107`(`!= "warn"` 即 return)、`backend/tools/normalizers.py:140`(Google 无价)。
- **修法**:`tool_invocation.py:107` 改为处理 warn 和 error 两级;error 级至少把 reason 写入 suggestion,由 LLM 决定补查。

### P1-6 精排→粗排反馈回路断裂:`NEEDS_PHASE3_REPLAN` 是死代码,骨架局部修正无通道

- **问题**:worker 发现骨架不可行时,生产代码从不触发 replan 错误码,7b 分支不可达;Phase 3 中 `set_skeleton_plans` 不可用(phases=[2]),"第 3 天和第 4 天区域该对调"这类天级修正没有合法通道——只有静默将就或核爆回退两个极端。编译时的静默软化(locked 超限自动降级为候选)不回写骨架,骨架与实际日程漂移无记录。
- **证据**:`backend/agent/phase3/day_worker.py:42`(仅定义)、`backend/agent/phase3/orchestrator.py:1156-1188`(不可达分支)、`orchestrator.py:441-462`(静默降级)。
- **修法**:见"设计级改进 D3(再协商 + 黑板)";最小版本是让 worker 的结构化失败里真实产生 `NEEDS_PHASE3_REPLAN`,orchestrator 收到后允许修改骨架单天并只重派受影响天。

### P1-7 文本 JSON 兜底不校验 + 与 artifact store 混用会静默丢天

- **问题**:`extract_dayplan_json` 结果不做 day 匹配/结构校验即标记 success;orchestrator 汇总时只要有 artifact 候选就忽略文本兜底成功的天 → 缺天 → 触发 P0-2 整批失败。耗尽迭代路径还优先取未校验文本而非已校验提交。
- **证据**:`backend/agent/phase3/day_worker.py:1124-1132, 1444-1460`、`backend/agent/phase3/orchestrator.py:1194-1199`。
- **修法**:文本兜底结果过 `candidate_store` 同一套校验后写入 store(统一汇总口径);耗尽迭代路径优先取已校验的 `submitted_dayplan`。

---

## P2:排期修(质量、健壮性、防御纵深)

| # | 问题 | 证据 | 修法 |
|---|---|---|---|
| P2-1 | locked_pois 覆盖无全局校验,用户必去项可静默丢失;跨天预算/连贯性无校验 | `orchestrator.py:563-641` | 全局验证加"每天 locked POI 必须出现在 activities"检查,缺失升 error 触发重派 |
| P2-2 | soft judge 反馈计数器跨工具共享且永不重置,Phase 4 最需要时额度已耗光 | `hooks.py:941-952` | 计数器按 (tool, phase) 分桶,phase 变更时重置 |
| P2-3 | 同一 POI 被两天锁定时任务自相矛盾(同时出现在 locked 和 forbidden) | `orchestrator.py:466-480` | 编译时检出重复锁定即拒绝并要求先修骨架(配合 P1-6 的通道) |
| P2-4 | message_fallbacks 路径绕过全部 gate(不传 hooks) | `message_fallbacks.py:66` + `router.py:126` | 该路径同样传入 hooks |
| P2-5 | soft judge / gate 反馈直接 append 运行中消息,可能插进 tool_calls 与 tool 响应之间破坏协议 | `hooks.py:955-960, 1201-1203` vs `pending_notes.py:6-13` | 统一走 pending note 机制 |
| P2-6 | 并行入口在 LLM 看到用户消息前无条件劫持("先等等"也会触发整轮并行);骨架失配时异常穿透成硬砖 | `loop.py:394-403`、`orchestrator.py:429-433` | 入口前置轻量意图检查(或首轮让 LLM 确认再触发);`_split_tasks` 异常转为可对话的错误提示 |
| P2-7 | 时间冲突校验漏报:transport_duration_min 默认 0、跨午夜必误报、机场组仅 4 个日本机场 | `validator.py:19-25, 146-159`、`state/models.py:114` | transport 缺省时按 haversine 估算最小通勤;机场组数据化 |
| P2-8 | search_flights 的 Amadeus 分支:key 未配置 + 写死沙箱环境,永远空转;航班单点依赖 flyai | `search_flights.py:168, 173, 190`、`.env` 无 AMADEUS_* | 决策:弃用该分支或配正式环境;不要留永久空转的假分支 |
| P2-9 | 交付物冻结后 sqlite 文件与 plan.deliverables 指针可能不一致(backtrack 只清指针) | `plan_writers.py:310-334` 无 clear_deliverables 调用 | backtrack 清下游时同步 `state_mgr.clear_deliverables` |

---

## 设计级改进(不是 bug,是能力缺口)

### D1 把"排好"的定义显式化

当前系统对"排好"的实际定义 = "每天有内容 + 时间不穿模 + 活动费用不超"(结构完整),而非"贴合用户、值得出发"(质量达标)。建议显式定义验收清单并纳入硬校验:全天数覆盖、时间无冲突、**全口径预算**(含交通住宿)、locked POI 全部落位、首末日与大交通对齐、pace 符合画像。前四项可直接代码化(对应 P0-6、P2-1)。

### D2 粗排补"排布原则与优先级"

skeleton prompt 只有密度上限,缺分布策略与冲突取舍规则。建议补一节:必去项落位 > 区域连续性 > 密度分布;到达/离开日强制轻排(date_role 从可选改必填);重体力日不相邻。其中可代码化的(date_role 必填、首末日活动数上限)下沉为 `set_skeleton_plans` 写入校验。

### D3 精排→粗排反馈:hub-and-spoke 再协商 + 共享黑板(不要 peer-to-peer agent team)

评估过"让 Day Worker 互相沟通"的方案,结论是否决:核心矛盾是 worker↔骨架的纵向再协商缺失,不是 worker 之间缺横向对话;互聊会破坏确定性(协商不收敛)、并行收益(依赖等待)和写入边界(共识仍需单点落地),且在 300s 超时下不可行。替代方案:

1. **纵向再协商**:worker 上行结构化消息(`INFEASIBLE_DAY` / `OVERLOADED` / `SUGGEST_MOVE(poi, to_day)`),orchestrator 有权修改骨架单天并只重派受影响天(即 P1-6 的完整版)。
2. **共享黑板**:orchestrator 单写、worker 只读的三张表——POI 认领登记簿(防语义重复)、预算台账(跨天总量)、日边界锚点。提交时查表即拒,替代事后全局验证的大部分场景。
3. **有限波次**:如需更紧的跨天衔接,按奇偶天分两波,第二波拿第一波边界事实作约束。

### D4 Steering(运行中引导)

Phase 3 是长 run,当前用户只能等完或取消。`asyncio.Queue` + `/steer` endpoint,在 `run_llm_turn` 开头 drain,即可让用户中途纠偏。与 D3 配合价值最大(用户看到第 3 天不对可立即喊停单天)。此前架构对比文档已评为 P0 体验项。

---

## 建议施工顺序

1. **第一批(管道止血,可独立验证)**:P0-4(一行豁免)→ P0-3(超时预算)→ P0-2(部分落盘/候选复用)→ P0-1(真降级)。做完这批,Phase 3 从"踩中即死循环"变为"最坏部分交付"。
2. **第二批(gate 补漏)**:P0-5(天数 gate 反馈)→ P0-6(全口径预算)→ P1-5(guardrail 死代码)→ P1-4(check_availability)。
3. **第三批(体验与回路)**:P1-2(交通死角)→ P1-3(变卦分级)→ P1-1(brief 架空,先 git blame 确认意图)→ P1-6/D3 最小版。
4. **第四批**:P2 清单 + D1/D2 的 prompt 与校验下沉,D4 视排期。

每批完成后建议跑 `evals/trace_grader` 对比修复前后 trace,并针对 P0-1/P0-2 补"单天失败注入"回归测试(当前 `tests/test_orchestrator.py` 只覆盖了 replan 的测试注入路径)。

---

## 实施进度与接力说明(2026-07-15 更新)

**分支**:`fix/agent-reliability-p0`(从 `main` 起,已含 7 个 commit)。
**测试基线**:全量 `python -m pytest backend/tests/ -q` = 1993 passed / 6 failed。这 6 个失败是**本任务开始前就存在的预存问题**(依赖外部 LLM 网关的集成测试),与本次改动无关,勿花时间修:
- `test_api.py::test_quality_gate_emits_internal_task_when_blocking`
- `test_api.py::test_soft_judge_uses_forced_tool_call_after_replace`
- `test_api.py::test_generate_summary_rejects_exact_weather_when_forecast_is_reference_only`
- `test_realtime_validation_hook.py::test_plan_tool_injects_realtime_incremental_feedback`
- `test_realtime_validation_hook.py::test_save_day_plan_replace_existing_shows_soft_judge_after_tool_result`
- `test_save_day_plan_notes_bug_integration.py::test_save_day_plan_missing_activities_rejected_then_fixed`

验证某个失败是否预存:`git stash && pytest <该测试> && git stash pop`。

### ✅ 已完成并提交(P0 6/6、P1 7/7、P2 5/9)

| commit | 覆盖项 | 关键落点 |
|---|---|---|
| `bc1da20` | P0-1/2/3/4 | 详见各项 |
| `29bcb77` | P0-5/6、P1-4/5 | |
| `4a76400` | P1-1/2/3 | |
| `63f2834` | P1-6/7 | |
| `57aa842` | P2-1/2/3 | |
| `9160330` | P2-7 | |
| `148a1e0` | P2-9 | |

已完成项的具体改动位置(供 review / 回归):
- **P0-1**:`orchestrator.py` 失败率>50% 分支删掉 `return`,继续走 step 7 逐天串行重试。
- **P0-2**:`agent/phase3/parallel.py::build_phase3_commit_calls`(全覆盖→replace_all,缺天→逐天 save_day_plan);`loop.py` 调用它 + `phase3_partial_delivery_notice`。
- **P0-3**:`config.py`/`config.yaml` 新增 `Phase3ParallelConfig.orchestration_timeout_seconds`(默认 None=豁免);`worker_max_iterations` 60→20;`stream_runtime.py::resolve_run_timeout_seconds` + `stream.py` 用它。
- **P0-4**:`day_worker.py` forced_emit skip 增加 `not any(... submit_day_plan_candidate ...)` 豁免。
- **P0-5**:`phase2_tools.py::_validate_skeleton_day_count`(set_skeleton_plans/select_skeleton 写入校验);`repair_hints.py::build_skeleton_day_mismatch_message`(gate 阻断反馈)。
- **P0-6**:`validator.py::validate_hard_constraints` 预算改为 活动+交通+住宿。
- **P1-1**:`router.py::_hydrate_phase3_brief` 保持 phase>=2 但 `models.py::infer_phase2_step_from_state` brief→candidate 要求 `goal`+`pace`。
- **P1-2**:`search_flights.py`/`search_trains.py` phases=[2,3];`phase2_tools.py` set_transport_options=[2,3]、select_transport=[2,3,4];prompts.py 相应文案。
- **P1-3**:`engine.py` skeleton/lock 步开放上游写工具;`models.py::_PHASE_DOWNSTREAM[2]` 选择性清除(保留 dates/trip_brief/candidate_pool/shortlist);`session/deliverables.py` 冻结改版本化(version 字段递增)。
- **P1-4**:`check_availability.py` 用 weekday_text 按目标日期星期几判断,新增 `open_on_date`/`hours_on_date`/`open_now_at_query_time`,声明节假日局限。
- **P1-5**:`tool_invocation.py::validate_tool_output` 处理 warn+error 两级。
- **P1-6**:`day_worker.py::_REPORT_SKELETON_INFEASIBLE_SCHEMA` + 分派早返回产生 `NEEDS_PHASE3_REPLAN`;`orchestrator.py` 7b 分支不再整批 return,走部分交付;`worker_prompt.py` 加说明。
- **P1-7**:`day_worker.py::_accept_text_fallback_dayplan`(文本兜底过 candidate_store 校验并写 store);耗尽路径优先取 submitted。
- **P2-1**:`orchestrator.py::_validate_locked_pois_present` + `self._locked_pois_by_day`;`locked_poi_missing` 加入 `_LOCAL_REPAIR_ISSUE_TYPES`。
- **P2-2**:`hooks.py` soft judge 计数器改 `_soft_judge_repair_feedback_buckets` 按 (tool, phase) 分桶;`stream.py` 初始化。
- **P2-3**:`orchestrator.py::_compile_day_tasks` 重复锁定保留首个 owner 并从后续天移除。
- **P2-7**:`validator.py::_estimated_commute_min`(haversine,transport_duration_min=0 时兜底);机场组抽到 `harness/airport_groups.py` 并扩展到 CN/HK/TW/SEA/KR。
- **P2-9**:`finalization.py::finalize_agent_run` phase 下降且 deliverables 指针消失时补调 `state_mgr.clear_deliverables`(覆盖工具经路 request_backtrack)。

### ⏳ 未完成(接力从这里开始)

**卡住需决策的项:**
- **P2-4**(message_fallbacks 绕过 gate):`session/message_fallbacks.py:66` 的 `check_and_apply_transition` 没传 hooks。**卡点**:`ChatStreamDeps`(stream.py) 没暴露 hooks,`main.py` 装配处也没传。要修需改三处签名:`ChatStreamDeps` 加 hooks 字段、`main.py` 传入、`stream.py:374` 调用 `apply_message_fallbacks` 时透传。**评估**:该路径主要回填 destination/dates 触发 Phase 1→2,而 gate 硬约束主要作用于 2→3/3→4,收益边际,建议低优先或确认是否值得改配管。

**可继续做的自包含项:**
- **P2-5**(soft judge/gate 反馈直接 append 运行中消息,可能插进 tool_calls 与 tool 响应之间破坏协议):`hooks.py:955-960` 附近的 `active_runtime_messages(session).append(...)` 改为统一走 `pending_notes.py` 机制。需先读 `agent/execution/pending_notes.py` 理解 pending note 如何在安全点注入。
- **P2-6**(并行入口无条件劫持 + 骨架失配异常穿透):`loop.py` 并行入口(should_enter_parallel_phase3 系列)前置轻量意图检查;`orchestrator.py::_split_tasks` 的 `raise ValueError("未找到已选骨架方案")` 转为可对话错误提示而非硬砖。
- **P2-8**(Amadeus 空转分支):已被"数据源下线"的条件注册基本覆盖(`api/orchestration/agent/tools.py` 在 Amadeus key 与 flyai 均不可用时不注册 search_flights)。剩余工作仅是决策**删除** `search_flights.py:167-` 的 Amadeus sandbox 分支还是保留;当前它只在配了 key 时才跑,不是永久空转。建议直接删该死分支或加注释说明。

**设计级(计划本身标为视排期,非 bug):**
- **D1**(验收清单代码化):前四项已随 P0-6/P2-1 落地;剩 pace 符合画像、首末日轻排等可继续下沉到硬校验。
- **D2**(粗排排布原则):`date_role 必填`、首末日活动数上限 下沉为 `set_skeleton_plans` 写入校验。
- **D3**(hub-and-spoke 再协商 + 黑板):P1-6 已做最小版(worker 上报通道);完整版需 orchestrator 改单天骨架 + 只重派受影响天 + 共享黑板三张表,是较大设计工作。
- **D4**(Steering 运行中引导):`asyncio.Queue` + `/steer` endpoint,长 run 中途纠偏。独立特性,未开始。

**建议接力顺序**:P2-5 → P2-6 → P2-8(决策删分支)→ D2/D1 剩余下沉 → P2-4(先定是否改配管)→ D3 完整版/D4。每做完一项跑一次相关子集测试,全部做完跑一次全量确认仍是 6 个预存失败。

---

## 追加(2026-07-14 实施):数据源下线与免费替代

背景:小红书 CLI 触发风控、飞猪 flyai CLI 转付费,两个数据源不可用。用户决策:不使用 Amadeus,免费方案优先,无方案则降级 web_search。已实施:

1. **flyai / xhs 下线**:`config.yaml` 双开关关闭;`api/orchestration/agent/tools.py` 改为条件注册(xhs 三件套按 `xhs.enabled`;`quick_travel_search`/`ai_travel_search`/`search_travel_services` 仅 flyai 可用时注册;`search_flights` 在 Amadeus key 与 flyai 均不可用时不注册)。
2. **火车 → 12306 直连**(免费,已实测):新增 `tools/train12306_client.py`(车站码表 + init 会话 + leftTicket 查询,查询路径动态解析 + 兜底轮换),`search_trains` 重写为 12306 后端,返回车次/时刻/历时/席别余票。**不含票价**——prompt 指导用 web_search 补票价。真实验证:北京→上海查得 54 车次。
3. **UGC → web_search 域内搜索**(零新增成本,已实测):`web_search` 透传 Tavily `include_domains`/`exclude_domains`;各阶段 prompt 中"小红书三件套"统一替换为"UGC 域内搜索"(`include_domains=["xiaohongshu.com","mafengwo.cn","qyer.com"]`)。真实验证:域内搜索能返回小红书/马蜂窝结果。局限:拿不到评论区共识与完整正文。
4. **航班 → web_search 降级**:lock 阶段 prompt 改为"web_search 查航线/价格带,给方向性方案,明确提示用户到购票平台核价后再锁定"。
5. **Phase 4 服务推荐 → web_search 降级**;住宿保留 Google 分支,prompt 注明房价需 web_search 补并以平台为准。
6. **测试**:重写 `test_search_trains.py`(12306 mock);更新 5 处断言旧设计的测试;全量 1991 passed,余 6 个失败为预存问题(依赖外部 LLM 网关的集成测试,stash 验证与本次无关)。

能力恢复评估:火车≈100%(且比 flyai 更实时)、UGC≈70%、国内航班≈40%(web_search 兜底)、酒店≈70%。
后续注意:12306 为非官方接口,有频控风险,失败时工具会引导 LLM 降级 web_search;若后续恢复 flyai/xhs,只需改回 config 开关并回调 prompt 措辞。

- 写入边界:worker 只读工具 + 候选 staging + 唯一 commit 通道(`day_worker.py:1493-1508`、`loop.py:261-276`)。
- 工具入参结构校验闭环(`daily_plans.py:67-193`)。
- 阶段推进由状态推断,3→4 硬约束 gate 无重试上限(`hooks.py:1059-1086`)。
- Phase 4 交付物一致性 gate(日期越界/航班号不符/预算偏差 >2% 即改写为 error,`hooks.py:340-528`)。
- 工具数据源全部真实、无 mock 兜底;`calculate_route` 错误映射与降级完备;`check_weather` 对超窗日期诚实标注。
- Phase 2 candidate/skeleton 的 prompt 方法论(先广后窄、经验采集、差异方案)与工具门控一致。
