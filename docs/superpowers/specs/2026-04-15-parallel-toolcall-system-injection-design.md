# 并行 tool_calls 场景下 system 消息错位注入的修复设计

- 日期：2026-04-15
- 范围：backend（`main.py` 内的 `on_tool_executed` 回调与 `on_before_llm` 钩子）
- 关联 Bug：`sess_47fff269f429` 香港会话，前端报错 "本轮生成未完成，请调整后重新发送 / 连接阶段：模型返回格式异常"

## 1. 问题陈述

### 1.1 现象

前端在 "香港两日游/住深圳" 这一轮对话中报错：

- 主文案：`本轮生成未完成，请调整后重新发送。`
- 详情：`连接阶段：模型返回格式异常，请重试或切换模型。`

后端日志：

```
openai.APIError: Xunfei request failed with ... status code: 400
Inference failed: request param validation error, Value error,
Invalid messages at index 10. Messages with role 'tool' must be a
response to a preceding message with 'tool_calls'.
```

### 1.2 根因

对照 `backend/data/sessions.db` 中 `sess_47fff269f429` 的消息序列，`seq 7` 是一条包含 3 个并行 `tool_calls` 的 `assistant`：

| seq | role | 说明 |
|---|---|---|
| 7 | assistant | tool_calls = [dates, travelers, constraints] |
| 8 | tool | call_23912ff120… (dates) ✓ |
| 9 | **system** | `[实时约束检查] 香港建议至少2天...` ← **违规插入** |
| 10 | tool | call_b6459b01f8… (travelers) ← 网关报的 index 10 |
| 11 | tool | call_c467e9db3a… (constraints) |

OpenAI 协议要求 `assistant.tool_calls` 后面必须**连续**跟完整的 `tool` 答复序列，中间不能插入其它 role。讯飞 OpenAI 兼容网关按此严格校验，在 index 10 看到 `tool` 之前是 `system`（而不是带 tool_calls 的 assistant），返回 HTTP 400。`openai_provider._classify_error` 将该异常兜底成 `LLMErrorCode.PROTOCOL_ERROR + failure_phase="connection"`，前端据此渲染成上述文案。

### 1.3 Bug 代码位置

`backend/main.py:459-468`，在 `on_tool_executed` 类回调中：

```python
if errors:
    if session:
        session["_pending_validation_errors"] = errors
        session["messages"].append(
            Message(role=Role.SYSTEM,
                    content="[实时约束检查]\n" + "\n".join(f"- {e}" for e in errors))
        )
```

该回调对每个工具执行完都会触发一次。当 LLM 一次性发起多个并行 `tool_calls`（如本例 3 个 `update_plan_state`），工具执行器依次回调，这条 `session["messages"].append(SYSTEM)` 会在"第一个工具 tool 答复"与"其余工具 tool 答复"之间被夹入，破坏了协议序列的原子性。

### 1.4 风险范围澄清

项目内 `[状态同步提醒]`（`backend/agent/loop.py:695-854`）曾一度被怀疑是同类风险。但实际查证其调用点（`loop.py:204-219`）前置条件是 `if not tool_calls:`——只在"LLM 本轮没有发起任何工具调用、只回纯文本"的路径上 append，**不可能落在并行 tool_calls 组中间**，协议上永远合法，不在本次修复范围内。

`backend/main.py:541` 的 `[对话摘要]` 属于 `on_before_llm` 内部 context manager 压缩产物，不经过 `session["messages"].append`，同样安全。

因此本次修复**只改一处**：`main.py:459-468` 的实时约束检查注入点。

## 2. 设计目标

1. **保留并行 tool_calls 能力**：不回退到强制单工具；这是主流模型能力与项目既有设计方向。
2. **保证协议原子性**：任何发往 LLM 的 `messages` 中，`assistant.tool_calls` 后面必须连续紧跟它的全部 `tool` 答复，中间无其它 role。
3. **最小影响面**：只改造本次踩坑的唯一触发路径（`main.py:459-468` 实时约束检查），不动 `loop.py` 中 reflection/repair/状态同步提醒等在 tool_calls 组外部触发的 system 注入（见 1.4）。
4. **单一 flush 点**：把"何时写消息"的决定从易出错的"事件触发"挪到必然正确的"调用前合拢点"，从"检测错位"升级为"消除错位"。

## 3. 非目标

