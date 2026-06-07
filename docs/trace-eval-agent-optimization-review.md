# Trace Eval 与 Agent 优化问题 Review

本文只做工程 Review，目标是判断 8 条问题是否成立、哪些说法需要修正，以及后续应该怎么定向优化 agent 和 trace eval 流程。

## 0. 结论

8 条里，1、3、4、5、6、7 基本成立；2 成立但原始 run 级 token / duration 数字不能直接采信；8 成立一半，因为当前保守确认是已有策略，不是偶然 bug。

最高优先级不是继续加 grader，而是先修 trace 口径。当前 run trace 和 run summary 都混入了 session 累计 stats。这个不修，后续所有“单轮成本、单轮耗时、单轮工具调用数”的 eval 都会失真。

## 1. Run 级 trace 实际是累计的

### Review

成立，而且是最高优先级。

代码证据：

- `SessionStats` 是 session 级对象，内部维护累计列表：`llm_calls`、`tool_calls`、`memory_hits`、`recall_telemetry`。
- `build_trace_events_from_stats(...)` 会遍历这些累计列表的全部内容。
- `persist_trace_run_safely(...)` 每次 flush 时把全量 stats 转成当前 `run_id` 的 events。
- `update_run_summary(...)` 也使用 `stats.total_input_tokens` / `stats.total_output_tokens`，所以 run summary 同样被累计污染。

因此后面的 run 包含前面 Phase 2 的 `set_candidate_pool`、`set_shortlist`、`set_skeleton_plans` 等事件，不是展示问题，而是数据口径问题。

### 修正后的说法

当前 `trace_events.run_id` 不是严格的 run scope，而是“把 session 累计 stats 复制到当前 run_id”。这会同时污染：

- run 级事件数
- run 级 tool call 数
- run 级 LLM token
- run 级 duration
- run 级 failure 统计
- replay / stability / cost delta 的解释

### 改进建议

优先做 offset 方案，改动小：

- run start 时记录当前 stats offset：
  - `llm_calls_offset`
  - `tool_calls_offset`
  - `memory_hits_offset`
  - `recall_telemetry_offset`
- flush trace 时只读取 offset 之后的新增记录。
- `update_run_summary(...)` 只汇总本轮 delta。
- 保留 session 聚合视图，但明确命名为 `session_trace` 或 `session_trace_summary`，不要伪装成 run trace。

### 验收标准

- 后续 run 不再重复出现前序 run 的工具失败。
- `trace_events.run_id` 内只包含本轮新增事件。
- run summary 的 token / duration / tool_count 等于本轮 delta。
- session 级累计数据通过单独聚合查询得到。

## 2. Phase 2 太贵、太慢、工具调用过密

### Review

方向成立，但原始数字要修正口径。

真实问题是 Phase 2 的外部搜索链路过重，尤其是 `xiaohongshu_search_notes` / `xiaohongshu_read_note` 多次出现秒级调用。但因为第 1 条 trace 累计污染存在，不能直接把某个 run summary 里的 token / duration 当成严格单轮指标。

### 修正后的说法

Phase 2 的搜索深度足够，但缺少预算控制和去重策略。当前 evidence 更适合表述为：

- candidate/skeleton 阶段发生大量小红书 search/read。
- 多个工具调用耗时在 4s 到 12s 量级。
- agent 做到了 search-before-shortlist，grader 也通过。
- 但搜索预算没有显式上限，成本和延迟不可控。

### 改进建议

- 给同类 query 做 session cache。
- 对搜索 query 做规范化去重，例如目的地、区域、意图、关键词归一化。
- candidate 阶段设置预算：
  - 最多 N 次 search。
  - 每个 POI / intent 最多 M 次 read。
  - 超预算后必须进入粗排或请求用户放宽预算。
- 先用搜索结果标题/摘要粗排，再只读 top notes。
- trace grader 增加 `tool_budget_exceeded` warning。
- TraceViewer 展示阶段预算消耗：`used / budget`。

### 验收标准

- Phase 2 在同一 golden case 下，工具调用数下降。
- shortlist 质量不下降。
- 超预算时 trace 中出现 warning，而不是静默继续搜索。

## 3. 工具参数失败能自动修，但修得有点笨

### Review

成立，但要区分工具。

实跑里：

- `update_trip_basics` 失败一次后成功，且 suggestion 有实际帮助。
- `set_skeleton_plans` 第一次失败后成功，suggestion 也有方向。
- `generate_summary` 连续两次 `INVALID_ARGUMENTS`，第三次才成功；这里的问题最明显。

代码上 `generate_summary` 的 `ToolError` 只给了错误 message，没有 suggestion。工具 schema 也只是描述“必须包含 H1 和逐日小节”，没有给最小合法模板。

