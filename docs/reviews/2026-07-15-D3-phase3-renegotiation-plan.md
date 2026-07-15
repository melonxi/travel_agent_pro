# D3:精排→粗排 hub-and-spoke 再协商 + 共享黑板 施工蓝图(2026-07-15)

> 源自 `docs/reviews/2026-07-14-agent-reliability-action-plan.md` 的 **D3** 与 **P1-6**。
> P1-6 已落地"最小版"(worker 上报通道),本文档规划 D3 **完整版**。
> 配套文档:`2026-07-15-D4-steering-plan.md`(D4 与 D3 配合价值最大)。

## 1. 一句话目标

让 Day Worker 在发现骨架对某天不可行时,能把**结构化**的修正意图上行给 Orchestrator;Orchestrator 有权**只改受影响的单天骨架**并**只重派那一天**,而不是"整批丢弃 / 核爆式回退 Phase 2"。配套一块 Orchestrator 单写、Worker 只读的**共享黑板**,把"防重复认领 POI、跨天预算不超、日边界对齐"从事后全局校验前移为提交时即时拒绝。

## 2. 为什么做(问题回顾)

当前"精排发现骨架不对"只有两个极端出口,中间地带缺失:

- **静默将就**:worker 减活动、写 notes 降级,骨架与实际日程漂移且无记录(`orchestrator.py:441-462` 编译期静默把超限 locked POI 降级为候选,不回写骨架)。
- **核爆回退**:`request_backtrack(to_phase=2)` 把 dates/trip_brief/候选/骨架全清(P1-3 已把 `clear_downstream` 改为选择性清除,但仍是"回退 Phase 2 手工重来"的粗粒度动作)。

"第 3 天和第 4 天区域该对调"这类**天级修正**没有合法的自动通道。D3 补的就是这条纵向再协商回路。

## 3. 现状锚点(施工基线,均已核实 file:line)

### 3.1 Worker 上报通道(P1-6 已落地,结构化程度不足)

- `backend/agent/phase3/day_worker.py:42` — `ERROR_NEEDS_PHASE3_REPLAN = "NEEDS_PHASE3_REPLAN"`。
- `day_worker.py:573-598` — `_REPORT_SKELETON_INFEASIBLE_SCHEMA`,工具名 `report_skeleton_infeasible`。当前参数**仅两个**:
  - `reason`(必填,自由文本)
  - `suggestion`(可选,自由文本,如"把 X 移到第 N 天")
- `day_worker.py:842-843` — 仅当 worker 有该工具时 append 进 worker_tools。
- `day_worker.py:1282-1301` — worker 调用 `report_skeleton_infeasible` → 早返回 `error_code=NEEDS_PHASE3_REPLAN`。

**缺口**:`suggestion` 是给人读的字符串,**不是机器可解析的结构化 payload**。D3 设想的 `SUGGEST_MOVE(poi, to_day)` / `OVERLOADED` / `INFEASIBLE_DAY` 三类结构化消息目前退化成一条 `reason` + 一句自由文本建议。Orchestrator 无法据此自动改骨架。

### 3.2 Orchestrator 收到 replan 后(只提示,不重排)

- `orchestrator.py:1218-1253` — 7b 分支:收集所有 worker 的 `NEEDS_PHASE3_REPLAN`,拼成 `reason` 文本,`yield` 一段 TEXT_DELTA 提示用户"受影响天需调整骨架后重排,其余天先保留交付",然后**继续走部分交付路径**(P0-2/P1-6 已确保不整批丢弃)。
- **没有**任何"修改骨架单天 + 只重派受影响天"的代码。这是 D3 完整版的核心新增。

### 3.3 骨架拆分与写入边界

