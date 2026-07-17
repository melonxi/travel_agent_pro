# 交接审查文档:D3/D4 修复实施情况复审(2026-07-15)

> **给接手 AI 的话**:你在对一批已实施的 bug 修复做独立代码审查。这份文档提供完整背景、审查范围、已确认事实与待核实疑点,你**不需要**本次对话的历史即可独立工作。请只报**正确性 bug**(会导致丢天、错误交付、崩溃、静默失效),辅以少量高置信度的健壮性问题。每个发现请给 `file:line` 证据 + 具体触发场景 → 错误结果。**不要改代码,只审查并报告。**

---

## 0. 一句话背景

项目 `travel_agent_pro` 是一个 LLM 旅行规划 Agent。Phase 3 是"逐日详排"阶段,采用 Orchestrator-Workers 并行架构:一个纯 Python 的 `Phase3Orchestrator` 把选定骨架拆成每天一个 `DayTask`,并发起 N 个 Day Worker,worker 只提交候选(写盘 + 内存结果),Orchestrator 负责收集、校验、汇总,最终由 AgentLoop 用 `replace_all_day_plans` 工具写入权威状态。

近期做了两个设计级功能:
- **D3**:精排→粗排"再协商 + 共享黑板"。worker 发现骨架不可行时上报结构化 `ReplanRequest`(INFEASIBLE_DAY / OVERLOADED / SUGGEST_MOVE);Orchestrator 有权改单天骨架副本并只重派受影响天。共享黑板(`Phase3Blackboard`)三张表:POI 认领登记簿、预算台账、日边界锚点,worker 提交候选时"查表即拒"。
- **D4**:运行中引导(steering)。用户在长 run 进行中可 POST `/api/chat/{session_id}/steer` 发一条纠偏消息(如"第 3 天别排太满"),Agent 在下一个安全点消费,run 不中断。

D3/D4 首版实施后经过一轮审查,发现约 12 个 bug。**本次要审查的,是针对那批 bug 的修复**——修复已完成、在工作区(尚未 commit)。你的任务是判断这些修复是否正确、有没有引入新问题。

---

## 1. 审查范围(精确 diff)

- **基线 commit**:`27c7d5c`(D3/D4 首版,含被修复的原始 bug)。
- **待审改动**:当前工作区未提交改动。用 `git diff 27c7d5c` 或 `git diff HEAD`(HEAD 就是 27c7d5c)获取完整 diff。
- **核心改动文件**(按重要度):
  - `backend/agent/phase3/orchestrator.py`(改动最大,+641 行区域)
  - `backend/agent/phase3/renegotiation.py`(黑板 + 再协商)
  - `backend/agent/phase3/candidate_store.py`(候选状态标记)
  - `backend/agent/steering.py`(steering drain)
  - `backend/api/orchestration/chat/stream.py`、`backend/api/routes/chat_routes.py`(/steer 端点 + queue 生命周期)
  - `frontend/src/types/plan.ts`、`frontend/src/components/ChatPanel.tsx`(前端类型 + 消费)
- **测试**:`test_phase3_renegotiation.py`、`test_phase3_candidate_store.py`、`test_parallel_phase3_integration.py`、`test_steering.py` 有对应新增/修改。

---

## 2. 被修复的原始 bug 清单(修复应覆盖这些)

