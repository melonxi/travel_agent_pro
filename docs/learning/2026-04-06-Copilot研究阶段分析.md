# Travel Agent Pro — Phase 更新机制深度解析

## 执行摘要

Travel Agent Pro 使用一套**状态驱动的阶段推进机制**。整个旅行规划被切分为 6 个阶段（1-5 + 7），每个阶段代表规划的一个里程碑（需求探索 → 目的地确认 → 日期确认 → 住宿确认 → 行程组装 → 出发清单）。Phase 的切换不由用户手工触发，而是由 `PhaseRouter` 检查 `TravelPlanState` 的字段完整度来**自动推断**，并在工具执行完毕后的 Hook 中触发。系统同时支持用户主动**回退（Backtrack）**到更早阶段，回退时会快照保存当前状态并清空下游字段。

---

## 一、阶段定义

| Phase | 角色 | 触发条件（进入此阶段的状态） | Control Mode |
|-------|------|--------------------------|-------------|
| 1 | 旅行灵感顾问 | 无目的地、无偏好 | `conversational` |
| 2 | 目的地推荐专家 | 无目的地，但 `preferences` 非空 | `agent_with_guard` |
| 3 | 行程节奏规划师 | `destination` 已填，`dates` 为空 | `workflow` |
| 4 | 住宿区域顾问 | `dates` 已填，`accommodation` 为空 | `conversational` |
| 5 | 行程组装引擎 | `accommodation` 已填，`daily_plans` 未满 | `structured` |
| 7 | 出发前查漏清单 | `daily_plans` 数量 ≥ `dates.total_days` | `evaluator` |

> **注意**：没有 Phase 6，Phase 7 直接是最终阶段。

---

## 二、核心组件关系

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           POST /api/chat/{session_id}                     │
└──────────────────────────────────────────────────────────────────────────┘
         │
         │ 1. apply_trip_facts() 快速提取消息中的旅行信息
         ▼
┌──────────────────────┐
│  state/intake.py      │   extract_trip_facts() → apply_trip_facts()
│  (NLP 规则提取)        │   从用户消息中直接抓取目的地/日期/预算
└──────────────────────┘
         │ 2. 如有更新，立即尝试 phase 推进
         ▼
┌──────────────────────┐
│  phase/router.py      │   check_and_apply_transition(plan)
│  PhaseRouter          │   infer_phase(plan) → 若新 phase ≠ 当前 → 更新
└──────────────────────┘
         │ 3. 根据当前 phase 生成 system prompt
         ▼
┌──────────────────────┐
│  phase/prompts.py     │   PHASE_PROMPTS[phase] + PHASE_CONTROL_MODE[phase]
└──────────────────────┘
         │ 4. 进入 agent loop
         ▼
┌──────────────────────┐      工具列表由 phase 过滤
│  agent/loop.py        │ ──▶  ToolEngine.get_tools_for_phase(phase)
│  AgentLoop.run()      │
└──────────────────────┘
         │ 5. LLM 决定调用 update_plan_state
         ▼
┌──────────────────────┐
│  tools/update_plan    │   field + value → 更新 TravelPlanState 字段
│  _state.py            │   field="backtrack" → BacktrackService.execute()
└──────────────────────┘
         │ 6. 工具执行完毕，触发 after_tool_call Hook
         ▼
┌──────────────────────┐
│  agent/hooks.py       │   on_tool_call() 回调
│  HookManager          │   → phase_router.check_and_apply_transition(plan)
└──────────────────────┘
         │ 7. agent.run() 完毕后的 fallback 检测
         ▼
┌──────────────────────┐
│  main.py              │   _detect_backtrack(message, plan)
│  keyword fallback     │   关键词触发 prepare_backtrack()
└──────────────────────┘
         │ 8. 持久化
         ▼
┌──────────────────────┐
│  state/manager.py     │   state_mgr.save(plan) → plan.json
│  StateManager         │   version++ , last_updated 更新
└──────────────────────┘
```

---

## 三、Phase 推进详细流程

### 3.1 infer_phase() — 状态推断引擎

**文件**：`backend/phase/router.py:16-27`

```python
def infer_phase(self, plan: TravelPlanState) -> int:
    if not plan.destination:        # destination 为 None
        if plan.preferences:        # 但有用户偏好
            return 2                # → Phase 2 目的地推荐
        return 1                    # → Phase 1 灵感探索
    if not plan.dates:              # 有目的地，但无日期
        return 3
    if not plan.accommodation:      # 有日期，但无住宿
        return 4
    if len(plan.daily_plans) < plan.dates.total_days:  # 日程不够
        return 5
    return 7                        # 全部完成 → 出发清单