- 不处理历史数据：`backend/data/sessions.db` 中已存在的错位消息序列（如 `sess_47fff269f429`）不迁移、不矫正，用户需手动新建会话。
- 不改进 `openai_provider._classify_error` 的错误分类质量（400 被误分为 PROTOCOL_ERROR 的副作用 bug 已记入 `docs/TODO.md` 第 2 条，单独处理）。
- 不统一重构 `loop.py` 中 reflection/repair/quality_gate 等其它 system 注入点，它们不在并行 tool 序列中触发，当前安全。

## 4. 架构

### 4.1 核心思路

把"工具执行后可能注入的 system 消息"从**立即 append 到 messages**，改成**写入 pending 缓冲区**；在下一次调用 LLM 前由**唯一的 flush 点**（`on_before_llm`）统一 append 到 `messages` 末尾。

此时 tool_calls 组一定已写入完整，system 落点永远在整组 tool 之后、下一次 assistant 之前，协议上始终合法。

### 4.2 关键组件

| 组件 | 位置 | 职责 |
|---|---|---|
| `_pending_system_notes` | `session` 字典新增字段，`list[str]` | 按插入顺序缓存本轮待注入的 system 消息正文 |
| 写入方 | `main.py:459-468`（实时约束检查，唯一需要改的点） | 只向 pending 缓冲区追加，不直接 `session["messages"].append` |
| Flush 点 | `on_before_llm` 开头（`main.py:470` 附近） | 取出 pending，按序 append 到 `msgs` 末尾，清空缓冲区 |

### 4.3 不变的部分

- `_pending_validation_errors` 字段保留，及其现有消费方不动。
- `loop.py` 内 reflection / repair 等 system 注入保持原状。
- 工具并行执行能力保持。
- 会话持久化格式不变（pending 仅存在于运行时 session 字典，不落盘）。

### 4.4 并发模型

pending 缓冲区仅在 agent 单协程内被写入（tool 执行回调在 agent loop 内串行触发），无需加锁。

## 5. 具体改动点

### 5.1 session 初始化处新增字段

在 session 字典创建/加载的地方加一行：

```python
"_pending_system_notes": []
```

（Plan 阶段定位精确行号，搜 `sessions[session_id] = {` 或等价初始化点）

### 5.2 实时约束检查改为写缓冲区

位置：`backend/main.py:459-468`

改后：

```python
if errors:
    if session:
        session["_pending_validation_errors"] = errors
        session["_pending_system_notes"].append(
            "[实时约束检查]\n" + "\n".join(f"- {e}" for e in errors)
        )
```

### 5.3 新增统一 flush 点

位置：`backend/main.py:470`（`on_before_llm` 函数开头，`if not msgs: return` 之后紧跟）

```python
async def on_before_llm(**kwargs):
    msgs = kwargs.get("messages")
    if not msgs:
        return
    pending = session.get("_pending_system_notes") if session else None
    if pending:
        for content in pending:
            msgs.append(Message(role=Role.SYSTEM, content=content))
        session["_pending_system_notes"] = []
    tools = kwargs.get("tools") or []
    ...
```

### 5.4 消息形式

多条 pending 按**各自独立一条 system 消息**写入（不合并）。原因：

- 不同来源（约束检查 / 状态同步）语义不同，合并后 LLM 处理更糊
- 保留独立 entry 便于调试与日志定位
- 每条内容都短，多一条 system header 的 token 开销可忽略

## 6. 数据流

```
用户发消息
  ↓
agent loop 调 LLM → 返回 assistant(tool_calls=[A,B,C])
  ↓
append assistant 到 messages
  ↓
依次/并行执行 A, B, C
  每个执行完：
    ├─ append tool(result) 到 messages    ← 主循环
    └─ 触发 on_tool_executed              ← 回调
         └─ 如有 errors → push 到 _pending_system_notes
  ↓
一轮工具全部跑完 → agent loop 准备再次调 LLM
  ↓
on_before_llm
  ├─ flush _pending_system_notes → 按序 append system 到 msgs 末尾
  └─ 原有 compact_messages_for_prompt 等逻辑
  ↓
发送给 LLM（messages 形如 ... assistant(tc) → toolA → toolB → toolC → system[约束] → system[同步提醒]）
  ↓
LLM 正常返回 ✅
```

## 7. 不变量（Invariants）

修复后，系统始终满足：

1. 任何发往 LLM 的 `messages` 中，`assistant.tool_calls` 后面紧跟的必须是其全部 `tool` 答复（顺序对应），中间无其它 role。
2. `_pending_system_notes` 的内容**只**在 `on_before_llm` 里被消费。
3. `session["messages"]` 在工具执行阶段只会被主循环追加 `tool` 消息，不会被回调追加 `system` 消息。

