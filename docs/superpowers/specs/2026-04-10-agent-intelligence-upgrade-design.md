# Agent 智能升级设计文档

> **状态**：已确认，待实施
> **日期**：2026-04-10
> **范围**：6 个 Agent 技术模块的统一设计

---

## 1. 背景与目标

当前 Travel Agent Pro 在 Agent "骨架"层面（状态机、工具系统、上下文压缩）已具备生产级水平，但在"智能"层面（自省、自迭代、自动记忆、并行执行）仍停留在第一代 Agent 的水平。

本次升级目标：引入 6 项 Agent 核心技术，使项目从"能跑通的 Agent"升级为"有自我改进能力的 Agent"。

### 6 个模块

| # | 模块 | 深度 | 架构层 |
|---|------|------|--------|
| 1 | Evaluator-Optimizer 质量门控 | 接近生产级 | Hook 扩展 |
| 2 | Reflection 自省机制 | 接近生产级 | Hook 扩展 |
| 3 | 并行工具执行 | 能演示 | 核心改造 |
| 4 | Structured Output 强制约束 | 能演示 | 核心改造 |
| 5 | Memory Extraction 自动记忆提取 | 能演示 | Hook 扩展 |
| 6 | Tool Guardrails 工具护栏 | 能演示 | Hook 扩展 |

### 架构组织原则（混合式）

- **核心改造**：Parallel Tool Exec（改 `ToolEngine`）、Structured Output（改 `AgentLoop` LLM 调用参数）—— 执行路径的本质变化
- **Hook 扩展**：Evaluator-Optimizer、Reflection、Memory Extraction、Tool Guardrails —— 可插拔的增强能力

区分 Agent 的"执行骨架"和"可插拔智能层"。

---

## 2. 模块 1：Evaluator-Optimizer 质量门控

### 定位

在阶段转换前插入质量评估关卡。评分不达标时，将修改建议注入回 Agent 循环，让 Agent 在当前阶段自我优化，直到通过或达到最大重试次数。

### 触发位置

`PhaseRouter.check_and_apply_transition()` — 当 `infer_phase(plan)` 检测到阶段应该前进时，先调用 Evaluator，通过后才允许转换。

### 评估范围

| 转换 | 评估内容 | 评分维度 |
|------|---------|---------|
| Phase 1→3 | destination 是否充分确认、用户约束是否已收集 | completeness（规则检查，无 LLM 调用） |
| Phase 3→5 | skeleton 质量、交通住宿锁定情况 | pace, geography, coherence, personalization（SoftScore） |
| Phase 5→7 | 完整行程 | 全部 4 维度 + 硬约束校验 |

### 核心流程

```
PhaseRouter.check_and_apply_transition(plan)
  │
  ├─ inferred_phase > current_phase ?
  │     │ no → return False
  │     │ yes ↓
  │
  ├─ Hook: before_phase_transition
  │     │
  │     ├─ 硬约束校验 (validate_hard_constraints)
  │     │     └─ 有 error → 注入修正指令, 阻止转换
  │     │
  │     ├─ 软评分 (SoftJudge, 需一次 LLM 调用)
  │     │     ├─ overall >= 3.5 → 放行
  │     │     ├─ overall < 3.5 且 retry < max_eval_retries(2) →
  │     │     │     注入 suggestions 为 system message, 阻止转换
  │     │     └─ overall < 3.5 且 retry >= 2 → 强制放行 + 记录警告
  │     │
  │     └─ 返回 (allow: bool, feedback: str | None)
  │
  ├─ allow=True → 正常执行阶段转换
  └─ allow=False → return False, feedback 已注入消息列表
```

### 关键设计决策

- **评分阈值 3.5**：4 维度平均分，可在 `config.yaml` 中配置
- **最大重试 2 次**：避免无限循环。第 3 次强制放行并在 telemetry 中记录为 `quality_gate_forced_pass`
- **Phase 1→3 用轻量评估**：不调用 SoftJudge，只检查关键字段完整性（destination、dates、budget 非空），用规则而非 LLM
- **评分结果写入 span**：`quality_gate.score`、`quality_gate.passed`、`quality_gate.retry_count`

