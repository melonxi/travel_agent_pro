# Phase 5 “只承诺不动手”被静默 DONE 的复盘

- 事故日期：2026-04-18
- 问题 session：`sess_1767a894cc49`（北京 · 7天6晚）
- 事故范围：`backend/agent/loop.py`（`_build_phase5_state_repair_message` 与零 tool_call 处理路径）、`backend/phase/prompts.py:PHASE5_PROMPT`
- 事故类型：Phase 5 已写入部分天数后，LLM 连续 4 轮只回口头承诺、不调用写入工具；AgentLoop 没有兜底 repair，每轮直接 `yield DONE` 静默结束
- 事故等级：中
- 事故状态：已定位根因，待修复

---

## 1. 事故摘要

用户在 Phase 5 阶段已经走到 Day 6 写入完成（骨架是 7 天，`dates=2026-05-09 → 2026-05-15`，`total_days=7`），距离收尾只差「补回 Day 6 的傍晚什刹海调整」+「写入 Day 7」。

接下来 4 个 assistant 回复（seq 117 / 119 / 121 / 123）全部是只有文本、没有 `tool_calls` 的短承诺：

```
seq 117 assistant: "Day 6通过！最后Day 7（天坛+返程）。…什刹海移至Day 6傍晚，Day 7仅保留天坛及前门周边。"
seq 119 assistant: "好的，继续把最后两天落地！…什刹海移到Day 6傍晚（夜景更出片），Day 7只保留天坛+返程…"
seq 121 assistant: "继续！先更新Day 6加入什刹海傍晚，再写入Day 7。"
seq 123 assistant: "抱歉，马上一口气把Day 6更新和Day 7全部写入！"
```

每一轮 AgentLoop 因为 `tool_calls == []` 走到 `_build_phase5_state_repair_message`，repair 函数因为关键词集合不命中直接返回 `None`，于是 `yield DONE` 把这一轮当作「正常完成」结束。run 状态全部是 `completed`，不留任何错误，但 plan 永远停在 6/7 天，Day 7 写不进去。

---

## 2. 用户可见现象

- 用户连发 3 次「好的 / 继续 / 为什么又停了」，每次 assistant 只用一两句话答应「马上写」，状态栏什么也不动。
- 前端 SSE 收到的是正常 `text_delta + done`，没有错误，没有 retry 提示，也没有继续按钮——run_status 是 `completed`。
- 数据库 `daily_plans` 始终停在 6/7 天；`sessions.last_run_status` 一直是 `completed`，`last_run_error` 为空，看起来「一切都好」。

---

## 3. 关键证据

### 3.1 plan 状态

```text
phase = 5
dates.start = 2026-05-09
dates.end   = 2026-05-15
total_days  = 7  (DateRange.total_days inclusive 语义)
selected_skeleton_id = photo_flower (skeleton 共 7 天)
daily_plans = [Day1..Day6]  # planned_count = 6
```

### 3.2 数据库消息流（`sess_1767a894cc49`）

| seq | role | tool_calls | content（截断） |
|-----|------|-----------|----------------|
| 116 | tool | – | `append_day_plan` 成功，Day 6 / activity_count=6 |
| 117 | assistant | **[]** | `"Day 6通过！最后Day 7（天坛+返程）…什刹海移至Day 6傍晚…"` |
| 118 | user | – | `"好的"` |
| 119 | assistant | **[]** | `"好的，继续把最后两天落地！…"` |
| 120 | user | – | `"继续"` |
| 121 | assistant | **[]** | `"继续！先更新Day 6加入什刹海傍晚，再写入Day 7。"` |
| 122 | user | – | `"为什么又停了"` |
| 123 | assistant | **[]** | `"抱歉，马上一口气把Day 6更新和Day 7全部写入！"` |

四个 assistant 回复全部 `tool_calls = []`，全部 `last_run_status = completed`。

### 3.3 AgentLoop 终止路径

`backend/agent/loop.py:202-226`：

```python
# If no tool calls, we're done — the LLM gave a final text response
if not tool_calls:
    full_text = "".join(text_chunks)
    repair_message = self._build_phase3_state_repair_message(...) \
        or self._build_phase5_state_repair_message(...)
    if full_text:
        messages.append(Message(role=Role.ASSISTANT, content=full_text))
    if repair_message:
        messages.append(Message(role=Role.SYSTEM, content=repair_message))
        continue
    yield LLMChunk(type=ChunkType.DONE)
    return
```