## 8. 边界情况

| 场景 | 行为 |
|---|---|
| 工具全部跑完但无错误 | pending 为空，flush 是空操作 |
| 某个工具抛异常中断 | agent loop 终止；pending 保留于 session；下次调 LLM 时 flush —— 等价于"上次没说完的提醒这次带上"，合理 |
| 用户在工具执行中途取消 | agent loop 中断；pending 保留；若用户重发消息，flush 随下一轮调 LLM 生效 |
| 会话被持久化后重新加载 | pending 不落盘，重载后为 `[]`；未 flush 的提醒被丢弃，可接受 |
| 同一条提醒一轮内被触发两次 | 按当前代码不会发生；即使发生也是两条独立 system 入列，LLM 读作两次强调，无害 |
| 连续多轮对话 | 每轮 flush 后缓冲区清空，下一轮重新累积，互不干扰 |

## 9. 测试计划

按 Section 4 选定的 B 方案：**单元 + 集成**。

### 9.1 单元测试

新增 `backend/tests/test_pending_system_notes.py`：

| 用例 | 断言 |
|---|---|
| `test_pending_buffer_starts_empty` | 新建 session 后 `_pending_system_notes == []` |
| `test_validation_error_pushes_to_buffer_not_messages` | 触发 errors → messages 不变，buffer 含一条 `[实时约束检查]` |
| `test_state_sync_reminder_pushes_to_buffer` | 触发状态同步提醒 → buffer 含对应内容，messages 不变 |
| `test_multiple_errors_keep_separate_entries` | 两次触发 → buffer 里 2 条独立 entry |
| `test_on_before_llm_flushes_buffer_in_order` | buffer 预置 2 条 → 调 on_before_llm → msgs 末尾按序追加 2 条 SYSTEM，buffer 清空 |
| `test_on_before_llm_flush_is_idempotent_when_empty` | buffer 为空 → on_before_llm 不 append、不报错 |
| `test_flush_preserves_first_system_prompt` | msgs[0] 是 phase prompt，flush 后 msgs[0] 不变 |

### 9.2 集成测试

新增 `backend/tests/test_parallel_tool_call_sequence.py`：

- **场景 1**：mock LLM 返回并行 3 个 `update_plan_state` 且 validate 返回 errors → 断言下一轮发往 LLM 的 messages 中，`assistant(tool_calls=3)` 后紧跟 3 条连续 `tool`，然后才出现 `[实时约束检查]` system。
- **场景 2**：同上但 validate 返回空 → 断言 messages 不含 `[实时约束检查]`，tool 组完整连续。
- **场景 3**：连续两轮对话，第 1 轮触发 errors → flush → 第 2 轮不触发。断言第 3 次调 LLM 时 buffer 已清空。
- **场景 4**（回归）：mock LLM 返回单 tool_call → 断言行为与现状等价。

### 9.3 已有测试调整

`backend/tests/test_realtime_validation_hook.py:108` 那条断言（`[实时约束检查]` 出现在 messages 里）需要调整：现在 errors 触发时 messages 里暂不会出现，需先触发 errors、再调一次 `on_before_llm`，再断言内容出现。

### 9.4 人工验证

本地用 "香港两日游/住深圳" 等价场景（多字段并行 update_plan_state）复测一遍，确认前端不再出现 "连接阶段：模型返回格式异常"。作为 definition of done 的人工门。

## 10. 回归风险评估

| 风险 | 评估 |
|---|---|
| 现有 `_pending_validation_errors` 消费方受影响 | 不改该字段读写，额外加一个 notes 缓冲；风险低 |
| system 消息延迟一拍到达 LLM | 语义上本应如此（提醒基于"这组工具完成后的状态"），更正确 |
| 首条 system（phase prompt）被误动 | flush 是 append 到末尾，不触碰 `msgs[0]`；风险零 |
| 压缩逻辑交互 | flush 先于 `compact_messages_for_prompt`，若超预算统一处理，行为与现状一致 |

## 11. 后续跟进（不在本 spec 范围）

- `docs/TODO.md` 第 2 条：`openai_provider` 错误分类从 `APIError` 中恢复真实 HTTP 状态码，让前端错误文案更准确。
- 长期可考虑把 `loop.py` 里的 system 注入（reflection/repair 等）也统一到同一个 pending 管道，但当前它们不在并行 tool 序列中触发，暂不必要。