### HookManager 改造要求

当前 `HookManager` 的 hook 是 fire-and-forget（无返回值）。`before_phase_transition` 需要返回 `(allow: bool, feedback: str | None)` 来控制是否放行。改造方式：

- `HookManager.run()` 保持现有语义（无返回值），用于 `before_llm_call`、`after_tool_call` 等
- 新增 `HookManager.run_gate()` 方法，返回 `GateResult(allow: bool, feedback: str | None)`，用于 `before_phase_transition`
- `check_and_apply_transition()` 改为 `async` 方法（因为 SoftJudge 需要 async LLM 调用）

### LLM 实例获取

质量门控的 SoftJudge 需要一次 LLM 调用。通过 `HookManager` 初始化时注入 `llm_factory`，在 `before_phase_transition` handler 内部按需创建 LLM 实例（可复用 `config.yaml` 中已有的 provider 配置，或指定低成本模型）。

### 代码改动

- `agent/hooks.py`：新增 `before_phase_transition` 事件类型 + `run_gate()` 方法
- `phase/router.py`：`check_and_apply_transition()` 改为 `async`，内调用 hook gate
- `harness/judge.py`：复用现有 `build_judge_prompt` + `parse_judge_response`，无改动
- `agent/loop.py`：`check_and_apply_transition()` 调用处加 `await`；阶段转换阻止时将 feedback 追加为 system message
- `config.yaml`：新增 `quality_gate.threshold: 3.5`、`quality_gate.max_retries: 2`

---

## 3. 模块 2：Reflection 自省机制

### 定位

轻量级内省提醒。在 Agent 完成关键操作后，通过 system message 提示 LLM 回顾当前方案是否遗漏用户需求。与 Evaluator-Optimizer 互补：Evaluator 是外部质量门控（阻止转换），Reflection 是内部自检（引导主动修正）。

### 触发时机

复用 `before_llm_call` hook，在特定条件下注入 reflection prompt：

| 条件 | 触发 Reflection |
|------|----------------|
| Phase 3 step 从 `skeleton` 变为 `lock` | 回顾 trip_brief 中的偏好/约束是否在骨架中体现 |
| Phase 5 且 `daily_plans` 天数刚好填满 | 回顾完整行程是否满足用户所有需求 |

### 注入模板

```
REFLECTION_PHASE3_LOCK:
"[自检]
你即将进入交通住宿锁定阶段，请先快速回顾：
1. 用户的偏好（{preferences_summary}）是否都在骨架方案中体现了？
2. 用户的约束（{constraints_summary}）有没有被违反？
3. 有没有用户明确说过"必须"或"不要"的内容被遗漏？
如果发现问题，先修正骨架再继续。如果没有问题，直接进入锁定。"

REFLECTION_PHASE5_COMPLETE:
"[自检]
所有天数的行程已填写完毕，请快速检查：
1. 用户最初提到的所有"必去"景点是否都安排了？
2. 每天的节奏是否符合用户偏好（{pace_preference}）？
3. 有没有连续两天重复相似类型的活动？
如果发现问题，调用 update_plan_state 修正。如果没有问题，继续。"
```

### 实现机制

```python
class ReflectionInjector:
    def __init__(self):
        self._triggered: set[str] = set()  # 每次会话内去重

    def check_and_inject(
        self, messages: list[Message], plan: TravelPlanState, prev_step: str | None
    ) -> str | None:
        key = self._compute_trigger_key(plan, prev_step)
        if key is None or key in self._triggered:
            return None
        self._triggered.add(key)
        return self._build_prompt(key, plan)
```

### 关键设计决策

- **每个触发点只触发一次**：`_triggered` 集合去重
- **模板中注入具体的用户偏好/约束**：从 `plan.preferences` 和 `plan.constraints` 摘要填入，给 LLM 具体检查清单
- **不额外调用 LLM**：只追加 system message，主 Agent 下一轮迭代自然处理
- **与 State Repair 的区别**：State Repair 检测格式问题（说了没写），Reflection 检测质量问题（方案是否满足需求）

### 代码改动