含义很明确：本轮无 tool_call → 全部依赖 repair 函数兜底；repair 不命中就直接 DONE 走人。

### 3.4 Phase 5 repair 触发条件

`backend/agent/loop.py:855-926`：

```python
def _build_phase5_state_repair_message(...):
    if current_phase != 5 or self.plan is None: return None
    if not self.plan.dates: return None
    if "p5_daily" in repair_hints_used: return None       # 同一 run 只触发一次
    if len(text) < 20: return None

    if planned_count >= total_days: return None           # 6 < 7 → 继续

    day_pattern_count = 命中 r"第\s*[1-9一二三四五六七八九十]\s*天|Day\s*\d|DAY\s*\d"
    has_time_slots    = 命中 r"\d{1,2}:\d{2}"
    has_activity_markers = any(k in text for k in
        ("活动","景点","行程","安排","上午","下午","晚上","餐厅"))
    has_json_markers  = ("\"day\"" / "\"date\"" / "\"activities\"" / "\"start_time\"" 命中 ≥ 2)
    has_date_patterns = 命中 r"\d{4}-\d{2}-\d{2}"

    if (
        (day_pattern_count >= 1 and (has_time_slots or has_activity_markers))
        or has_json_markers
        or (has_date_patterns and has_activity_markers)
    ):
        return "[状态同步提醒] …请立即调用 replace_daily_plans / append_day_plan…"
    return None
```

逐句对 seq 117 文本判定：

- `Day\s*\d` 命中（"Day 6/Day 7"）→ `day_pattern_count >= 1` ✓
- `\d{1,2}:\d{2}` 时间槽 → ✗（文本里没有任何 HH:MM）
- 关键词集合 ✗（文本只有「跨城 / 傍晚 / 返程 / 周边 / 调整」，**这些词都不在白名单里**）
- JSON 字段 ✗
- `YYYY-MM-DD` 日期 ✗

→ 三个 `or` 分支全部不命中 → `return None` → loop `yield DONE`。

seq 119 / 121 / 123 文本更短、更口语，同样不命中。`p5_daily` 这个 key 还做了「同一 run 只允许触发一次」的去重，意味着即便后续触发也会被去重锁住。

### 3.5 `repair_hints_used` 的额外锁

`repair_hints_used` 是 **per-run** 的 `set`（loop 顶部 `repair_hints_used: set[str] = set()`，run 内累加）。这意味着：即便修好了关键词检测，模型在同一次 run 多次连续只承诺，repair 也只会注入一次；之后再次零 tool_call 仍会静默 DONE。

### 3.6 Phase 5 prompt 现状

`backend/phase/prompts.py:550-665` 的 PHASE5_PROMPT 已经包含「不允许只在正文描述而不写状态」「每完成 1-2 天就写入」等纪律性条款，但纪律全部依赖 LLM 自觉；运行时没有任何门控会在「未写满 + 零 tool_call」时强制把球踢回模型。

---

## 4. 根因分析

### 4.1 直接根因：repair 关键词集合过窄

`_build_phase5_state_repair_message` 用「文本里出现 `\d{1,2}:\d{2}` 或 `活动/景点/行程/安排/上午/下午/晚上/餐厅`」作为「模型在描述行程而非闲聊」的判据。这个集合只能捕捉「LLM 输出完整逐日行程文本但忘了调用工具」的情况，无法捕捉「LLM 只输出一两句承诺式回复」。

承诺式回复在自然语言里是非常常见的失败模式（「好的，我马上写」/「继续！」），关键词检测天然漏掉。

### 4.2 上游诱因：repair 是「内容启发式」，不是「状态门控」

repair 函数把判断条件挂在「文本特征」上，而真正可靠的信号是状态层的硬数：`planned_count < total_days` + 本轮零 tool_call。这两个信号是确定的，不依赖 LLM 用什么口吻回复。

把状态信号让位给文本启发式，意味着只要 LLM 选择「沉默式偷懒」（少说话、说没特征的话），系统就漏过去。

### 4.3 上游诱因：`repair_hints_used` 单次锁导致「修了也只修一次」

`repair_hints_used` 防的是「同一轮重复注入 repair」，但 key 用的是 `"p5_daily"`，粒度过粗——整个 run 的所有 Phase 5 daily 异常共用一个 key。一旦修好检测、第一次 repair 注入后，模型仍然只承诺不动手，repair 不会再次注入，loop 还是 DONE。

### 4.4 下游放大因素：零 tool_call → DONE 的默认语义