| # | 原始 bug | 修复思路 |
|---|---|---|
| 1 | **黑板拒绝被磁盘候选绕过**:worker 提交时已把候选写盘,`_accept_worker_dayplan` 只改内存 result 为 rejected,但 step 8 汇总优先从磁盘 `load_latest_candidates` 取(不看状态)→ 被拒候选仍交付 | candidate_store 加 `status` 字段与 `update_candidate_status()`;`load_latest_candidates(accepted_only=True)`;step 8 用 accepted_only |
| 2 | **POI 登记簿 activity 名 vs POI 名不匹配**:seed 用 POI 名("浅草寺"),accept 用 activity.name("参观浅草寺"),两套命名空间不匹配 → 防重复失效 + 同名泛化活动("午餐")误拒 | 新增 `canonical_poi_key()` 归一化;`_activity_poi_keys()` 优先用 poi_id/place_id;泛化名跳过 |
| 3 | **预算台账漏算交通/住宿**:住宿字段 `price_per_night` 根本不存在于 `Accommodation`(只有 area/hotel)→ 住宿恒 0;交通用 ad-hoc key 累加不读 segments | 复用 `validator.py` 的 `_selected_transport_cost` / `_selected_accommodation_nightly_price` / `_trip_nights` |
| 4 | **受影响天熔断拆散 MOVE 成对天**:`sorted(affected_days)[:max_affected]` 截断把 MOVE 的 (source,target) 拆开 → POI 丢失或两天都在 | 删掉末尾截断,改为分支入口预检 `proposed_days > max_affected` 就整条降级;有 MOVE 时 `max_affected≥2` |
| 5 | **live registry 从不下发 + 精确 vs 模糊匹配不一致**:`snapshot_forbidden_for_day` 是死代码,重派 worker 拿不到运行时认领 | 新增 `_refresh_task_blackboard_snapshot()` 把 forbidden 快照塞进 task.forbidden_pois |
| 6 | **黑板拒绝没转成 repair hint**:被拒天 step 7 重试拿相同上下文 → 大概率再撞 | step 7 重试把 error_msg 加进 repair_hints |
| 7 | **再协商 attempt=4 丢 steering hint**:`_rebuild_tasks_for_days` 新 split 出的 rd_task 没调 `_apply_steer_hints_to_task` | 再协商/repair 重派前都调 `_apply_steer_hints_to_task` |
| 8 | **MOVE 目标天容量只算 locked**:`_capacity_left` 不减 candidate → 迁入已满天 | `_capacity_left` 改为 `max_core - locked - candidates` |
| 9 | **`to_day="0"` 触发 AssertionError**:字符串 "0" 绕过正数校验 + assert 崩溃 | 字符串解析加 `parsed>0`;`assert` 改为优雅 return unresolved |
| 10 | **/steer 三个静默丢失窗口**:承诺 accepted 但可能丢(run 尾部、step7 期间、洪水裁剪) | queue `maxsize=64`;满时返 429;`{"status":"queued"}` 不再 "accepted";洪水不再默认裁剪;step7 内加 drain |
| 11 | **前端构建阻断**:`ChatPanel.tsx` 比较 `event.stage === 'steering_ack'`,但 `AgentStatusEvent.stage` 联合类型没有该成员 → `tsc` TS2367 失败 | `plan.ts` 联合类型加 `'steering_ack'` |
| D4-new | **运行中 worker 收不到 steering**:steering 到达时目标天 worker 正在跑(已持有旧 task),只改 task.repair_hints 运行中 worker 读不到,又不触发重派 → "已 ack 实际空操作" | 新增 `active_worker_days`;`_drain_steering` 里 `active_days` 也走 redispatch;新增 late-redispatch 段(attempt=5) |

---

## 3. 已确认的事实(可作为审查地基,已核实 file:line)