- `orchestrator.py:430-434` — `_split_tasks()` → `split_skeleton_to_day_tasks(skeleton, plan)`,把选定骨架拆成 `list[DayTask]`。
- `orchestrator.py:436-490` — `_compile_day_tasks()` 富化跨天约束。其中 **`poi_owner: dict[str, int]`(469-490)是"POI 认领登记簿"的编译期雏形**:一次性构建 POI→owner_day 映射,去重跨天锁定(P2-3),并据此派生每天的 `forbidden_pois`。**但它是静态的、一次性的,不是运行时 worker 可查的黑板**。
- 骨架的权威写入通道:`backend/tools/plan_tools/phase2_tools.py` 的 `set_skeleton_plans` / `select_skeleton`(`phases=[2]` 工具,D2 已加 `date_role` 必填与天数校验)。**Orchestrator 当前无权改骨架**——它只读 `self.plan` 里的选定骨架。
- 写入不变量(`docs/agent/deep/phase3-parallel.md`):Worker 只提交候选;Orchestrator 只拆分/派发/收集/验证/handoff;最终写 `daily_plans` 必须由 AgentLoop 用 `replace_all_day_plans` 标准工具走 `_execute_tool_batch → detect_phase_transition`。**D3 不能打破这条边界**——改骨架必须走一条受控的、可追溯的通道,不能让 Orchestrator 直接 `self.plan.skeleton_plans[x] = ...`。

### 3.4 黑板三张表的现有雏形

| 黑板表 | 现有雏形 | 位置 | 差距 |
|---|---|---|---|
| POI 认领登记簿 | `poi_owner` 映射 + `forbidden_pois` 派生 + P2-1 `_validate_locked_pois_present`(`orchestrator.py:655-680`)| `orchestrator.py:469-490`、`:249` `_locked_pois_by_day` | 编译期静态构建 + 事后全局校验;缺"worker 提交时查表即拒" |
| 预算台账 | `day_budget` 下发给 worker;全局校验事后汇总 | DayTask.day_budget | 缺跨天运行时总量登记,worker 各自为战 |
| 日边界锚点 | `arrival_time`/`departure_time`/`date_role` 下发 | DayTask 字段 | 已下发但无"相邻天边界一致性"运行时校验 |

### 3.5 Worker 并发模型

- Worker 通过 `asyncio` 并发起(见 `orchestrator.py:911+` worker_statuses 初始化与后续 gather 式收集)。**当前是一次性全部并发,无波次概念**。有限波次(奇偶天分两波)是纯新增。

## 4. 目标设计

### 4.1 分层与三部分关系

```text
Worker(只读黑板 + 结构化上报)
   │  ReplanRequest(结构化:INFEASIBLE_DAY / OVERLOADED / SUGGEST_MOVE)
   ▼
Orchestrator(hub,单写)
   ├── 黑板(单写三张表):认领登记簿 / 预算台账 / 边界锚点
   ├── 再协商决策:改单天骨架 → 只重派受影响天(bounded 次数)
   └── 骨架改动经受控通道回写 + trace 记录
   ▲
(可选)有限波次:奇偶天两波,第二波拿第一波边界事实作约束
```

三部分**独立可上线**,建议实施顺序:**再协商 > 黑板 > 波次**(再协商价值最高;黑板把事后校验前移;波次是锦上添花)。

### 4.2 Part A:纵向再协商(最高优先级)

**A1. 结构化上报消息**

把 `report_skeleton_infeasible` 从"reason + 自由文本"升级为**判别式结构化消息**。新 schema(day_worker.py:573):

```python
_REPORT_SKELETON_INFEASIBLE_SCHEMA = {
    "name": "report_skeleton_infeasible",
    "parameters": {
        "type": "object",
        "required": ["kind", "reason"],
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["INFEASIBLE_DAY", "OVERLOADED", "SUGGEST_MOVE"],
                "description": "INFEASIBLE_DAY:本天结构性排不下;OVERLOADED:密度超限需减载;SUGGEST_MOVE:建议把某 POI 移到另一天",
            },
            "reason": {"type": "string"},
            # 仅 SUGGEST_MOVE 需要:
            "move_poi": {"type": "string", "description": "建议移动的 POI 名(须是本天 locked/candidate 之一)"},
            "to_day": {"type": "integer", "description": "建议移到的目标天(1-indexed)"},
        },
    },
}
```

Worker 端(day_worker.py:1282-1301)把 `kind`/`move_poi`/`to_day` 一并塞进结构化 error payload(现在只传 error/error_code)。**新增一个 dataclass `ReplanRequest`** 承载,避免用裸 dict。

