# Phase 5 Orchestrator-Workers 并行架构设计

日期：2026-04-18
关联设计：`docs/superpowers/specs/2026-04-18-phase5-clear-plan-tools-design.md`

---

## 1. 背景

Phase 5 的任务是把已选骨架展开为覆盖全部出行天数的可执行逐日行程。当前实现是单 Agent 串行完成所有天的 expand → assemble → validate → commit，存在三个已确认的问题：

1. **上下文膨胀**：串行处理 N 天时，前序天的 POI 搜索结果、中间推理累积在上下文中，导致 token 总量超出有效注意力范围。（来源：`docs/postmortems/2026-04-18-phase5-promise-only-loop-termination.md`）
2. **"只承诺不动手"**：上下文过长时 LLM 倾向于用自然语言描述行程而不调用 `save_day_plan` 写入，触发 loop termination。
3. **串行延迟**：5 天行程需要 ~5 分钟，用户体验差。

### 关键发现：天间依赖远弱于初始假设

Phase 3 的 skeleton 子阶段已经为每天定义了：主区域、主主题、核心活动、疲劳等级、预算等级、关键取舍。到 Phase 5 时，天间协调问题已被解决：

| 协调问题 | Phase 3 骨架是否已解决 | Phase 5 是否还需跨天协调 |
|----------|----------------------|------------------------|
| 区域分配 | ✅ 每天已指定主区域 | ❌ |
| 主题/体验分配 | ✅ 每天已指定主题和核心活动 | ❌ |
| 体力节奏 | ✅ 每天已标注疲劳等级 | ❌ |
| 预算分配 | ✅ 每天已标注预算等级 | ⚠️ 仅需汇总验证 |
| 锚点/必去项 | ✅ 骨架阶段已锚定 | ❌ |
| 活动不重复 | ⚠️ 骨架是粗粒度 | ⚠️ 需后验去重 |

结论：Phase 5 各天的 expand→assemble→validate→commit 是高度可并行的。

### 前沿技术支撑

本设计参考以下生产级多 Agent 系统的架构模式：

- **Claude Code Coordinator Mode**：Orchestrator 改写 system prompt 为编排模式，Workers 通过 AgentTool 生成，独立上下文窗口
- **Claude Code Fork Sub-Agent**：从相同上下文 fork 多个 agent 时，最大化 API prompt cache hit（相同 prefix = cache HIT）
- **Codex spawn_agent + wait_agent**：Parent session 并行 spawn sub-agents，通过 wait_agent 收集结果
- **Manus Context Engineering**：KV-Cache 命中率是北极星指标；多 Agent 的真正优势是上下文隔离而非角色专业化
- **Anthropic "Building Effective Agents"**：Orchestrator-Workers 模式适用于"任务可自然分解为独立子任务"的场景
- **Google DeepMind "Scaling Agent Systems"**：拓扑比数量重要，性能取决于 Agent 数量 × 协调拓扑 × 模型能力 × 任务属性

---

## 2. 目标

1. 将 Phase 5 的逐日行程生成从串行改为并行，延迟从 O(N) 降到 O(1)。
2. 通过上下文隔离，彻底解决 token 膨胀和"只承诺不动手"问题。
3. 利用共享 prefix 的 KV-Cache 优化，控制并行化带来的成本增量。
4. 保留串行回退路径，确保单天失败不阻塞整体。
5. 建立通用的 Orchestrator-Worker 框架，为未来其他阶段的并行化铺路。

非目标：

1. 不改变 Phase 5 的工具集（`optimize_day_route`、`save_day_plan`、`replace_all_day_plans` 保持不变）。
2. 不改变 `DayPlan` / `Activity` 数据模型。
3. 不改变 Phase 3 骨架生成逻辑。
4. 不改变前端交互模式（用户仍然看到逐天出现的行程卡片）。
5. 不改变 Phase 1/3/7 的执行模式。

---

## 3. 架构

### 3.1 整体拓扑

