# Internal Task Visibility Design

## 1. 背景

当前聊天流能显示真实工具调用（如 `save_day_plan`）和部分 Agent 状态（如 thinking、compacting、parallel progress），但运行时内部耗时任务没有统一的前端表达。

典型问题来自 Phase 5 的 `save_day_plan` 后置评审链路：

1. `save_day_plan` 已完成状态写入。
2. `after_tool_call` hook 中的 `on_soft_judge` 同步调用 LLM 做行程质量评审。
3. 前端在收到 `tool_result` 前一直显示 `save_day_plan` 执行中。
4. 用户会误以为写入工具本身卡住，而真实等待点是内部质量评审。

本设计目标是建立统一的内部任务可观测模型，让所有内部耗时任务都进入聊天流，以用户可理解的方式展示进行中、完成、警告、失败和跳过状态。

## 2. 目标

- 真实工具调用和内部运行时任务在聊天流中语义分离。
- 所有内部耗时任务都有用户可见的生命周期状态。
- 阻塞型内部任务不能让上一个工具卡片看起来仍在执行。
- 后台型内部任务也进入聊天流，但用低调视觉层级和折叠详情降低噪音。
- Trace、聊天流和运行统计共享同一套任务语义，避免三套状态各自解释。

## 3. 非目标

- 不把内部任务伪装成 LLM tool call。
- 不改变 `ToolEngine` 的工具定义和工具选择机制。
- 不在第一版重做 TraceViewer 的整体信息架构。
- 不改变 soft judge、quality gate、memory extraction 的业务判断逻辑，只改变其可见性和生命周期事件。

## 4. 用户体验设计

聊天流新增“系统任务卡片”，与真实工具卡片并列显示。

真实工具卡片表达外部或状态写入动作：

- `save_day_plan`
- `web_search`
- `get_poi_info`
- `replace_all_day_plans`

系统任务卡片表达运行时内部流程：

- 行程质量评审
- 阶段推进检查
- 上下文整理/压缩
- 记忆召回
- 记忆抽取
- 反思注入
- Phase 5 并行编排

系统任务卡片字段：

- 任务名：用户可读 label。
- 状态：`pending`、`success`、`warning`、`error`、`skipped`。
- 耗时：开始后持续更新；结束后显示总耗时。
- 简短说明：说明系统正在做什么或结果摘要。
- 详情：可展开 JSON 或结构化摘要，如评分、建议、命中记忆、错误原因。
- 关联对象：如 `related_tool_call_id`、`phase`、`from_phase`、`to_phase`。

示例聊天顺序：

1. `保存单日行程` 工具卡片：成功。
2. `行程质量评审` 系统任务卡片：执行中。
3. `行程质量评审` 系统任务卡片：完成或警告，显示评分和建议。
4. Assistant 继续输出自然语言总结或修复建议。

## 5. 事件模型

新增 SSE 事件类型：`internal_task`。

事件承载内部任务的生命周期更新。同一个 `task.id` 的多次事件更新同一张卡片。

```json
{
  "type": "internal_task",
  "task": {
    "id": "soft_judge:call_abc",
    "kind": "soft_judge",
    "label": "行程质量评审",
    "status": "pending",
    "message": "正在检查节奏、地理顺路性、连贯性和个性化匹配…",
    "related_tool_call_id": "call_abc",
    "blocking": true,
    "scope": "turn",
    "started_at": 1776610000.0
  }
}
```

完成或警告：

```json
{
  "type": "internal_task",
  "task": {
    "id": "soft_judge:call_abc",
    "kind": "soft_judge",
    "label": "行程质量评审",
    "status": "warning",
    "message": "评分 3.5/5，发现 3 条建议",
    "related_tool_call_id": "call_abc",
    "blocking": true,
    "scope": "turn",
    "result": {
      "overall": 3.5,
      "pace": 4,
      "geography": 3,
      "coherence": 3,
      "personalization": 4,
      "suggestions": ["删除重复海龟湾安排", "统一交通方式"]
    },
    "ended_at": 1776610008.1
  }
}
```

字段约定：