```

**参数说明**：
- `plan: TravelPlanState` — 当前会话的完整规划状态对象
- `plan.destination: str | None` — 目的地名称，为 `None` 时表示未确定
- `plan.preferences: list[Preference]` — 用户偏好列表（空列表为 falsy）
- `plan.dates: DateRange | None` — 出行日期范围
- `plan.accommodation: Accommodation | None` — 住宿信息
- `plan.daily_plans: list[DayPlan]` — 已生成的每日行程列表
- `plan.dates.total_days` — 计算属性：`(end - start).days`（天数）

> **关键逻辑**：`infer_phase` 是一个纯函数，它只读取状态，不修改任何字段。它的唯一职责是根据哪些字段"已填充"来决定当前应该处于哪个阶段。

---

### 3.2 check_and_apply_transition() — 阶段切换执行

**文件**：`backend/phase/router.py:35-57`

```python
def check_and_apply_transition(self, plan: TravelPlanState) -> bool:
    inferred = self.infer_phase(plan)       # 计算应该处于哪个阶段
    if inferred != plan.phase:              # 与当前阶段不同
        tracer = trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("phase.transition") as span:
            span.set_attribute(PHASE_FROM, plan.phase)   # 记录来源阶段
            span.set_attribute(PHASE_TO, inferred)       # 记录目标阶段
            span.add_event(
                EVENT_PHASE_PLAN_SNAPSHOT,
                {
                    "destination": plan.destination or "",
                    "dates": f"{plan.dates.start} ~ {plan.dates.end}" if plan.dates else "",
                    "daily_plans_count": len(plan.daily_plans),
                },
            )
            plan.phase = inferred           # 直接修改 plan.phase
        return True
    return False
```

**参数说明**：
- `plan: TravelPlanState` — 可变状态对象（in-place 修改 `plan.phase`）
- 返回值 `bool` — `True` 表示发生了阶段切换，`False` 表示阶段未变
- `PHASE_FROM / PHASE_TO` — OpenTelemetry span 属性，用于可观测性追踪
- `EVENT_PHASE_PLAN_SNAPSHOT` — span 事件名，记录切换时的规划快照（目的地、日期、日程数）

> **关键逻辑**：这个方法被调用 **3 次**（见第四节），确保任何时机的状态更新都能触发阶段推进。

---

### 3.3 完整 Phase 推进流程图

```
用户发送消息 POST /api/chat/{session_id}
│
├─→ [A] apply_trip_facts(plan, message)
│       │ 用正则从消息中提取旅行事实（目的地/日期/预算）
│       │ 直接调用 setattr(plan, field, value) 修改状态
│       └─→ 若 updated_fields 非空
│               └─→ [B] phase_router.check_and_apply_transition(plan)
│                       快速推进（消息中已含足够信息时，不需等 LLM）
│
├─→ build_system_message(plan, phase_prompt, user_summary)
│       phase_prompt = PHASE_PROMPTS[plan.phase]
│       （使用当前最新 phase 生成对应系统提示词）
│
├─→ agent.run(messages, phase=plan.phase)
│       │
│       │   LLM 流式输出 + 工具调用循环
│       │
│       ├─→ [每次工具调用] ToolEngine.execute(tool_call)
│       │       │
│       │       └─→ update_plan_state(field, value)
│       │               │
│       │               ├─→ field != "backtrack"
│       │               │       plan.destination = value  (or dates, etc.)
│       │               │       return {"updated_field": ..., "new_value": ...}
│       │               │
│       │               └─→ field == "backtrack"
│       │                       BacktrackService.execute(plan, to_phase, reason, "")
│       │                       return {"backtracked": True, ...}
│       │
│       └─→ [C] after_tool_call Hook: on_tool_call()
│               │
│               ├─→ result.data.get("backtracked") == True
│               │       session["needs_rebuild"] = True  （标记重建 agent）
│               │       return  （不推进 phase，因为是回退）
│               │
│               └─→ 否则
│                       phase_router.check_and_apply_transition(plan)
│                       （工具执行后再次检查是否需要推进）
│
└─→ agent.run() 完毕后
        │
        ├─→ [D] 若 plan.phase == phase_before_run（本轮未发生回退）
        │       _detect_backtrack(message, plan)
        │       若检测到回退关键词
        │           save_snapshot() → prepare_backtrack() → needs_rebuild=True
        │
        └─→ state_mgr.save(plan)   持久化（版本号+1）