```
┌─────────────────────────────────────────────────┐
│              Orchestrator (Python)                │
│  不是 LLM Agent，是纯代码调度器                     │
│                                                   │
│  1. 从 plan 中读取骨架 → 切分为 N 个天级任务        │
│  2. 为每个任务构建独立的 system prompt + messages    │
│  3. 并行 spawn N 个 Day Worker（LLM Agent）        │
│  4. 收集所有 Worker 结果                            │
│  5. 全局验证（预算、去重、首尾衔接）                  │
│  6. 批量写入 daily_plans                           │
│  7. 如有问题，针对性 re-spawn 单天 Worker            │
└───┬───────┬───────┬───────┬───────┬──────────────┘
    │       │       │       │       │
    ▼       ▼       ▼       ▼       ▼
┌──────┐┌──────┐┌──────┐┌──────┐┌──────┐
│Day 1 ││Day 2 ││Day 3 ││Day 4 ││Day 5 │
│Worker││Worker││Worker││Worker││Worker│
└──────┘└──────┘└──────┘└──────┘└──────┘
```

### 3.2 为什么 Orchestrator 不是 LLM Agent

Orchestrator 的职责是纯确定性的——切分骨架、构建 prompt、并发调用、结果合并、写入状态。这些操作不需要 LLM 推理，用 Python 代码实现更可靠、更可测试、延迟更低。

这与 Manus 的设计哲学一致：在确定性操作上不要浪费 LLM 调用。

### 3.3 为什么 Worker 是 LLM Agent

每天内部的 assemble 需要常识推理：哪些 POI 适合上午/下午、餐饮如何穿插、交通衔接是否合理、时间缓冲如何分配。这些决策需要 LLM 能力，纯代码很难覆盖所有场景。

---

## 4. Worker 设计

### 4.1 Worker 输入（只读上下文）

每个 Day Worker 接收的 system prompt 由两部分组成：

**共享 prefix（所有 Worker 相同，最大化 KV-Cache 命中）**：

```
[soul.md 内容]
---
## 当前时间
...
---
## 工具使用硬规则
...
---
## 角色
你是单日行程落地规划师。你的任务是为指定的一天生成完整的 DayPlan。
---
## 旅行上下文
- 目的地：{destination}
- 日期范围：{dates.start} 至 {dates.end}（{total_days} 天）
- 出行人数：{travelers}
- 旅行画像：{trip_brief（完整内容）}
- 用户偏好：{preferences}
- 用户约束：{constraints}
- 住宿：{accommodation（区域/酒店/坐标）}
---
## DayPlan 严格 JSON 结构
{与当前 PHASE5_PROMPT 中的结构定义相同}
---
## 输出要求
完成本天的行程规划后，直接以 JSON 格式输出 DayPlan，不需要调用 save_day_plan。
```

**天级后缀（每个 Worker 不同）**：

```
---
## 你的任务：第 {day} 天（{date}）

骨架安排：
- 主区域：{skeleton.day[i].area}
- 主题：{skeleton.day[i].theme}
- 核心活动：{skeleton.day[i].core_activities}
- 疲劳等级：{skeleton.day[i].fatigue}
- 预算等级：{skeleton.day[i].budget_level}

节奏要求：{pace} → 本天 {activity_count_range} 个核心活动

请为这一天生成完整的 DayPlan JSON。
```

### 4.2 Worker 工具集

Worker 拥有精简的只读工具集：

| 工具 | 用途 | side_effect |
|------|------|-------------|
| `get_poi_info` | 补齐 POI 坐标、票价、开放时间 | read |
| `optimize_day_route` | 单日 POI 路线排序 | read |
| `calculate_route` | 验证两点间交通可行性 | read |
| `check_availability` | 验证景点在指定日期是否可行 | read |
| `xiaohongshu_search_notes` | 搜索真实体验 | read |
| `xiaohongshu_read_note` | 读取笔记正文 | read |

Worker **不拥有**写工具（`save_day_plan`、`replace_all_day_plans`）。写入由 Orchestrator 统一完成。

### 4.3 Worker 输出

Worker 的最终输出是结构化的 DayPlan JSON：

```json
{
  "day": 3,
  "date": "2026-05-03",
  "notes": "浅草-上野文化区，节奏适中",
  "activities": [
    {
      "name": "浅草寺",
      "location": {"name": "浅草寺", "lat": 35.7148, "lng": 139.7967},
      "start_time": "09:00",
      "end_time": "10:30",
      "category": "shrine",
      "cost": 0,
      "transport_from_prev": "从酒店乘地铁银座线",
      "transport_duration_min": 20,
      "notes": "建议早到避开人流"
    }
  ]
}
```