**A2. Orchestrator 再协商决策器**

在 `orchestrator.py` 新增 `_renegotiate_skeleton(replan_requests: list[ReplanRequest]) -> RenegotiationOutcome`,在 7b 分支(1226 附近)调用。决策规则(确定性,不引入 LLM 协商以保并行确定性):

- `SUGGEST_MOVE(poi, to_day)`:校验 `to_day` 合法且目标天有容量(查预算台账 + `max_core_activities_for_pace`)→ 在**骨架副本**上把 poi 从 source_day 的 locked/candidate 移到 to_day → 标记 `{source_day, to_day}` 为受影响天。
- `OVERLOADED`:把该天超限的 candidate 降级/移除 → 标记该天受影响。
- `INFEASIBLE_DAY`:无自动解 → 回退到当前的"文字提示 + 部分交付"(即现状行为),不阻断其余天。

**A3. 只重派受影响天**

- 熔断:每天最多再协商 **1 次**(`self._renegotiate_count: dict[int, int]`),超限则降级为 INFEASIBLE_DAY 处理,防止 A↔B 反复移动不收敛。
- 只对受影响天重新 `_compile_day_tasks` 子集 + 重跑 worker,已成功的天保留候选。复用现有 step 7 逐天重试骨架(`orchestrator.py:1180-1216` 附近)。

**A4. 骨架改动的受控回写**

再协商在**骨架副本**上操作,run 结束时:
- 若产生 daily_plans → 通过 AgentLoop 的 `replace_all_day_plans` 正常 handoff(不变)。
- 骨架副本的改动**不直接写 `plan.skeleton_plans`**,而是记入 trace + 作为"本次 run 的有效骨架"用于校验;是否持久化回 `skeleton_plans` 由后续 handoff 决策(建议:记一条 `skeleton_amendments` 供 AgentLoop 在 commit 时一并落地,保持单写边界)。**此点需在实施时与 tool-state-writes 边界复核**(见风险 R1)。

### 4.3 Part B:共享黑板(把事后校验前移)

Orchestrator 持有三张**单写**表,worker 通过只读快照查询(worker 无并发写,规避锁):

- **认领登记簿** `poi_registry: dict[str, int]`:把编译期 `poi_owner`(469-490)提升为 run 级实例状态。worker 提交候选时,候选里的 POI 若已被别天认领 → 提交即拒(返回结构化 reject,worker 换 POI)。替代 P2-1 事后 `_validate_locked_pois_present` 的大部分场景。
- **预算台账** `budget_ledger: dict[int, float]`:每天已提交活动成本登记,跨天累计。worker 提交时若累计超 `plan` 总预算(含 P0-6 的交通住宿口径)→ 提交即拒。
- **边界锚点** `day_boundaries: dict[int, tuple[arrival, departure]]`:相邻天边界一致性(如 D 天 departure 与 D+1 天 arrival 不冲突)。

黑板查询是**只读快照**下发(随 DayTask 或 shared_prefix),worker 无写权,保持写入边界。提交时的"查表即拒"在 Orchestrator 收集候选处(候选 store 落盘后、全局校验前)执行。

### 4.4 Part C:有限波次(可选,最后做)

按奇偶天分两波:
- 第一波:奇数天(1,3,5...)并发。
- 第二波:偶数天(2,4,6...),把第一波已确定的 `day_boundaries` 作为额外约束下发。

价值:更紧的跨天衔接(第 2 天知道第 1 天实际几点结束)。代价:并行度腰斩、wall-clock 上升。**仅当再协商 + 黑板上线后仍观测到跨天衔接问题时才做**。文档保留设计,不默认实施。

## 5. 分期施工步骤

### 阶段 1:结构化再协商(A1-A3,不含骨架回写持久化) — ✅ 已完成(2026-07-15)