- 新增 `agent/reflection.py`：`ReflectionInjector` 类，~60 行
- `agent/hooks.py`：在 `before_llm_call` handler 中调用 `ReflectionInjector.check_and_inject()`
- `agent/loop.py`：`AgentLoop.__init__` 接收 `ReflectionInjector` 实例

---

## 4. 模块 3：并行工具执行

### 定位

当 LLM 一次返回多个工具调用时，将无副作用的"读"操作并行执行，有副作用的"写"操作保持顺序执行。

### 工具分类

在 `@tool` 装饰器新增 `side_effect` 属性：

| side_effect | 工具 |
|-------------|------|
| `read` | search_flights, search_trains, search_accommodations, web_search, xiaohongshu_search, get_poi_info, check_weather, check_availability, check_feasibility, calculate_route, quick_travel_search |
| `write` | update_plan_state, assemble_day_plan, generate_summary |

### 执行策略

```
tool_calls = [search_flights, search_accommodations, check_weather, update_plan_state]

Step 1: 按 side_effect 分组
  reads  = [search_flights, search_accommodations, check_weather]
  writes = [update_plan_state]

Step 2: 并行执行所有 reads
  results = await asyncio.gather(*[engine.execute(tc) for tc in reads], return_exceptions=True)

Step 3: 顺序执行所有 writes（reads 全部完成后）
  for tc in writes:
      result = await engine.execute(tc)

Step 4: 按原始顺序排列所有 results 回消息列表
```

### 关键设计决策

- **结果按原始顺序排列**：LLM 期望 tool_results 和 tool_calls 顺序一致
- **并行中某个失败不影响其他**：`asyncio.gather(return_exceptions=True)`
- **SSE 事件顺序**：并行执行完成后按原始 index 逐个 yield
- **Backtrack 中断**：并行批次中出现 backtrack result 时取消其他任务
- **配置开关**：`config.yaml` 新增 `parallel_tool_execution: true`

### 与 Tool Guardrails 的交互

`execute_batch()` 内部对每个工具执行前仍调用 `before_tool_exec` hook（Guardrails 输入校验）。被 reject 的工具不进入并行池，直接构造 error ToolResult。流程：

```
execute_batch(tool_calls):
  for tc in tool_calls:
    guardrail_result = hook.run_gate("before_tool_exec", tc)
    if rejected → 记录 error result, 跳过
    else → 按 side_effect 分入 reads/writes 池
  并行执行 reads → 顺序执行 writes → 按原始顺序返回所有 results
```

### 代码改动

- `tools/base.py`：`@tool` 装饰器和 `ToolDef` 新增 `side_effect: str = "read"` 字段
- `tools/engine.py`：新增 `execute_batch(tool_calls) -> list[ToolResult]` 方法
- `agent/loop.py`：顺序循环替换为 `engine.execute_batch(tool_calls)`
- 各工具文件：`update_plan_state`、`assemble_day_plan`、`generate_summary` 标记 `side_effect="write"`

---

## 5. 模块 4：Structured Output / Forced Tool Choice

### 定位

在关键决策点强制 LLM 通过工具调用输出结构化数据，消除"说了但没写状态"的问题根源。

### 触发条件

| 条件 | tool_choice 值 |
|------|---------------|
| Phase 3 step=`brief` 且 `trip_brief` 为空 且已有 ≥2 轮对话 | forced `update_plan_state` |
| Phase 3 step=`skeleton` 且 `skeleton_plans` 为空 且上一条助手消息包含方案关键词 | forced `update_plan_state` |
| Phase 5 且 `daily_plans` 天数未满 且上一条助手消息包含逐日内容 | forced `update_plan_state` |
| 其他所有情况 | `"auto"` |

### 助手消息内容检测

复用现有 State Repair 中已验证的关键词匹配逻辑（非新增）：
- skeleton 场景：检测 `"骨架"`, `方案\s*[A-C1-3]`, `"轻松版"/"平衡版"/"高密度版"`
- Phase 5 场景：检测 `第N天/Day N` + 时间槽 `\d{1,2}:\d{2}` 或活动关键词

与 State Repair 的区别：State Repair 在 LLM 输出后做检测并注入修正提示（被动补救），ToolChoiceDecider 在 LLM 调用前根据当前状态预判并强制工具调用（主动预防）。两者检测逻辑可共享，但作用时机不同。