```

---

## 四、三处 check_and_apply_transition 调用时机

| 触发点 | 代码位置 | 触发条件 | 说明 |
|--------|----------|----------|------|
| **A - NLP 快提取后** | `main.py:285` | `apply_trip_facts()` 有结果 | 消息中直接含旅行信息，无需等 LLM 响应即可推进 |
| **B - 工具执行后 Hook** | `main.py:111`（`on_tool_call`） | `update_plan_state` 成功且非 backtrack | LLM 调用工具更新字段后，立即重新推断阶段 |
| *(隐式) 下一轮 chat* | `main.py:288` | 每次 `/api/chat` 都调用 `get_prompt(plan.phase)` | phase 已是最新，prompt 自然更新 |

---

## 五、Backtrack（回退）机制

### 5.1 触发路径

系统支持三种回退触发方式：

```
方式1: REST API 直接回退
  POST /api/backtrack/{session_id}
  Body: {"to_phase": 3, "reason": "用户想换目的地"}
  │
  ├─→ save_snapshot(plan)  保存快照
  ├─→ phase_router.prepare_backtrack(plan, to_phase, reason, snapshot_path)
  │       └─→ BacktrackService.execute(plan, to_phase, reason, snapshot_path)
  └─→ session["agent"] = _build_agent(plan)  重建 agent

方式2: LLM 工具调用触发
  update_plan_state(field="backtrack", value={"to_phase": 2, "reason": "..."})
  │
  ├─→ BacktrackService.execute(plan, to_phase, reason, snapshot_path="")
  └─→ on_tool_call Hook → needs_rebuild = True
                         （下一次 /api/chat 时重建 agent）

方式3: 关键词 Fallback（main.py:325-335）
  agent.run() 完成后，若本轮未发生 phase 变化
  _detect_backtrack(message, plan) 检测关键词
  匹配到 → prepare_backtrack() + needs_rebuild=True
```

### 5.2 BacktrackService.execute() 详解

**文件**：`backend/phase/backtrack.py:8-28`

```python
def execute(
    self,
    plan: TravelPlanState,   # 当前规划状态（in-place 修改）
    to_phase: int,           # 目标阶段（必须 < plan.phase）
    reason: str,             # 回退原因（记录到历史）
    snapshot_path: str,      # 快照文件路径（可为空字符串）
) -> None:
    if to_phase >= plan.phase:
        raise ValueError("只能回退到更早的阶段")

    # 1. 记录回退历史
    plan.backtrack_history.append(
        BacktrackEvent(
            from_phase=plan.phase,
            to_phase=to_phase,
            reason=reason,
            snapshot_path=snapshot_path,
        )
    )

    # 2. 清空下游字段
    plan.clear_downstream(from_phase=to_phase)

    # 3. 更新 phase
    plan.phase = to_phase
```

**参数说明**：
- `to_phase >= plan.phase` 会抛出 `ValueError` — 系统强制"只能往前退，不能往后退"（回退到同级或更高阶段是非法的）
- `BacktrackEvent` — 包含 `from_phase`, `to_phase`, `reason`, `snapshot_path`, `timestamp`（ISO 格式），持久化到 `plan.backtrack_history`
- `clear_downstream(from_phase=to_phase)` — 核心操作，详见下节

### 5.3 clear_downstream() — 字段清空逻辑

**文件**：`backend/state/models.py:209-248`

```python
_PHASE_DOWNSTREAM: dict[int, list[str]] = {
    1: ["destination", "destination_candidates", "dates", "accommodation", "daily_plans"],
    2: ["destination", "dates", "accommodation", "daily_plans"],
    3: ["dates", "accommodation", "daily_plans"],
    4: ["accommodation", "daily_plans"],
    5: ["daily_plans"],
}