### 修正后的说法

不是所有 validator 都弱。真正要优先修的是高价值 writer tool，尤其是 `generate_summary`：它处在 Phase 4 冻结交付物之前，连续参数失败会直接拖慢最终交付。

### 改进建议

- 给 `generate_summary` 的 `INVALID_ARGUMENTS` 增加结构化 suggestion。
- 工具错误返回 required fields、invalid fields、最小合法 markdown 示例。
- Phase 4 prompt 增加 `generate_summary` 最小合法例子。
- 连续同一工具失败 2 次后进入“参数修复模式”：
  - 不再自由生成大段内容。
  - 先列 required fields。
  - 再基于上一次错误只修参数结构。
- trace grader 增加 `repeated_tool_argument_failure`。

### 验收标准

- `generate_summary` 连续失败不超过 1 次。
- 失败事件 payload 里有可执行 repair hint。
- grader 能标出“同一 writer tool 连续 2 次失败”。

## 4. Phase 3 soft judge 发现问题，但最终没有完全修掉

### Review

成立。

`on_soft_judge` 注册在 `after_tool_result`，会在 `save_day_plan`、`replace_all_day_plans`、`generate_summary` 成功后做质量评审。它会把建议写进 message history，也会把 judge score 挂到最新 tool call 记录上。

问题是它当前更像 advisory，不是 repair loop。实跑中 soft judge 指出 Day 1 抵达日太紧、活动数偏多、Day 3 有空档、偏好覆盖不足。最终产物确实吸收了一部分偏好，例如药妆、甜品、拉面，但 Day 1 仍然有 6 个活动，和 balanced 节奏冲突。

### 修正后的说法

soft judge 有发现能力，也能影响后续生成；缺的是“从 warning 到重写相关 day”的闭环。

### 改进建议

- 把 soft judge feedback 结构化成 action items：
  - affected_day
  - issue_type
  - severity
  - repair_instruction
- 对明确 hard constraint 的问题使用 deterministic rubric，例如 `balanced_day_activity_limit`。
- `replace_all_day_plans` 成功后，如果 soft judge 分数低于阈值，不直接进入 Phase 4。
- 对低分 day 只重写相关 day，不重跑全量行程。
- 区分 warning 和 error：
  - balanced 节奏下活动数超限属于 error。
  - 餐厅不够具体可以是 warning。

### 验收标准

- balanced 用户偏好下，到达日活动数超限会被 deterministic rubric 拦截。
- soft judge 分数低于阈值时，agent 生成一次 targeted repair。
- repair 前后的 judge score 和 changed fields 都进入 trace。

## 5. 远期天气被当成确定天气

### Review

成立。

`check_weather` 在没有匹配目标日期时，会返回：

```text
精确日期预报不可用，返回最近预报作为参考
```

但最终 checklist / travel plan 写成了确定的 “20°C 中雨” 一类表达。这不是工具没有提示，而是 agent 没有正确传播 forecast uncertainty。

### 修正后的说法

天气工具已经暴露了不确定性，但 Phase 4 生成没有强制保留这个不确定性。

### 改进建议

- `check_weather` payload 增加结构化字段：
  - `forecast_precision: exact | nearest_available | unavailable`
  - `matched_date`
  - `requested_date`
- Phase 4 prompt 明确：
  - `nearest_available` 只能写“近期天气参考”。
  - 禁止写成目标日期确定预报。
  - checklist 必须加入“出发前 3 天复查天气”。
- trace grader 增加 `future_weather_not_treated_as_exact`。

### 验收标准

- 远期日期没有精确预报时，最终文案不出现确定天气表述。
- checklist 自动包含复查天气项。
- grader 能检测 weather note 被忽略的情况。

## 6. `search_travel_services` 空数据却 status=success

### Review

成立。

工具层现在把 `flyai_client.fast_search(...)` 的返回直接 map 成 services。即使 item 核心字段为空，也会返回普通 success：

- `title: ""`
- `price: null`
- `booking_url: null`
- `image_url: null`

这会让 agent 把“调用成功”误解成“结果可用”。

### 修正后的说法

这里不是 agent 推理问题，主要是工具结果质量没有被建模。工具状态应该区分 transport 成功、provider 成功、业务结果可用。

### 改进建议

- 工具层过滤无效 item。
- 如果过滤后为空，返回：
  - `status=empty`，或
  - `warning=EMPTY_RESULT`
- result payload 增加 `quality`：
  - `usable`
  - `empty`
  - `partial`
  - `low_confidence`
- trace grader 增加 `empty_tool_result_not_used_as_evidence`。
- TraceViewer 对 `success + empty/low_quality` 标黄。

### 验收标准