`loop.py:225` 的 `yield DONE` 把「LLM 无 tool_call」默认理解为「任务完成」。在 Phase 5 这个阶段，这个默认语义是错的：Phase 5 只要 `planned_count < total_days`，无 tool_call 就一定不是完成。该默认语义在 prompt 纪律 + repair 启发式都失效时，没有最后一道防线。

### 4.5 下游放大因素：run_status 静默 completed，可观测性失真

每次零 tool_call 退出后，run_status 被标记为 `completed`、`last_run_error = NULL`。前端没有「继续」按钮、没有错误条；trace 里 `tool_call_count = 0`，看起来正常。用户是唯一知道「卡住了」的人——只能通过自然语言反复催促。直到用户放弃。

---

## 5. 与 2026-04-18 早些事故的关系

同一天的 `2026-04-18-phase5-repair-read-state-and-provider-routing.md` 记录的是 **Phase 5 已写满（7/7）后修改任务** 失败，关键症状是模型自造 `get_trip_info` 并被 provider 透传成 XML。两次事故都暴露同一个抽象漏洞：**Phase 5 repair 只盯「未写满」并且只看文本特征**。

| 维度 | 4-18 早些事故 | 本次事故 |
|------|----------------|----------|
| 触发条件 | `planned_count == total_days` 且用户要求修改 | `planned_count < total_days` 且 LLM 只承诺 |
| repair 失效原因 | `planned_count >= total_days` 提前 return | 关键词集合不命中 → return None |
| 可见症状 | 裸 XML 工具协议泄漏到 UI | 文本承诺 + 静默 DONE，状态不动 |
| run_status | completed | completed |
| 修复方向 | 加裸 XML 拦截 + 修改型任务门控 + 只读工具 | 改用状态硬信号兜底 + 解锁 repair_hints_used |

两次都说明：**当前 Phase 5 修复机制对「LLM 选择沉默或降级输出」缺乏防线**。

---

## 6. 影响评估

### 6.1 用户影响

- 体验断裂：用户能感觉「又停了」，但系统给不出任何错误提示或继续按钮，只能反复催。
- 任务残缺：plan 留在 6/7 天，没有 Day 7，前端 Map / Timeline / 摘要全部不完整。
- 信任损失：用户问「为什么又停了」是直接的信任质询。

### 6.2 系统影响

- run_status 错报为 completed，掩盖真实失败。
- repair 机制存在「按文本特征兜底」的设计错误，留下后续 prompt-injection 与启发式失效的长期风险。
- 同一漏洞在已写满 / 未写满两条路径上以不同症状反复出现，说明需要的是结构性补丁，而不是再加一个关键词。

---

## 7. 修复建议

### 7.1 P0：用状态硬信号替换文本启发式作为兜底

在 `_build_phase5_state_repair_message` 里增加「无条件兜底分支」：

```python
# 状态硬信号兜底（最高优先级）：未写满且本轮零 tool_call，必触发
if planned_count < total_days and len(text) < 400:
    remaining = total_days - planned_count
    return (
        "[状态同步提醒]\n"
        f"daily_plans 当前 {planned_count}/{total_days} 天，仍缺 {remaining} 天。"
        "本轮你没有调用任何写入工具——禁止只用自然语言承诺。"
        "请立即调用 `append_day_plan(...)` 追加下一天，"
        "或调用 `replace_daily_plans(days=[...])` 整体替换。"
    )
```

理由：

- 文本特征是猜模型意图，状态字段是事实。
- `len(text) < 400` 用来避开「模型输出完整 JSON 但漏掉 tool wrapper」之类的现有分支冲突，可以按需调整或拆条件。
- 写满后（`planned_count >= total_days`）仍走原路径，不影响其他场景。

### 7.2 P0：解锁 `repair_hints_used`，按「状态进展」决定是否重置

把 `repair_hints_used` 的去重粒度从「整个 run 一次」改成「同一 planned_count 一次」：

```python
repair_key = f"p5_daily:{planned_count}"
```

含义：只要 daily_plans 没有任何进展（planned_count 没变），repair 提示在同一轮 run 多次零 tool_call 时仍然能持续注入；一旦写入新天数，key 自然变化，提示重置。

### 7.3 P0：零 tool_call 在「未完成阶段」不应自动 DONE

修改 `backend/agent/loop.py:202-226` 的默认语义：在 Phase 5 且 `planned_count < total_days` 的情况下，即便所有 repair 都不命中，也要：