### 实现机制

```python
class ToolChoiceDecider:
    def decide(
        self, plan: TravelPlanState, messages: list[Message], phase: int
    ) -> dict | str:
        if self._should_force_state_write(plan, messages, phase):
            return {"type": "function", "function": {"name": "update_plan_state"}}
        return "auto"
```

### 与 State Repair 的关系

渐进式替代，非一刀切：
1. 新增 `ToolChoiceDecider`，触发条件命中时强制工具调用
2. State Repair 保留作为 fallback
3. Telemetry 记录 `forced_tool_choice_triggered` 和 `state_repair_triggered`
4. 长期观察后，repair 触发率降至零时可安全删除

### LLM Provider 兼容性

| Provider | 格式 |
|----------|------|
| OpenAI | `{"type": "function", "function": {"name": "xxx"}}` |
| Anthropic | `{"type": "tool", "name": "xxx"}`（provider 内部转换） |

### 代码改动

- 新增 `agent/tool_choice.py`：`ToolChoiceDecider` 类，~50 行
- `llm/base.py`：`chat()` 接口新增 `tool_choice: dict | str | None = None` 参数
- `llm/openai_provider.py`：透传 tool_choice 参数
- `llm/anthropic_provider.py`：格式转换后透传
- `agent/loop.py`：LLM 调用前调用 `ToolChoiceDecider.decide()`

---

## 6. 模块 5：Memory Extraction 自动记忆提取

### 定位

在 Phase 1→3 转换时，从对话历史中自动提取用户的跨会话持久偏好，写入 `UserMemory`。

### 提取流程

```
Phase 1→3 转换触发
  │
  ├─ 收集 Phase 1 的所有用户消息
  ├─ 构建提取 prompt（附带现有 UserMemory 避免重复）
  ├─ 异步 LLM 调用（低成本模型，如 gpt-4o-mini，强制 JSON 输出）
  ├─ 解析结果，合并到现有 UserMemory
  │     ├─ preferences: 新值覆盖同 key 旧值
  │     ├─ rejections: 按 item 去重追加
  │     └─ 忽略本次旅行专属信息
  └─ 异步保存（不阻塞主流程）
```

### 提取 Prompt

```
从以下用户消息中提取**持久化个人偏好**（适用于未来任何旅行，不限于本次）。

提取规则：
- 只提取用户明确表达的偏好，不推测
- 排除本次旅行专属信息（具体目的地、具体日期、本次预算）
- 适合提取：饮食禁忌、住宿星级/类型偏好、飞行座位偏好、节奏偏好、带小孩/老人的常态
- 不适合提取："这次想去京都""预算3万""4月15号出发"
- 已有记忆中已包含的不要重复输出

输出 JSON：
{"preferences": {"key": "value"}, "rejections": [{"item": "...", "reason": "...", "permanent": true/false}]}
```

### 关键设计决策

- **低成本模型**：提取任务不需要强推理，成本约主模型 1/10
- **异步非阻塞**：`asyncio.create_task()` 发起，提取失败不影响主流程
- **幂等合并**：多次对话提取结果可安全合并
- **传入已有记忆**：避免重复提取

### 代码改动

- 新增 `memory/extraction.py`：`MemoryExtractor` + `MemoryMerger`，~80 行
- `agent/hooks.py`：新增 `on_phase_change` 事件，Phase 1→3 时调用 extractor
- `config.yaml`：新增 `memory_extraction.enabled`、`memory_extraction.model`

---

## 7. 模块 6：Tool Guardrails 工具护栏

### 定位

工具执行前后的确定性校验层，拦截不合理的参数和异常输出。

### 架构位置

```
LLM 返回 tool_call
  ├─ Hook: before_tool_exec → Guardrail 输入校验
  │     ├─ pass → 继续执行
  │     └─ reject → 构造 error ToolResult，跳过执行
  ├─ ToolEngine.execute()
  └─ Hook: after_tool_call → Guardrail 输出校验（扩展已有 hook）
        ├─ pass → 正常返回
        └─ warn → 附加警告信息供 LLM 参考
```

### 输入校验规则