def clear_downstream(self, from_phase: int) -> None:
    for phase in sorted(_PHASE_DOWNSTREAM):    # 按升序遍历 1,2,3,4,5
        if phase >= from_phase:                # 只清空目标阶段及之后产生的字段
            for attr in _PHASE_DOWNSTREAM[phase]:
                default = [] if isinstance(getattr(self, attr), list) else None
                setattr(self, attr, default)   # 列表→[]，对象→None
```

**示例**：回退到 Phase 3（重新确认日期）

```
clear_downstream(from_phase=3) 执行：
  phase=3: 清空 dates, accommodation, daily_plans
  phase=4: 清空 accommodation, daily_plans（重复，无害）
  phase=5: 清空 daily_plans（重复，无害）

结果：destination 保留，dates/accommodation/daily_plans 全部重置
```

**注意**：`preferences` 和 `constraints` 永远不在 `_PHASE_DOWNSTREAM` 中，因此**回退不会丢失用户偏好**。

### 5.4 Backtrack 完整流程图

```
触发回退（三种方式之一）
│
├─→ 验证 to_phase < plan.phase（否则报错）
│
├─→ save_snapshot(plan)
│       data_dir/sessions/{session_id}/snapshots/{timestamp_ns}.json
│       保存当前完整状态快照（用于回滚恢复）
│
├─→ BacktrackService.execute(plan, to_phase, reason, snapshot_path)
│       ├─→ plan.backtrack_history.append(BacktrackEvent(...))
│       ├─→ plan.clear_downstream(from_phase=to_phase)
│       │       清空目标阶段及之后产生的所有字段
│       │       保留：session_id, preferences, constraints, backtrack_history
│       └─→ plan.phase = to_phase
│
├─→ 重建 agent（_build_agent(plan)）
│       因为 update_plan_state 工具是 plan-bound 闭包
│       plan 已变，agent 内的工具引用必须刷新
│
└─→ state_mgr.save(plan)  持久化
```

---

## 六、Phase Prompt 与 Control Mode

每个 Phase 对应一个**系统提示词**和一个**控制模式**，它们在每次 `/api/chat` 请求时通过 `context_mgr.build_system_message()` 注入到对话上下文中。

**文件**：`backend/phase/prompts.py`

| Phase | 系统提示词角色 | Control Mode | 含义 |
|-------|-------------|-------------|------|
| 1 | 旅行灵感顾问 | `conversational` | 自由对话，探索用户模糊需求 |
| 2 | 目的地推荐专家 | `agent_with_guard` | 允许调用工具但有约束（不替用户做决定） |
| 3 | 行程节奏规划师 | `workflow` | 按步骤输出结构化约束清单 |
| 4 | 住宿区域顾问 | `conversational` | 类似 Phase 1，开放式建议 |
| 5 | 行程组装引擎 | `structured` | 强制结构化输出（时间/地点/费用） |
| 7 | 出发前查漏清单 | `evaluator` | 评估器模式，检查遗漏项 |

**get_prompt() 方法**（`router.py:29-30`）：

```python
def get_prompt(self, phase: int) -> str:
    return PHASE_PROMPTS.get(phase, PHASE_PROMPTS[1])
    # 若 phase 不存在于字典中（理论上不会），默认返回 Phase 1 的提示词
```

**get_control_mode() 方法**（`router.py:32-33`）：

```python
def get_control_mode(self, phase: int) -> str:
    return PHASE_CONTROL_MODE.get(phase, "conversational")
    # 默认降级为 conversational
```

---

## 七、ToolEngine 与 Phase 的关系

**文件**：`backend/tools/engine.py:21-22`

```python
def get_tools_for_phase(self, phase: int) -> list[dict[str, Any]]:
    return [t.to_schema() for t in self._tools.values() if phase in t.phases]
```

每个工具在注册时声明自己适用的 phases：

```python
@tool(
    name="update_plan_state",
    phases=[1, 2, 3, 4, 5, 7],   # 所有阶段都可用
    ...
)