- 把 `last_run_status` 标为 `incomplete` / `needs_continue`
- 通过 SSE 给前端发出「继续」按钮触发条件（参考现有 `can_continue` 通道）
- 不要把这一轮当成 completed 落库

这样即便兜底 repair 没注入或未来再被绕过，用户至少能拿到「继续」入口，而不是只能复读「为什么又停了」。

### 7.4 P1：扩 Phase 5 prompt 的「禁口头承诺」条款

在 `PHASE5_PROMPT` 的「状态写入契约」段最前面加一行硬指令：

```text
- 禁止单回合内只用自然语言承诺「马上写 / 继续 / 接下来更新」而不附带 tool_call。
  未写满 daily_plans 之前，每一回合都必须以 append_day_plan / replace_daily_plans 收尾。
```

这是 prompt 层的最后兜底，配合上面 P0 的运行期门控形成双保险。

### 7.5 P1：Trace 标记「promise-only」类零 tool_call 失败

当一轮零 tool_call 且阶段未完成时，trace/stats 应记录：

- `promise_only_termination: true`
- `phase`、`planned_count`、`total_days`、`assistant_text_len`
- `repair_injected: true/false`、`repair_key`

调试时一眼就能看出「这一轮其实是空转」，避免再次出现「`tool_call_count=0`、`errors=[]`、`last_run_status=completed`」的假正常。

### 7.6 P2：把启发式归一化为「状态层规则」

中长期上，建议抽出 `state_completeness_check(plan, phase) -> Optional[RepairHint]` 这一层，统一回答「在当前 phase 下，plan 是否还差什么」：

- Phase 1：`destination` 是否就绪
- Phase 3：四个子阶段各自缺哪个字段
- Phase 5：`daily_plans` 是否覆盖 `total_days`、是否含严重冲突
- Phase 7：检查清单是否解决高优先级项

零 tool_call 的兜底分支统一咨询这一层，而不是各自维护一套关键词。两次 4-18 事故的根因都能由这层一次性根除。

---

## 8. 建议回归测试

### 8.1 promise-only 兜底触发测试

构造 Phase 5 plan：`daily_plans = 6`、`total_days = 7`。模拟 LLM 返回纯文本短承诺（如 `"好的，马上写入Day 7"`，不带 tool_call）。

断言：

- AgentLoop 不直接 `yield DONE`，而是注入兜底 repair。
- 注入的 system message 包含 `daily_plans 当前 6/7 天` 这种状态描述。
- run_status 不被标为 `completed`。

### 8.2 同一 planned_count 多轮重复触发测试

同上场景，连续 3 轮 LLM 都返回承诺式短文本。

断言：

- 每一轮都有 repair 注入（按 `planned_count = 6` 这个 key 不去重）。
- 一旦模拟 LLM 真正调用 `append_day_plan` 推进到 `planned_count = 7`，repair 不再注入，run 才真正 DONE。

### 8.3 关键词扩展回归

模拟 LLM 输出含「傍晚 / 返程 / 跨城」但不含旧白名单关键词的描述性文本。

断言：兜底分支仍能注入 repair，不依赖关键词命中。

### 8.4 阶段未完成 → SSE 给出可继续信号

在 8.1 的场景下，断言 SSE 最后一帧 `done` 携带 `can_continue = true` 或等价标志，前端能渲染「继续」按钮。

### 8.5 写满后行为不回归

构造 `daily_plans = 7/7` 的 plan，LLM 返回纯文本无 tool_call。

断言：兜底分支 **不** 触发（因为 `planned_count >= total_days`），保持现有 4-18 早些事故的处理路径不被本次修复污染。

---

## 9. 事故定性

本次事故是 **Phase 5 兜底机制的「文本启发式 vs 状态硬信号」抽象错误**：

- 真正可靠的失败信号是 `planned_count < total_days` + 本轮零 tool_call。
- 当前实现却用关键词检测去猜模型意图，模型只要「少说话 / 说没特征的话」就能从兜底里溜走。
- 一旦溜走，零 tool_call 的默认语义（DONE）把空转伪装成完成，对外是 `last_run_status = completed`，对内是 plan 永远卡在 6/7 天。
- 用户只能反复催促「继续 / 为什么又停了」，系统没有任何机制把这种催促升级为状态推进。

修复重点不是再加关键词，而是把 Phase 5 兜底改建在状态层（`planned_count` vs `total_days`）上，让「未完成阶段 + 零 tool_call」永远不会被静默归为 completed。