- 空服务结果不再显示成普通成功。
- agent 不会把空 result 写进最终计划。
- TraceViewer 能一眼区分“工具调用成功”和“信息质量可用”。

## 7. 交付物冻结和 soft judge 顺序拧巴

### Review

成立。

代码上，`generate_summary` 工具成功后，`stream.py` 会立即调用 `persist_phase4_deliverables(...)`。但 soft judge 是 `after_tool_result` hook，它会在工具结果后追加质量反馈。结果就是：deliverable 已冻结，message history 里又出现 soft judge 低分和改进建议。

这不是单纯 UI 问题，而是生命周期顺序问题。

### 修正后的说法

当前 Phase 4 把“schema 合法”当成了“可以冻结”。正确顺序应该是：

1. `generate_summary` 生成候选交付物。
2. 运行 schema validation。
3. 运行 quality validation。
4. 低于阈值则修订。
5. 通过后才冻结 deliverables。

### 改进建议

有两种实现方式：

方案 A：拆工具。

- `draft_summary`：生成候选交付物，不冻结。
- soft judge / deterministic rubric 评审 draft。
- `finalize_summary`：通过质量门后冻结。

方案 B：保留工具名，但延迟冻结。

- `generate_summary` 成功后先暂存 draft。
- soft judge 通过后再 `persist_phase4_deliverables(...)`。
- 不通过则把 feedback 塞回下一轮修订。

更推荐方案 A，生命周期更清楚。

### 验收标准

- soft judge 低于阈值时，不会产生 frozen deliverables。
- 最终 assistant 不会在刚说“交付完成”后，message history 里又出现未处理质量问题。
- trace 里能看到 draft、judge、repair、finalize 四个阶段。

## 8. Agent 很依赖用户确认，推进偏保守

### Review

部分成立。

现有 prompt 明确写了：

- 大交通给 2-3 个方案，“不替用户拍板”。
- `red_flags` 里有 `P2-LOCK-1`：用户未确认就锁定交通或住宿是红旗。

所以它不是随机啰嗦，而是当前安全策略的结果。实跑里用户说“直接写入/锁定”时，agent 仍然只写 options、不 select，这说明系统缺少“明确授权下的 auto-lock”分支。

### 修正后的说法

不要把它改成默认自动选择。应该增加显式授权模式：只有用户明确说“直接锁定 / 不用再问 / 按推荐写入”时，agent 才可以从 options 里 select 推荐项。

### 改进建议

- 增加授权识别：
  - `direct_lock_authorized=true`
  - `authorization_quote`
  - `scope=transport | accommodation | both`
- 允许在授权范围内自动调用：
  - `select_transport`
  - `set_accommodation`
- trace 记录：
  - `auto_lock_reason`
  - `selected_option_id`
  - `authorization_quote`
  - `risk_summary`
- 保留默认保守策略：没有授权就继续确认。

### 验收标准

- 用户没授权时不自动锁定。
- 用户明确授权时不再反复询问。
- auto-lock 行为可审计、可回放、可解释。

## 9. 推荐实施顺序

### P0：Run-scoped trace delta

先修第 1 条。否则后续所有 eval 数字都不可信。

### P1：Phase 4 交付物生命周期

先拆出 draft / finalize，或者至少延迟冻结。否则 soft judge 低分后无法自动修。

### P1：高失败 writer tool repair

重点修 `generate_summary` 的 repair hint、最小合法模板、连续失败检测。

### P1：天气不确定性 rubric

这类问题规则清楚，适合 deterministic grader，收益高。

### P1：Phase 3 quality repair loop

先做 `balanced_day_activity_limit`，再做 soft judge action items。

### P2：搜索预算与空结果质量建模

Phase 2 budget 和 `search_travel_services` quality status 都值得做，但最好在 run trace 修准后再量化收益。

### P2：Explicit auto-lock

这是交互体验优化，不是核心正确性问题。做的时候要保持可审计。

## 10. 最小可落地任务列表

1. 给 `SessionStats` 或 session 增加 run offsets。
2. 修改 `persist_trace_run_safely(...)`：只写本轮 delta events 和 delta summary。
3. 加测试：第二个 run 不应包含第一个 run 的 tool event。
4. 给 `generate_summary` 增加 suggestion 和最小合法模板。
5. 加 grader：`repeated_tool_argument_failure`。
6. 把 `generate_summary` 冻结流程改成 draft/finalize 或延迟冻结。
7. 加 grader：`future_weather_not_treated_as_exact`。
8. 加 grader：`balanced_day_activity_limit`。
9. `search_travel_services` 过滤空 item，并返回 empty / low_quality。
10. TraceViewer 标黄 empty success / budget exceeded / low judge score。