Orchestrator 从 Worker 的最后一条 assistant message 中提取此 JSON。

### 4.4 Worker 执行约束

- **最大迭代轮次**：5 轮（单天任务不应需要更多）
- **最大 token**：4096 output tokens
- **超时**：60 秒
- **失败处理**：Worker 失败时标记为 failed，Orchestrator 可选择重试或串行回退

---

## 5. Orchestrator 设计

### 5.1 执行流程

```python
async def run_phase5_parallel(plan: TravelPlanState) -> None:
    # 1. 预检
    skeleton = find_selected_skeleton(plan)
    if not skeleton:
        raise Phase5PreconditionError("未找到已选骨架")

    # 2. 切分天级任务
    day_tasks = split_skeleton_to_day_tasks(skeleton, plan)

    # 3. 构建共享 prefix
    shared_prefix = build_shared_prefix(plan)

    # 4. 并行 spawn Workers
    results = await asyncio.gather(
        *[run_day_worker(shared_prefix, task) for task in day_tasks],
        return_exceptions=True,
    )

    # 5. 收集和解析结果
    day_plans, failures = parse_worker_results(results, day_tasks)

    # 6. 全局验证
    issues = global_validate(day_plans, plan)

    # 7. 处理失败和问题
    if failures:
        day_plans, still_failed = await retry_failed_days(
            shared_prefix, failures, plan
        )
    if issues:
        day_plans = await fix_global_issues(day_plans, issues, plan)

    # 8. 批量写入
    write_all_day_plans(day_plans, plan)
```

### 5.2 骨架切分

```python
def split_skeleton_to_day_tasks(
    skeleton: dict, plan: TravelPlanState
) -> list[DayTask]:
    days = skeleton.get("days", [])
    tasks = []
    for i, day_skeleton in enumerate(days):
        day_num = i + 1
        date = compute_date(plan.dates.start, i)
        tasks.append(DayTask(
            day=day_num,
            date=date,
            skeleton_slice=day_skeleton,
            pace=plan.trip_brief.get("pace", "balanced"),
        ))
    return tasks
```

### 5.3 全局验证

Orchestrator 在所有 Worker 完成后执行纯 Python 验证：

1. **预算检查**：`sum(act.cost for day in day_plans for act in day.activities)` ≤ `plan.budget.total`
2. **POI 去重**：检查是否有相同 POI 出现在多天；如有，标记冲突天
3. **首日衔接**：第 1 天第一个活动的交通是否从机场/车站出发（如果有 `selected_transport`）
4. **尾日衔接**：最后一天是否留出了返程交通时间
5. **时间冲突**：复用现有 `validate_day_conflicts` 逻辑

### 5.4 结果写入

Orchestrator 收集验证通过的 DayPlans 后，有两种写入策略：

- **增量写入**（默认）：每完成一天就 `save_day_plan(mode="create")`，前端即时展示
- **批量写入**（全部完成后）：`replace_all_day_plans(days=[...])`

推荐增量写入——虽然 Workers 是并行执行的，但 Orchestrator 可以按 day 顺序逐个写入，给用户"依次出现"的体验。

### 5.5 流式进度

Orchestrator 通过现有的 `IterationProgress` 和前端 SSE 通道向用户推送进度：

```
[Orchestrator] 正在并行规划第 1-5 天的详细行程...
[Day 2 完成] 浅草-上野文化区：浅草寺 → 仲见世商店街 → 上野公园 → 阿美横丁
[Day 4 完成] 新宿-涩谷购物区：明治神宫 → 竹下通 → 涩谷 Sky → 涩谷十字路口
[Day 1 完成] 到达日 + 新宿周边：新宿御苑 → 歌舞伎町 → 思出横丁
...
[全部完成] 5 天行程已生成，正在做最终验证...
```

### 5.6 用户交互兼容

并行 Workers 完成初始生成后，后续的用户修改请求（"第 3 天不想去博物馆"）走回现有的单 Agent 流程——只需对单天做 `save_day_plan(mode="replace_existing")`，不需要再次启动 Orchestrator。

也就是说，Orchestrator-Workers 只负责 **Phase 5 的初始全量生成**，后续的增量修改仍由原单 Agent 处理。

---

## 6. KV-Cache 优化策略

参考 Manus 和 Claude Code 的实践，本设计在以下层面优化 KV-Cache 命中率：