| 字段 | 含义 |
|------|------|
| `id` | 单轮内稳定 ID。用于前端合并更新同一张卡片 |
| `kind` | 机器可读任务类型 |
| `label` | 用户可读任务名 |
| `status` | `pending` / `success` / `warning` / `error` / `skipped` |
| `message` | 一句话说明当前动作或结果 |
| `blocking` | 是否阻塞当前回复或阶段推进 |
| `scope` | `turn` / `background` / `session` |
| `related_tool_call_id` | 可选，关联真实工具调用 |
| `result` | 可选，结构化详情 |
| `error` | 可选，错误摘要 |
| `started_at` / `ended_at` | Unix timestamp，便于前端计算耗时 |

## 6. 后端设计

### 6.1 InternalTask 数据结构

新增轻量模型，建议放在 `backend/agent/types.py` 或单独的 `backend/agent/internal_tasks.py`：

```python
@dataclass
class InternalTask:
    id: str
    kind: str
    label: str
    status: str
    message: str | None = None
    blocking: bool = True
    scope: str = "turn"
    related_tool_call_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
```

`LLMChunk` 增加 `internal_task: InternalTask | None`，并新增 `ChunkType.INTERNAL_TASK`。不复用 `AGENT_STATUS`，避免与泛化 thinking 状态混用。

### 6.2 事件发射 helper

在 `main.py` 内部增加 helper：

```python
def build_internal_task_event(task: InternalTask) -> str:
    return json.dumps({"type": "internal_task", "task": task.to_dict()}, ensure_ascii=False)
```

对需要在 hook 内发事件的流程，提供一个 session 级队列：

- `session["_internal_task_events"] = asyncio.Queue()`
- hook 将 pending / final 事件放入队列。
- `_run_agent_stream()` 在安全点 drain 队列并 yield。

这能避免 hook 直接耦合 SSE writer，同时允许后台任务完成后向当前或下一轮流输出可见事件。

### 6.3 真实工具结果必须先结束

对 `save_day_plan` 这类工具后置评审，用户感知顺序必须是：

1. emit `tool_result` for `save_day_plan`
2. emit `internal_task pending` for `soft_judge`
3. run soft judge
4. emit `internal_task warning/success/error/skipped`

这要求 slow post-tool work 不能继续阻塞工具卡片终态。

实现方式：新增 `after_tool_result` hook，将 `on_soft_judge` 从 `after_tool_call` 移到 `after_tool_result`。`after_tool_call` 继续保留轻量状态变更和实时约束校验，确保现有 Stats 写入语义不回退；`after_tool_result` 承载可能耗时的后置评审。

### 6.4 覆盖任务

| kind | 来源 | blocking | scope | 说明 |
|------|------|----------|-------|------|
| `soft_judge` | `on_soft_judge` | true | turn | 工具写入后的行程质量评审 |
| `quality_gate` | `before_phase_transition` | true | turn | 阶段推进前质量门控 |
| `context_compaction` | `before_llm_call` / compression events | true | turn | 上下文整理和摘要压缩 |
| `memory_recall` | chat 入口 `memory_mgr.generate_context` | true | turn | 本轮记忆召回 |
| `memory_extraction` | `_schedule_memory_extraction` | false | background | 后台抽取待确认记忆 |
| `reflection` | `ReflectionInjector.check_and_inject` | false | turn | 本地反思提示注入 |
| `phase5_orchestration` | `Phase5Orchestrator` | true | turn | 并行编排总任务，worker 进度作为详情 |

### 6.5 超时与终态

所有内部任务必须保证有终态事件。

- 成功：`success`
- 成功但发现问题：`warning`
- 异常：`error`
- 配置关闭、无数据、无需执行：`skipped`

LLM 型内部任务必须有超时。建议：

- `soft_judge`: 20s
- `quality_gate`: 20s
- `memory_extraction`: 已有 20s，可补可见事件
- context summary compression: 20s 或沿用上下文预算配置

超时不能导致工具卡片 pending，也不能让当前 SSE 无终态。

## 7. 前端设计

### 7.1 类型

在 `frontend/src/types/plan.ts` 或新文件中增加：

```ts
export interface InternalTaskEvent {
  id: string
  kind: string
  label: string
  status: 'pending' | 'success' | 'warning' | 'error' | 'skipped'
  message?: string
  blocking: boolean
  scope: 'turn' | 'background' | 'session'
  related_tool_call_id?: string | null
  result?: unknown
  error?: string | null
  started_at?: number
  ended_at?: number
}
```

