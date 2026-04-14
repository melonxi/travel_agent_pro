# Agent 前端等待体验与状态同步设计

## 概述

当前聊天面板在长时间工具调用、Phase 切换、LLM 思考阶段缺少与后端执行节奏对齐的反馈，导致两类问题：

1. **执行与 UI 错位**：后端已完成 Phase 切换（`check_and_apply_transition` 返回 true），但顶部 `PhaseIndicator` 要等到下一次 `update_plan_state` 成功或整轮结束才更新；Phase3 子步骤同样滞后。
2. **等待焦虑**：发送到首 token 之间 2–5 秒无占位、工具 pending 状态无耗时/副标题、30 秒才升级到连接不稳提示、`done` 事件静默收尾、`memory_recall` 只在侧栏高亮。

本设计在现有 SSE 协议上新增 **2 个事件 + 1 个字段**，并分 4 个 PR 按优先级纵向交付，在不破坏已有 retry/continue 机制的前提下消除错位和焦虑。

## 设计目标

1. Phase 和 Phase3 子步骤切换立即在 UI 反映，不等 `state_update` 延迟。
2. 每轮交互的四个阶段（思考 / 工具执行 / 汇总 / 收尾）在 UI 上可辨识。
3. 等待反馈分三档（0–8s / 8–20s / 20s+），小卡顿可见但不打断。
4. 工具条展示人类可读的动作描述（"翻小红书找灵感"），pending 状态带计时器。
5. 保留 Solstice 克制气质（方案 D：Claude 风为基 + 关键节点显式化 + 轻度人格化）。
6. 不改动 `error` / `done` / `can_continue` / `retryable` 字段，不影响现有 retry-recovery 机制。

## 非目标

显式排除以避免 scope creep：

1. **真思维链投射**（LLM reasoning chunk 透传）——PR4 仅做 spike 调研
2. 移动端适配——专注桌面
3. Trace 面板的实时等待态优化——Trace 是事后审计视图
4. 通知系统（声音 / 桌面通知 / tab title）
5. i18n——中文优先
6. MemoryCenter 抽屉内部 UI——已成熟

## 协议契约（共享基础）

### 新事件 1：`phase_transition`

合并处理主 phase 切换与 Phase3 子步骤切换（结构同构）。

```json
{
  "type": "phase_transition",
  "from_phase": 1,
  "to_phase": 3,
  "from_step": null,
  "to_step": "brief",
  "reason": "check_and_apply_transition"
}
```

**触发点**：
- `backend/agent/loop.py:410` `check_and_apply_transition` 返回 true 时，loop yield `LLMChunk(type=PHASE_TRANSITION, phase_info={...})`
- `loop.py:381` 显式 phase 变化路径（由 `update_plan_state` 直接触发的 phase 跳变）
- `phase3_step` 变化：在 `on_validate` hook 检测 `updated_field == "phase3_step"` 时 yield；使用 session 级 `_pending_phase_transition` 暂存模式（复用 `_pending_state_changes` 的时序处理）
- Backtrack 路径：`_is_backtrack_result` 分支 yield `from_phase > to_phase` 的反向迁移，前端据此清理任何前向 override

**幂等**：前端按 `(from_phase, to_phase, from_step, to_step)` 去重；同一对多次到达不重复动画。

### 新事件 2：`agent_status`

承载仅后端可知的生命周期状态。

```json
{
  "type": "agent_status",
  "stage": "thinking",
  "iteration": 0,
  "hint": null
}
```

**stage 枚举**：

| stage | 触发点 | 前端用途 |
|---|---|---|
| `thinking` | `before_llm_call` hook 每次调用 | ThinkingBubble 呈现/保持 |
| `summarizing` | 若上一 iteration 执行过工具且无 phase 变化，下一轮 LLM 调用前（loop 内用 bool flag 追踪） | ThinkingBubble 文案切"汇总中…" |
| `compacting` | `before_llm_call` 预判到压缩将触发时 | ThinkingBubble 文案切"整理上下文中…" |

**可选字段**：
- `iteration` — 本轮第 N 次 LLM 调用，超过 1 时前端展示"继续思考…（第 N 轮）"
- `hint` — PR4 Track A 使用；规则式合成的意图方向文案，v1 为空

### 字段增强：`tool_call.human_label`

现有 `tool_call` 事件加一个可选字段：

```json
{
  "type": "tool_call",
  "tool_call": {
    "id": "…",
    "name": "xiaohongshu_search",
    "arguments": {…},
    "human_label": "翻小红书找灵感"
  }
}
```

**来源**：`backend/tools/base.py` 的 `@tool` 装饰器新增 `human_label: str | None = None` 参数，`ToolEngine` 构造 `tool_call` 时读出。空值时前端 fallback 到 `name`。