### 6.1 共享 prefix 最大化

所有 Worker 的 system prompt 前 80% 内容完全相同（soul + 规则 + 旅行上下文 + DayPlan 结构），只有最后 ~200 token 的天级任务不同。

假设共享 prefix 为 ~3000 token，天级后缀为 ~200 token：
- 第 1 个 Worker：3200 token 全部 uncached
- 第 2-N 个 Worker：3000 token cached + 200 token uncached
- Cache 命中率：3000/3200 ≈ 93.75%

按 Claude 的定价（cached token 成本为 uncached 的 10%），5 天行程的总成本约为：
- 串行：5 × 3200 = 16000 uncached tokens（但串行时每次上下文更长，实际更多）
- 并行：3200 + 4 × (3000 × 0.1 + 200) = 3200 + 4 × 500 = 5200 等效 tokens

**并行模式的 LLM 输入成本可能低于串行模式。**

### 6.2 工具定义稳定

参考 Manus 的 logit masking 策略：所有 Worker 的工具定义列表完全相同（即使某些工具在特定天不太会用到），避免工具定义变化导致 cache miss。

### 6.3 Append-only 上下文

每个 Worker 的消息历史是 append-only 的（system → user task → assistant → tool results → assistant），不在中间插入或删除消息。

---

## 7. 错误处理与回退

### 7.1 Worker 级别失败

| 失败类型 | 处理 |
|----------|------|
| LLM 调用超时 | 重试 1 次，仍失败则标记 |
| LLM 返回非法 JSON | 尝试 JSON 修复（正则提取），失败则重试 |
| Worker 耗尽迭代次数 | 标记为 failed |
| Rate limit | 对该 Worker 做指数退避重试 |

### 7.2 全局级别失败

| 失败类型 | 处理 |
|----------|------|
| 预算超标 | 标记超标天，re-spawn Worker 附加预算约束 |
| POI 重复 | 标记后序重复天，re-spawn 附加排除列表 |
| 部分天失败 | 已成功天先写入，失败天串行回退（用原单 Agent 模式补） |
| 全部天失败 | 完全回退到串行模式 |

### 7.3 串行回退路径

如果并行模式失败率过高（>50% 的天失败），自动降级为原有串行模式。这确保了功能兜底。

```python
if len(failures) > len(day_tasks) / 2:
    logger.warning("并行模式失败率过高，回退到串行模式")
    return await run_phase5_serial(plan)  # 原有逻辑
```

---

## 8. 并发控制

### 8.1 LLM API Rate Limit

- 使用 `asyncio.Semaphore` 控制并发 Worker 数量，默认上限 5
- 支持在 `config.yaml` 中配置 `phase5.max_parallel_workers`
- 如果行程天数 > 并发上限，分批执行（如 7 天行程分 5+2 两批）

### 8.2 工具并发

Worker 内部的工具调用复用现有的 `parallel_tool_execution=True` 机制——读工具并行，写工具串行（但 Worker 没有写工具，所以全部并行）。

不同 Worker 之间的工具调用天然隔离，不存在竞争。

### 8.3 状态写入串行

Orchestrator 的最终写入阶段是串行的——按 day 顺序依次调用 `save_day_plan`，确保 `plan.daily_plans` 的顺序正确。

---

## 9. 新增/修改文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/agent/orchestrator.py` | 新增 | Orchestrator 核心逻辑 |
| `backend/agent/day_worker.py` | 新增 | Day Worker 的 prompt 构建和执行逻辑 |
| `backend/agent/worker_prompt.py` | 新增 | Worker system prompt 模板 |
| `backend/agent/loop.py` | 修改 | Phase 5 入口分流：并行 or 串行 |
| `backend/context/manager.py` | 修改 | 新增 `build_worker_context` 方法 |
| `backend/phase/router.py` | 修改 | Phase 5 路由增加并行模式判断 |
| `config.yaml` | 修改 | 新增 `phase5.parallel` 配置段 |
| `backend/tools/plan_tools/daily_plans.py` | 不变 | Worker 不拥有写工具，Orchestrator 直接调用 Python 函数 |

---

## 10. 配置

```yaml
phase5:
  parallel:
    enabled: true
    max_workers: 5          # 最大并发 Worker 数
    worker_max_iterations: 5  # 单 Worker 最大迭代轮次
    worker_timeout_seconds: 60
    fallback_to_serial: true  # 失败时是否回退到串行
```