### 7.2 ChatPanel

`ChatPanel` 监听 `event.type === "internal_task"`。

行为：

- 如果 `task.id` 不存在，插入新的系统任务消息。
- 如果 `task.id` 已存在，更新同一条消息。
- pending 时显示持续耗时。
- final 状态时固定耗时和结果摘要。

同一轮内的后台任务允许在 assistant 之后继续追加或更新，但视觉层级应低于阻塞任务。

### 7.3 MessageBubble

新增 `internalTask` props，渲染系统任务卡片。

视觉原则：

- 不使用工具 badge 样式，避免和真实 tool 混淆。
- 使用 `系统任务` / `内部检查` 标签。
- `warning` 用琥珀色，`error` 用红色，`success` 用绿色，`pending` 用蓝色或中性色。
- 详情默认折叠，用户可展开查看 `result`。

### 7.4 TraceViewer

TraceViewer 后续可消费同一批 `InternalTaskRecord`，但第一版可先保留现有 `judge_scores` / `validation_errors` 展示，再逐步将内部任务纳入 trace timeline。

## 8. 降噪规则

虽然所有内部任务进入聊天流，但必须控制噪声：

- 同一个 task 更新同一张卡片，不重复追加。
- `skipped` 默认低调或折叠，但仍可见。
- `reflection` 默认折叠，除非注入了关键修正提示。
- `memory_recall` 命中 0 条显示短状态：“未找到可用记忆”，默认折叠。
- `memory_extraction` 是后台任务，显示为低调卡片，不抢占主回答视觉层级。
- Phase 5 并行 worker 详情保留在展开区，聊天流只显示总任务摘要。

## 9. 测试策略

### 后端

- `save_day_plan` 后 soft judge 慢时，SSE 顺序必须是 `tool_result` 先于 `internal_task soft_judge pending` 的终态阻塞。
- `on_soft_judge` 发出 pending 和 final internal task。
- `before_phase_transition` 发出 `quality_gate pending` 和 `success/warning/error/skipped`。
- memory extraction 开始和完成时发 `memory_extraction` 事件，不阻塞主响应。
- context compaction 现有事件映射为 `internal_task context_compaction`。
- 内部任务异常时仍发 `error` 或 `skipped` 终态。

### 前端

- pending 和 final 事件使用同一 `task.id` 更新同一张卡片。
- `status` 不同值渲染不同视觉状态。
- 详情可展开，并显示 `result`。
- 后台任务卡片低调显示。

### E2E

- 模拟 `save_day_plan` 后 soft judge 慢 8 秒：
  - `save_day_plan` 工具卡片应显示成功。
  - 聊天流应显示“行程质量评审 · 执行中”。
  - soft judge 完成后同一张卡片更新为完成或警告。
- 模拟阶段推进前 quality gate 慢 5 秒：
  - 聊天流应显示“阶段推进检查 · 执行中”。
  - 阻断时显示建议。
- 模拟后台记忆抽取：
  - Assistant 回复不被阻塞。
  - 记忆抽取卡片在后台完成后显示结果。

## 10. 实施顺序

1. 后端类型与 SSE 事件：新增 `InternalTask` 和 `internal_task` 事件输出。
2. `on_soft_judge` 可见化：先解决当前误导问题。
3. 前端系统任务卡片：ChatPanel 状态合并 + MessageBubble 渲染。
4. quality gate 可见化。
5. context compaction / memory recall / memory extraction / reflection / phase5 orchestration 纳入统一事件。
6. TraceViewer 后续增强：将 internal tasks 纳入时间线。

## 11. 验收标准

- 用户永远能区分“真实工具执行中”和“内部系统任务执行中”。
- 任一内部任务开始后最终都有可见终态。
- `save_day_plan` 完成后不会因为 soft judge 而继续显示执行中。
- 所有内部耗时任务都进入聊天流。
- 后台任务不阻塞当前回答，但仍可见。
- 现有工具卡片、状态更新、阶段转换、记忆中心和 Phase 5 并行进度不回退。