**文案原则**：动作 + 对象，≤ 8 汉字，略带人格化。完整 24 个工具映射表见附录 A。

### 向后兼容

- 新事件前端未识别时静默丢弃（`useSSE` 已有兜底）
- 新字段都是可选，老会话回放不受影响
- 不删除/重命名现有字段
- `error` / `done` / retry / continue 语义完全不动

## PR1（P0）· Phase 同步

**目标**：消除 Phase tab 和 Phase3 子步骤的 UI 滞后。

### 后端

`backend/llm/types.py` 扩展：

```python
class ChunkType(Enum):
    PHASE_TRANSITION = "phase_transition"
    AGENT_STATUS = "agent_status"  # PR2 使用
```

`LLMChunk` 增加可选字段：
- `phase_info: dict | None`
- `agent_status: dict | None`

`backend/agent/loop.py`：在三处 yield：

1. `check_and_apply_transition` 返回 true 后（`loop.py:410` 之后）
2. 显式 phase 变化路径（`loop.py:381` 之后）
3. Backtrack 分支（`_is_backtrack_result` 之后）

`backend/main.py`：新增 chunk type 到 SSE 事件的翻译分支（约 10 行）。

### 前端

`App.tsx` 新增 hoist state：

```ts
const [phaseOverride, setPhaseOverride] = useState<{
  phase: number
  step?: string
  expiresAt: number
} | null>(null)
```

- 收到 `phase_transition` 时 `setPhaseOverride({ phase, step, expiresAt: Date.now() + 800 })`
- 下一次 `state_update` 到达且 `plan.phase` 与 override 一致时，清空 override
- Backtrack 事件（`from_phase > to_phase`）立即清空 override，避免回退后仍指向未来

`PhaseIndicator.tsx`：

```ts
const effectivePhase = phaseOverride?.phase ?? currentPhase
```

动效：顶部 tab 下方新增锚点条元素，横向滑动 300ms ease-out；目标 tab 触发 180ms 发光脉冲。

`Phase3Workbench.tsx`：同样叠加 `phaseOverride?.step` 作为 `activeStep` 覆盖。

新增 `PhaseTransitionCard`（MessageBubble 新 variant）：切换瞬间插入系统消息 "已进入方案设计阶段"，复用 `state-update-card` 基础样式。

### 测试

| 层级 | 用例 |
|---|---|
| 后端 unit | `test_phase_transition_event_emitted_on_check_and_apply` / `test_phase_transition_on_explicit_phase_change` / `test_phase_transition_not_emitted_when_no_change` / `test_phase_transition_on_backtrack` |
| 前端 unit | `ChatPanel` 收到 phase_transition → PhaseIndicator effectivePhase 立即变更；state_update 到达后 override 清空 |
| E2E | 扩展 `e2e-test.spec.ts`：发送"锁定候选"消息后 300ms 内 tab 应已切到 Phase 3 |

## PR2（P1）· 思考气泡 + 工具信息增强

**目标**：消除首 token 空白和工具 pending 黑箱。

### 后端

`backend/agent/loop.py` 在 `before_llm_call` hook 后 yield `AGENT_STATUS(thinking)`；在最后一次工具批完成且无 phase 变化、即将进入收尾 LLM 调用时 yield `AGENT_STATUS(summarizing)`。

`backend/tools/base.py`：`@tool` 装饰器加 `human_label` 参数；`ToolDef` 记录该字段；`ToolEngine` 构造 tool_call 事件时带出。

全部工具补 `human_label`（目前 ≈ 24 个，见附录 A；实际数量以 PR2 实施时 `ToolEngine.list_tools()` 为准）。

### 前端

新增组件 `ThinkingBubble`：
- 发送瞬间本地插入（不等后端），视觉上一个脉动光点 + "思考中…"
- 收到 `agent_status.thinking` 时确认保持
- 收到 `text_delta` / `tool_call` / `error` 时 200ms fade out
- `iteration ≥ 1` 时文案换成"继续思考…（第 N 轮）"
- 2 秒超时兜底：若 2s 内无任何 SSE 事件，文案切为"正在连接…"，衔接现有 `streamFeedback.waiting`

`MessageBubble` 的 tool 卡改动：
- 新增 `tool-subtitle` 行：显示 `human_label`（灰色小字）
- 新增计时器：`pending` 时每 500ms 更新，结束时冻结在最终耗时
- Pending 超 8s 时副标题末尾追加"（运行较久，请稍候）"，tone 转 muted warning
- Pending 状态外层加极轻呼吸动画（`.tool.pending::before` 左侧 2px 渐变条流动）

`ChatPanel` 新增 tool message 字段 `startedAt`（tool_call 到达时 `Date.now()`）、`endedAt`（tool_result 到达时）。