@tool(
    name="assemble_day_plan",
    phases=[5],                    # 只在 Phase 5（行程组装）阶段可用
    ...
)
```

`AgentLoop.run()` 中：

```python
tools = tools_override or self.tool_engine.get_tools_for_phase(phase)
```

这保证了不同阶段的 LLM 看到不同的工具集，避免 LLM 在早期阶段误用行程组装工具。

---

## 八、NLP 快速提取（apply_trip_facts）

**文件**：`backend/state/intake.py:101-110`

```python
def apply_trip_facts(
    plan: TravelPlanState,
    message: str,
    *,
    today: date | None = None,
) -> set[str]:
    updates = extract_trip_facts(message, today=today)  # 正则提取
    for field, value in updates.items():
        setattr(plan, field, value)                     # 直接写入 plan
    return set(updates)                                  # 返回已更新的字段集合
```

这是一个**轻量级的 NLP 预处理步骤**，在 LLM 介入之前就能更新状态，主要处理以下场景：

- **目的地提取**：`"我想去巴黎"` → `plan.destination = "巴黎"`（正则匹配 `去|到|飞往|前往` + 中文/英文地名）
- **日期提取**：`"五一去玩5天"` → `plan.dates = DateRange("2026-05-01", "2026-05-06")`（支持节假日关键词 + 天数）
- **预算提取**：`"预算2万元"` → `plan.budget = Budget(total=20000, currency="CNY")`（支持万/千/k单位）

---

## 九、状态持久化

**文件**：`backend/state/manager.py`

```
data_dir/
└── sessions/
    └── {session_id}/
        ├── plan.json                    # 主状态文件（每次 save() 覆盖）
        ├── snapshots/
        │   └── {timestamp_ns}.json      # 回退前的快照（不覆盖，新增）
        └── tool_results/
            └── {tool_name}-{ts}.json   # 工具执行结果缓存
```

`save()` 的副作用：
- `plan.version += 1`（乐观锁版本号）
- `plan.last_updated = datetime.now().isoformat()`
- 覆盖写入 `plan.json`

**快照** 由 `save_snapshot()` 创建，使用纳秒时间戳命名，永久保留，用于 backtrack 的 `snapshot_path` 引用。

---

## 十、可观测性（OpenTelemetry）

Phase 切换时创建名为 `phase.transition` 的 span，携带：

| 属性/事件 | 值 |
|----------|-----|
| `span attribute: PHASE_FROM` | 切换前的 phase 编号 |
| `span attribute: PHASE_TO` | 切换后的 phase 编号 |
| `span event: phase.plan_snapshot` | `destination`, `dates`, `daily_plans_count` |

这使得在 Jaeger/Zipkin 等追踪系统中可以直接观察每次会话的 Phase 演进轨迹。

---

## 置信度评估

| 结论 | 置信度 | 依据 |
|------|--------|------|
| Phase 推断逻辑（infer_phase） | ✅ 确定 | 直接读取源码 `router.py:16-27` |
| 三处触发点 | ✅ 确定 | `main.py:285`, `main.py:111`, `main.py:325` |
| Backtrack 字段清空规则 | ✅ 确定 | `models.py:209-248` 的 `_PHASE_DOWNSTREAM` |
| Control Mode 的实际效果 | ⚠️ 推断 | `PHASE_CONTROL_MODE` 定义在 `prompts.py`，但 `ContextManager` 如何使用它未深度追踪 |
| NLP 提取的覆盖率 | ⚠️ 推断 | 正则覆盖场景有限，LLM 工具调用是主要更新路径 |

---

## 脚注

[^1]: `backend/phase/router.py:16-27` — `infer_phase()` 实现
[^2]: `backend/phase/router.py:35-57` — `check_and_apply_transition()` 实现
[^3]: `backend/phase/prompts.py` — Phase 提示词和控制模式定义
[^4]: `backend/phase/backtrack.py:8-28` — `BacktrackService.execute()` 实现
[^5]: `backend/state/models.py:209-248` — `_PHASE_DOWNSTREAM` 和 `clear_downstream()` 实现
[^6]: `backend/main.py:103-111` — `on_tool_call` Hook 触发 phase 推进
[^7]: `backend/main.py:283-285` — `apply_trip_facts` 后立即推进 phase
[^8]: `backend/main.py:325-335` — 关键词 fallback 回退检测
[^9]: `backend/state/intake.py:101-110` — `apply_trip_facts()` NLP 提取
[^10]: `backend/tools/engine.py:21-22` — `get_tools_for_phase()` 按阶段过滤工具