1. `day_worker.py` 升级 schema 为 `kind` 判别式 + `move_poi`/`to_day`。
2. 新增 `backend/agent/phase3/renegotiation.py`:`ReplanRequest` / `RenegotiationOutcome` / `renegotiate_skeleton`。
3. worker 上报时填充结构化 `replan_request` 挂在 `DayWorkerResult`。
4. `orchestrator.py` `_renegotiate_skeleton()` + `_renegotiate_count` 熔断;7b 分支接入。
5. 受影响天 attempt=4 重派;骨架副本改动记 `_skeleton_amendments` + trace(不持久化权威 skeleton_plans)。

### 阶段 2:共享黑板(B) — ✅ 已完成(2026-07-15)

6. `Phase3Blackboard`：`poi_registry` / `budget_ledger` / `day_boundaries`；seed locked + precommitted 交通住宿。
7. 候选收集 `_accept_worker_dayplan` 查表即拒 → `BLACKBOARD_REJECT`。
8. P2-1 事后 locked POI 校验保留作兜底。

### 阶段 3(可选):有限波次(C) — 未实施(按文档默认不做)

9. `orchestrator.py` worker 调度改两波;第二波注入第一波边界事实。

## 6. 测试策略

- **单元**:`ReplanRequest` 解析、`_renegotiate_skeleton` 三类 kind 的决策(含 SUGGEST_MOVE 目标天满载拒绝)、熔断计数器。
- **注入式回归**(补齐行动计划 §151 提到的缺口):在 `tests/test_orchestrator.py` 注入"某 worker 返回 SUGGEST_MOVE" → 断言只重派受影响天、其余天候选保留、骨架副本正确改动。当前该文件只覆盖 replan 的测试注入路径(`test_run_converts_missing_skeleton_to_dialogue` 是 P2-6 的失配对话化)。
- **黑板**:注入"两天认领同一 POI" → 断言第二次提交被拒、worker 换 POI 后通过。
- **熔断**:注入"A→B→A 循环 move" → 断言第 2 次即熔断降级,不无限重排。
- **不回归**:现有 `NEEDS_PHASE3_REPLAN` 部分交付路径(P1-6)在 INFEASIBLE_DAY 分支下行为不变。
- 每阶段跑 `evals/trace_grader` 对比修复前后 trace。

## 7. 风险与开放问题

- **R1(最关键):骨架回写与单写边界的张力**。D3 要 Orchestrator "改骨架",但写入不变量要求骨架只由 `phases=[2]` 工具写。**折中方案**:再协商只在**骨架副本**上操作用于本 run 重派,改动作为 `skeleton_amendments` 交回 AgentLoop 在 handoff 时决定是否落地 `skeleton_plans`,保持"Orchestrator 不直接写权威状态"。实施前必须读 `docs/agent/deep/tool-state-writes.md` 复核,可能需要新增一个受控的 skeleton amendment 通道。
- **R2:确定性**。再协商决策必须是**纯确定性规则**(不引入 worker 间 LLM 协商),否则破坏并行可复现性。SUGGEST_MOVE 的目标天选择由 worker 建议、Orchestrator 校验,不做多方协商。
- **R3:收敛性**。move 可能引发连锁(A 移到 B → B 超载 → B 又想移)。熔断(每天 1 次再协商)是硬保证;宁可降级为 INFEASIBLE_DAY 部分交付,也不追求完美重排。
- **R4:超时预算**。再协商 + 重派会拉长 run。必须在 P0-3 的 `orchestration_timeout_seconds` 预算内;受影响天重派数量应有上限(如 ≤ 总天数一半)。
- **R5:黑板与现有校验的关系**。黑板"查表即拒"上线后,P2-1/P2-3 的事后校验保留作兜底,不删——防止黑板逻辑有洞时静默放行。

## 8. 与其他项的关系

- **前置**:P1-6(worker 上报通道)、P0-2(部分交付)、P2-3(去重雏形)已完成,是 D3 地基。
- **配合**:D4(steering)让用户在 D3 再协商发生时能看到进度并中途干预,两者配合价值最大(见 D4 文档 §与 D3 的配合)。
- **不推翻**:D3 是在现有 hub-and-spoke 骨架上的**增量**,不引入 peer-to-peer worker 通信(行动计划 D3 节已论证否决)。