### 测试

| 层级 | 用例 |
|---|---|
| 后端 unit | `test_agent_status_thinking_emitted_before_llm_call` / `test_agent_status_summarizing_emitted_after_last_tool` / `test_tool_call_event_includes_human_label` / `test_human_label_serializes_chinese_correctly` |
| 前端 unit | ThinkingBubble 生命周期（插入 → text_delta 到达 → 移除）；工具条计时器冻结逻辑 |
| E2E | 新建 `e2e-waiting-experience.spec.ts`：发送后 500ms 内断言 `[data-testid=thinking-bubble]` 存在；首 text_delta 后 300ms 内断言移除；工具条 pending 状态显示 `human_label` 和 ≥1s 的计时器读数 |

## PR3（P2）· 细颗粒反馈 + 回声收尾

### Keepalive 三档

| 静默时长 | UI 反馈 |
|---|---|
| 0–8s | 正常（ThinkingBubble / 工具计时器在跑） |
| 8–20s | 当前焦点元素右侧挂呼吸小点（淡琥珀 `⋯`） |
| 20s+ | 升级为 `streamFeedback.waiting` 横幅 |

- 后端 keepalive 从 15s 降至 **8s**
- 前端 `KEEPALIVE_TIMEOUT_MS` 从 30_000 降至 **20_000**
- `ChatPanel` 新增 `staleness` 状态，基于 `lastEventTimeRef` 每 2s 计算；呼吸小点挂在最新 pending 工具条或 ThinkingBubble 上

### RoundSummaryBar

`done` 事件且 `run_status === "completed"` 时，在最后一条消息下方插入一条高 22px 的收尾条：

```
✓ 本轮已完成 · 3 个工具 · 用时 5.4s · 命中 2 条记忆
```

- 数据：工具计数（tool_call 事件累计）+ 用时（firstEventAt → done）+ 记忆命中（最后一次 memory_recall 的 item_ids 长度）
- 2.5s 后 fade out 移除 DOM
- 停止 / 错误路径不显示
- 新一轮发送时立即销毁历史 Summary

### Memory Recall 内联 chip

第一次 `memory_recall` 到达时插入 `role: 'system'` 卡片：

```
💭 本轮使用 2 条旅行记忆
```

- 复用 `system-state-update` 结构
- 点击打开 MemoryCenter 抽屉（复用 `App.tsx` 已有逻辑）
- 每轮只插一次（tool_call 开始到 done 之间的窗口去重）

### Compacting 预告

在 `before_llm_call` hook 内，复用 `compact_messages_for_prompt` 的 token 估算判定。若预判将触发压缩，提前 yield `AGENT_STATUS(compacting)`。ThinkingBubble 文案切换为"整理上下文中…"，随后的 `context_compression` 卡片无违和感。

### 测试

| 层级 | 用例 |
|---|---|
| 后端 unit | `test_keepalive_interval_is_8s` / `test_agent_status_compacting_emitted_when_budget_triggers_compression` |
| 前端 unit | 3 档静默升级路径（mock 时间流逝）；RoundSummaryBar 2.5s 淡出；memory_recall chip 单轮去重 |
| E2E | 扩展 `e2e-waiting-experience.spec.ts`：done 事件后 summary bar 出现且 2.5s 内消失 |

## PR4（P3）· 推理旁白探索

### Track A：规则式 narration（落地）

`backend/agent/loop.py` 在 `before_llm_call` 新增 `compute_narration(plan, last_user_msg) -> str | None`：

```python
def compute_narration(plan, last_user_msg) -> str | None:
    if plan.phase == 1 and not plan.destination:
        return "先搞清楚你想去哪，然后翻点真实游记"
    if plan.phase == 3 and plan.phase3_step == "brief":
        return "建立旅行画像，理清你的节奏和偏好"
    if plan.phase == 3 and plan.phase3_step == "candidate":
        return "挑几个候选景点，看看哪些对你胃口"
    if plan.phase == 3 and plan.phase3_step == "skeleton":
        return "把候选拼成 2–3 套骨架方案"
    if plan.phase == 3 and plan.phase3_step == "lock":
        return "锁定交通和住宿，核一下预算"
    if plan.phase == 5:
        return "把骨架展开成日程，核对冲突"
    if plan.phase == 7:
        return "做出发前检查清单"
    return None
```

结果写入 `agent_status.hint`。

**前端**：ThinkingBubble 收到带 hint 的 agent_status 时，文案替换为 hint；否则保持"思考中…"。气泡右侧挂小 × 按钮，手动收起后偏好持久化到 `localStorage`（默认折叠只显示"思考中…"）。

**文案原则**：只描述**意图方向**，不指定具体工具名，避免"说要查小红书但没查"的信任流失。