---

## 11. 可观测性

### 11.1 Trace 结构

```
agent_loop.run (Phase 5)
  └── orchestrator.run
       ├── orchestrator.split_tasks
       ├── orchestrator.spawn_workers
       │    ├── day_worker.run [day=1]
       │    │    ├── llm.call
       │    │    ├── tool.get_poi_info
       │    │    ├── tool.optimize_day_route
       │    │    └── llm.call
       │    ├── day_worker.run [day=2]
       │    │    └── ...
       │    └── day_worker.run [day=N]
       │         └── ...
       ├── orchestrator.global_validate
       ├── orchestrator.retry_failures (if any)
       └── orchestrator.write_results
```

### 11.2 关键指标

- `phase5.parallel.total_duration_ms`：并行模式总耗时
- `phase5.parallel.worker_duration_ms`：各 Worker 耗时（含 p50/p95）
- `phase5.parallel.cache_hit_rate`：推算的 KV-Cache 命中率
- `phase5.parallel.failure_count`：Worker 失败数
- `phase5.parallel.retry_count`：重试次数
- `phase5.parallel.fallback_to_serial`：是否触发串行回退

---

## 12. 测试策略

### 12.1 单元测试

- `test_split_skeleton_to_day_tasks`：骨架切分逻辑，覆盖 1/3/5/7 天
- `test_build_shared_prefix`：共享 prefix 构建，验证 KV-Cache 友好
- `test_parse_worker_output`：Worker 输出解析，覆盖合法 JSON / 非法 JSON / 空输出
- `test_global_validate`：全局验证逻辑，覆盖预算超标 / POI 重复 / 首尾衔接

### 12.2 集成测试

- `test_parallel_phase5_happy_path`：Mock LLM，验证 5 天并行生成端到端流程
- `test_parallel_phase5_partial_failure`：Mock 1 个 Worker 失败，验证重试和部分写入
- `test_parallel_phase5_full_fallback`：Mock 全部 Worker 失败，验证串行回退
- `test_parallel_vs_serial_equivalence`：对比并行和串行的输出结构是否一致

### 12.3 Eval 验证

在现有的 harness eval 框架中新增 Phase 5 并行模式场景，验证：
- 生成的 daily_plans 覆盖全部天数
- 每天的活动数量符合节奏要求
- 无严重时间冲突
- 预算在合理范围内

---

## 13. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| LLM API rate limit 导致并发失败 | 中 | 中 | Semaphore 控制 + 指数退避 + 可配置并发数 |
| Worker 输出质量不如串行（上下文更少） | 低 | 中 | Worker 拥有完整的旅行上下文和工具集；单天任务更简单 |
| 不同 Worker 选择重复 POI | 中 | 低 | Orchestrator 后验去重 + re-spawn |
| 并行化框架引入新 bug | 中 | 高 | 串行回退兜底 + 充分测试 + 可配置开关 |
| 首日/尾日与大交通衔接不佳 | 低 | 中 | Orchestrator 全局验证 + 首尾日特殊 prompt hint |

---

## 14. 未来扩展

本设计建立的 Orchestrator-Worker 框架可复用于：

1. **Phase 7 并行化**：出行准备清单的各类别（证件、天气、保险、通讯）可并行生成
2. **Phase 3 candidate 子阶段**：多个搜索方向可并行 fan-out
3. **跨阶段 backtrack 后的增量重生成**：只重新生成受影响的天数

---

## 15. 被拒绝的方案

### 15.1 方案 B：单 Agent + 并行厚工具

将 expand_day 实现为"厚工具"，内部封装 POI 搜索和路线优化。LLM 一次调用并行发出 N 个 expand_day tool call。

被拒绝原因：
- 厚工具内部如果需要 LLM 推理，就是隐藏的 sub-agent，不如显式多 Agent 透明
- 所有工具结果回到同一上下文，上下文膨胀问题未解决
- 工具并行受限于 LLM 原生 parallel tool calling 的实现质量

### 15.2 方案 C：串行 + 上下文压缩

保持串行，每完成 1 天后压缩上下文。

被拒绝原因：
- 延迟仍然是 O(N)
- 压缩可能丢失关键信息
- 没有利用天间可并行的结构性机会
- 治标不治本