| 规则 | 工具 | 检查内容 | 处理 |
|------|------|---------|------|
| 日期合法性 | search_flights, search_trains, check_weather | 日期不能是过去的日期 | reject + suggestion |
| 预算非负 | update_plan_state (field=budget) | total 不能是负数或零 | reject |
| 地名非空 | search_flights, search_accommodations, get_poi_info | origin/destination/query 不能是空字符串 | reject |
| 天数上限 | update_plan_state (field=daily_plans) | 单次写入天数不超过 plan.dates.total_days | reject |
| Prompt Injection 基础检测 | 所有工具 | 参数值包含 `ignore previous instructions` 等模式 | reject + 告警 |

### 输出校验规则

| 规则 | 工具 | 检查内容 | 处理 |
|------|------|---------|------|
| 搜索结果为空 | search_flights, search_accommodations | results 列表为空 | warn |
| 价格异常 | search_flights, search_accommodations | 单价 > 100,000 | warn |
| 结果过大 | 所有搜索工具 | result data > 50,000 字符 | 截断 + warn |

### 关键设计决策

- **规则硬编码**：Guardrails 必须确定性，不依赖 LLM 判断
- **reject 带 suggestion**：让 LLM 知道为什么被拒、怎么修正
- **输出用 warn 不用 reject**：结果已产生，附加警告让 LLM 自行判断
- **可配置**：`config.yaml` 中可全局开关或按规则名关闭

### 代码改动

- 新增 `harness/guardrail.py`：`ToolGuardrail` 类，~100 行
- `agent/hooks.py`：新增 `before_tool_exec` 事件类型
- `agent/loop.py`：`tool_engine.execute(tc)` 前调用 hook，reject 时跳过执行
- `config.yaml`：新增 `guardrails.enabled`、`guardrails.disabled_rules`

---

## 8. Hook 系统扩展总览

当前 hook 事件：
- `before_llm_call`
- `after_tool_call`

升级后 hook 事件：
| 事件 | 时机 | 消费者 |
|------|------|--------|
| `before_llm_call` | 每次 LLM 调用前 | Context 压缩（已有）、Reflection 注入（新） |
| `before_tool_exec` | 每个工具执行前 | Tool Guardrails 输入校验（新） |
| `after_tool_call` | 每个工具执行后 | 硬约束校验（已有）、Tool Guardrails 输出校验（新） |
| `before_phase_transition` | 阶段转换前 | Evaluator-Optimizer 质量门控（新） |
| `on_phase_change` | 阶段转换后 | Memory Extraction（新，仅 Phase 1→3） |

---

## 9. 配置扩展

```yaml
# config.yaml 新增项

quality_gate:
  threshold: 3.5
  max_retries: 2

parallel_tool_execution: true

memory_extraction:
  enabled: true
  model: "gpt-4o-mini"

guardrails:
  enabled: true
  disabled_rules: []  # 可填入规则名关闭特定规则
```

---

## 10. 新增文件清单

| 文件 | 行数估算 | 说明 |
|------|---------|------|
| `agent/reflection.py` | ~60 | ReflectionInjector |
| `agent/tool_choice.py` | ~50 | ToolChoiceDecider |
| `memory/extraction.py` | ~80 | MemoryExtractor + MemoryMerger |
| `harness/guardrail.py` | ~100 | ToolGuardrail |

### 改动文件清单

| 文件 | 改动性质 |
|------|---------|
| `agent/loop.py` | 并行调度、forced tool_choice、guardrail hook 调用 |
| `agent/hooks.py` | 新增 3 个事件类型、注册新 handler |
| `tools/base.py` | `ToolDef` 新增 `side_effect` 字段 |
| `tools/engine.py` | 新增 `execute_batch()` 方法 |
| `phase/router.py` | `check_and_apply_transition()` 集成质量门控 |
| `llm/base.py` | `chat()` 新增 `tool_choice` 参数 |
| `llm/openai_provider.py` | 透传 tool_choice |
| `llm/anthropic_provider.py` | tool_choice 格式转换 |
| 各工具文件 | 3 个写工具标记 `side_effect="write"` |
| `config.yaml` | 新增 4 个配置段 |