### Track B：真思维链 spike（不落地）

产出 `docs/learning/2026-0X-XX-thinking-stream-spike.md`，调研：

1. `claude-sonnet-4-20250514` / `gpt-4o` 是否支持 reasoning chunk，开启后 token 成本变化
2. Provider 层 `LLMChunk` 改造（`ChunkType.REASONING_DELTA`）
3. UI 形态：accordion 嵌入 assistant bubble 上方，还是单独侧栏
4. 与 compaction 的交互（reasoning 文本是否计入压缩目标）

下一迭代决定是否实施。

### 测试

| 层级 | 用例 |
|---|---|
| 后端 unit | `test_narration_hint_phase1_no_destination` / `test_narration_hint_phase3_each_step` / `test_narration_hint_none_for_unrecognized_state` |
| 前端 unit | 收到带 hint 的 agent_status → ThinkingBubble 文案替换；手动收起 → localStorage 持久化 → 下次默认折叠 |
| E2E | 扩展 `e2e-waiting-experience.spec.ts` 文案快照 |

## 交付节奏

| PR | 后端 diff | 前端 diff | 依赖 |
|---|---|---|---|
| PR1（P0） | ~40 行 | ~60 行 | 无 |
| PR2（P1） | ~60 行（+24 工具各 1 行） | ~150 行 | PR1 事件基础设施 |
| PR3（P2） | ~30 行 | ~100 行 | PR2 agent_status 通道 |
| PR4（P3） | ~50 行 | ~30 行 | PR2 |

总计：后端 ≈ 180 行、前端 ≈ 340 行、测试 ≈ 200 行、文档 1 spec + 1 spike memo。

每个 PR 合入时同步更新 `PROJECT_OVERVIEW.md` § 9（前端架构）与 SSE 协议段落。

## 回归风险

| 风险 | 缓解 |
|---|---|
| 新 chunk type 下游未处理导致 SSE 断流 | `main.py` 兜底分支对未知 chunk 打日志 + 跳过，不中断流 |
| retry / continue 机制受影响 | PR1–4 不动 `error` / `done` / `can_continue` / `retryable` 字段；retry 专项 E2E 作为强制门禁 |
| `human_label` 中文 SSE 序列化出错 | `ensure_ascii=False` 已是默认；测试加中文断言 |
| Override state 与 state_update 乱序导致 tab 跳动 | Override `expiresAt` 800ms；state_update 到达时只清不回改 |
| Track A narration 和动作不符引发信任流失 | 文案锁定"意图方向"；规则覆盖不全返回 None，气泡退化为"思考中…" |
| Compaction 预告被错误触发 | 预判逻辑只复用 `compact_messages_for_prompt` 现成的 token 估算，不新增判定 |

## 可观测性

- 新事件在后端各挂 OTel span 属性：`phase_transition.from/to`、`agent_status.stage`，Jaeger 中可见
- 开发模式下前端把每个 SSE 事件打 `console.debug`（沿用 `useSSE` 现有风格，env flag 开关）

## 附录 A：工具 `human_label` 映射表

| 工具 | human_label |
|---|---|
| `update_plan_state` | 更新旅行计划 |
| `xiaohongshu_search` | 翻小红书找灵感 |
| `web_search` | 上网查资料 |
| `quick_travel_search` | 快速查行程价格 |
| `search_flights` | 检索航班 |
| `search_trains` | 检索火车 |
| `search_accommodations` | 检索住宿 |
| `get_poi_info` | 查 POI 详情 |
| `calculate_route` | 规划路线 |
| `assemble_day_plan` | 组装日程 |
| `check_weather` | 查天气 |
| `check_availability` | 查景点可用性 |
| `check_feasibility` | 核行程可行性 |
| `generate_summary` | 生成方案摘要 |

其余 10 个领域工具在 PR2 实施时按同样风格补齐（动作 + 对象、≤ 8 汉字、略带人格化）；新工具入库时 `@tool` 装饰器参数缺省会在 lint 阶段告警，确保不漏对齐。

## 附录 B：ThinkingBubble 生命周期状态机

```
[用户点击发送]
    │
    ├─→ 立即插入 ThinkingBubble（本地触发，不等后端）
    │
[2s 超时]  → 文案切"正在连接…"
    │
[agent_status.thinking]  → 保持（仅作后端确认）
[agent_status.summarizing]  → 文案切"汇总中…"
[agent_status.compacting]  → 文案切"整理上下文中…"
[agent_status.hint 非空]  → 文案切 hint 内容（PR4）
    │
    ├─→ [text_delta 到达]  → fade out 200ms, assistant bubble 接替
    ├─→ [tool_call 到达]   → fade out 200ms, 工具条替代
    └─→ [error 到达]       → fade out, streamFeedback 接管
```