- **测试基线**:全量 `python -m pytest backend/tests/ -q` = `6 failed, 2034 passed`。这 6 个失败是**修复前就存在的预存问题**(依赖外部 LLM 网关的集成测试),与本次无关:`test_api.py::test_quality_gate_emits_internal_task_when_blocking`、`test_api.py::test_soft_judge_uses_forced_tool_call_after_replace`、`test_api.py::test_generate_summary_rejects_exact_weather_when_forecast_is_reference_only`、`test_realtime_validation_hook.py::test_plan_tool_injects_realtime_incremental_feedback`、`test_realtime_validation_hook.py::test_save_day_plan_replace_existing_shows_soft_judge_after_tool_result`、`test_save_day_plan_notes_bug_integration.py::test_save_day_plan_missing_activities_rejected_then_fixed`。验证某失败是否预存:`git stash && pytest <该测试> && git stash pop`。
- **前端构建**:`cd frontend && npm run build` 已恢复通过(#11 修复到位)。修复方式仅改 `plan.ts:250` 联合类型加 `'steering_ack'`,`ChatPanel.tsx:766` 的消费逻辑本就正确。
- **候选写盘默认状态**:worker 提交时 `candidate_store.py:49` 写 `"status": "submitted"`。`load_latest_candidates(accepted_only=True)` 在 `candidate_store.py:80` 过滤 `payload.get("status") != "accepted"`。
- **`_accept_worker_dayplan` 五个调用点的 attempt 号**:attempt=1(主收集,orchestrator.py 约 1263)、attempt=2(step7 重试,约 1444)、attempt=5(late steering redispatch,约 1548)、attempt=4(7b 再协商重派,约 1689)、attempt=3(repair worker,约 1925)。`load_latest_candidates` 每天按**最大 attempt 号**取最新文件(`candidate_store.py:78-80`)。
- **`_time_cmp`**:解析失败返回 0(即"相等"),不抛异常。
- **收集循环结构**:`while pending`(约 1241)用 `asyncio.wait(FIRST_COMPLETED)`,只处理 `done_set` 里已完成的 worker;正在跑的 worker 仍在 `pending` 里,其 `task.day` 在 `active_worker_days` 集合中。

---

## 4. 需要重点核实的疑点(我审查中已观察到,但**未完成验证**)

> 这些是我停下前正在核实的高价值疑点。请独立验证每一条:成立就报,不成立请指出证据在哪。**不要因为我列了就默认成立。**

### 疑点 A(最高危):`accepted_only=True` 是否会丢天

step 8 现在用 `load_latest_candidates(accepted_only=True)`(orchestrator.py 约 1776),而写盘默认状态是 `"submitted"`。**只有走过 `_accept_worker_dayplan` 且被标 accepted 的候选才会被交付。** 请穷举所有"产出成功交付的天"的路径,确认每条都调了 `update_candidate_status(status="accepted")`:
- 五个 `_accept_worker_dayplan` 调用点(见 §3)是否覆盖了所有成功路径?
- **text-fallback 路径**:worker 通过文本兜底(`_accept_text_fallback_dayplan` 或类似,在 `day_worker.py`)产出 dayplan 时,写盘 status 是什么?会不会是 submitted 且没被标 accepted → 被 accepted_only 过滤 → 丢天?
- 第 8b 步全局重派后再次 `load_latest_candidates(accepted_only=True)`(约 1858)的 replacement 逻辑:重派成功天有没有被标 accepted?
- `update_candidate_status` 传入的 attempt 号,与该 worker `run_day_worker(attempt=X)` **写盘用的 attempt** 是否一致?不一致会标不到文件(该函数找不到文件返回 False,静默 no-op)→ 候选留 submitted → 丢天。

### 疑点 B:steering redispatch 的 attempt=5 与 race

- **attempt 号时序倒挂**:同一天若先 late-steering-redispatch(attempt=5)、后进 7b 再协商(attempt=4),`load_latest_candidates` 取**最大 attempt=5**,即取到**先执行**的旧结果而非后执行的 attempt=4 → 交付错版本。请核实这个时序是否可能发生。
- **正在跑的 worker 未取消**:steering 到达时目标天 worker 正在跑(active_days),被加入 `_steer_redispatch_days`,但那个 asyncio task **没被取消**,会继续跑完、结果进 successes。late-redispatch 段(约 1350、约 1511)从 successes 摘除它再 attempt=5 重跑。核实:(a) 若正在跑的 worker 最终**失败**(没进 successes),`_steer_redispatch_days` 有它但 successes 没有,摘除逻辑 `for s in successes` 找不到 → 会不会漏派 / 空转?(b) late-redispatch 段能否保证正在跑的 worker 已 await 完成、结果已可摘除,还是存在 race?

### 疑点 C:canonical POI key 的归一化边界

`canonical_poi_key()`(renegotiation.py 约 41)去标点、小写、去"参观/游览/前往/漫步/逛"等前缀 + "参观/游览"后缀。
- **过度归一 → 误合并**:两个本来不同的 POI 会不会归一成同一 key(如以这些动词开头的真实地名、去标点后塌缩)?→ 误拒合法天。
- **归一不足 → 该合并没合并**:seed 用 locked POI 名,accept 用 activity 的 location.name / name。经 canonical 后能匹配吗?`seed_from_locked` 里 `aliases = {key, poi.strip()}` 同时登记 canonical key 和原始 strip 名两个键 → 一个 POI 占两个 registry 条目,`release_day` 按 owner 删能否删干净?

### 疑点 D:`_capacity_left` 是否矫枉过正

改成 `max(0, max_core - locked - candidates)`(renegotiation.py 约 311)。候选池通常给满(locked+candidate ≥ max_core 很常见)→ `_capacity_left` 恒为 0 → **所有 SUGGEST_MOVE 目标天都被判满载拒绝 → MOVE 功能实际失效**。请判断这是否在常见配置下把 MOVE 变成死功能。

### 疑点 E:MOVE 熔断预检的顺序依赖 + 边界校验

- 熔断预检 `proposed_days = outcome.affected_days | {req.day, req.to_day}` 用**当前已累积**的 affected_days → 多 request 时结果依赖处理顺序(前面的 request 先占了 affected_days 名额,后面 MOVE 更易被拒)。这算不算确定性/公平性问题?
- 新增边界校验 `if first_start and last_end and _time_cmp(first_start,last_end)>0: return False`(renegotiation.py 约 246):跨午夜活动(23:00 开始、01:00 结束)会不会 first_start > last_end 误报为"首活动晚于末活动"?

### 疑点 F:测试脆弱性(非阻塞,但值得一提)

- `test_main_structure.py::test_api_orchestration_package_splits_chat_stream_details` 断言 `stream.py < 400 行`。D4 改动后 `stream.py` = **398 行**,距上限仅 2 行。全量测试中该测试曾**偶发失败一次**(重跑不复现,可能是后台 run 撞了中间态)。这是个脆弱 canary:下次给 stream.py 加几行就会触发。建议核实是否真实存在非确定性,或只是一次性假阳。

---

## 5. 建议的审查方法

1. `git diff 27c7d5c` 通读全 diff,重点是 orchestrator.py 的收集循环(约 1241)、late-redispatch 段(约 1511-1570)、step 8 汇总(约 1776-1790)、8b 重派(约 1850-1970);renegotiation.py 的 `canonical_poi_key`/`_activity_poi_keys`/`Phase3Blackboard`/`renegotiate_skeleton`;candidate_store.py 全文。
2. 逐条核实 §4 的疑点 A–F,每条给 file:line + 触发场景 → 错误结果。
3. 额外扫一遍我可能漏掉的:被删除代码丢了什么不变量、跨文件调用点是否被新签名破坏(如 `_accept_worker_dayplan` / `load_latest_candidates` / `_drain_steering` 的所有调用点)、Python 陷阱(可变默认参数、闭包晚绑定、dict 迭代时删除)。
4. 跑测试作证:`python -m pytest backend/tests/test_phase3_renegotiation.py backend/tests/test_phase3_candidate_store.py backend/tests/test_parallel_phase3_integration.py backend/tests/test_steering.py -q`(应全绿);全量 `python -m pytest backend/tests/ -q`(应为 6 failed 预存 + 其余 passed)。前端:`cd frontend && npm run build`(应通过)。
5. 注意:测试可能因为构造了**理想数据**(activity.name 恰好等于 POI 名、fake_worker 不写盘绕过磁盘路径)而绕过真实 bug 路径——**测试全绿不等于修复正确**。判断修复正确性时以真实数据流为准,并留意测试是否真的覆盖了被修 bug 的触发路径。

---

## 6. 输出格式

请按严重度排序,每个发现给:
- `file:line`
- 一句话 bug 描述
- 具体触发输入/状态 → 错误输出/崩溃
- 若涉及 §2 的某个原始 bug,注明修复是"未生效 / 部分生效 / 引入新问题"

若某疑点核实后不成立,请明确写"疑点 X 不成立"并给否证的 file:line —— 这同样有价值。
