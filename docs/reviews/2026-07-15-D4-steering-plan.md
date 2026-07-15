# D4:Steering(运行中引导)施工蓝图(2026-07-15)

> 源自 `docs/reviews/2026-07-14-agent-reliability-action-plan.md` 的 **D4**。
> 此前架构对比文档已评为 **P0 体验项**(用户长 run 中只能等完或取消)。
> 配套文档:`2026-07-15-D3-phase3-renegotiation-plan.md`(D4 与 D3 配合价值最大)。

## 1. 一句话目标

给进行中的长 run(尤其 Phase 3 并行精排)一条**中途纠偏通道**:用户看到"第 3 天不对"时,不必取消整轮重来,而是发一条 steering 消息(如"第 3 天别排太满""跳过筑地市场"),Agent 在下一个安全点消费它并调整,run 不中断。

## 2. 为什么做(问题回顾)

- Phase 3 并行编排是长 run(多 worker × 每 worker 可到 `worker_timeout_seconds`)。当前用户对一个进行中的 run **只有两个动作:等它完 / 取消它**。
- 取消是核弹级的:整轮成果丢弃(虽有 P0-2 部分交付兜底,但用户主动取消不触发)。
- 缺一条"run 不停、只微调"的中间通道。这正是 steering。

## 3. 现状锚点(施工基线,均已核实 file:line)

### 3.1 长 run 入口与 drain 注入点

- `backend/agent/loop.py:363` — `AgentLoop.run()`,内层主循环(`for iteration in range(self.max_iterations)`)。
- `backend/agent/loop.py:389-399` — **Phase 3 分流**:`should_enter_parallel_phase3_now` 为真时,`async for chunk in self._run_parallel_phase3_orchestrator(...): yield chunk` 然后 **`return`**。
- `backend/agent/execution/llm_turn.py:123-128` — `run_llm_turn` 开头即 `await hooks.run("before_llm_call", ...)`。**这是 D4 设想的 drain 注入点**,且 P2-5 的 pending note flush 已挂在这个 hook 上(见 3.4)。

**关键约束**:Phase 3 orchestrator 一旦进入(loop.py:394),是**一次性 `async for ... yield ... return`**——主循环在 orchestrator 跑完前**不会回到 `run_llm_turn` 的 drain 点**。因此:
- **非 Phase 3 的普通迭代**:在 `run_llm_turn` 开头 drain 即可(每次 LLM turn 前)。
- **Phase 3 长 run 期间**:必须在 **orchestrator 的 worker 收集循环内**额外插 drain 检查点,否则 steering 消息要等到整个 orchestrator 跑完才被消费,失去"中途"意义。

### 3.2 现有取消/停止机制(steering 的直接模板)

- `backend/agent/loop.py:95` — `AgentLoop.run(..., cancel_event: asyncio.Event | None = None)`。
- `backend/agent/loop.py:401` — 每次迭代顶 `self._check_cancelled()`。
- `backend/api/orchestration/chat/stream.py:72` — `run_agent_stream(..., cancel_event, ...)`。
- `backend/api/orchestration/chat/stream.py:87` — `except asyncio.CancelledError`。
- `backend/api/orchestration/chat/stream.py:395` — `session.pop("_cancel_event", None)`(**cancel_event 挂在 session dict 上**)。
- `backend/api/routes/chat_routes.py:344-352` — `/api/chat/{session_id}/cancel` 端点:`session.get("_cancel_event").set()`。

**这套机制就是 D4 的同构模板**:跨请求信号(cancel_event)已经通过 session dict 传递,steering queue 照抄即可。

### 3.3 API 端点与 session 载体

- `backend/api/routes/chat_routes.py:185-186` — `/api/chat/{session_id}` 主聊天端点。
- `backend/api/routes/chat_routes.py:344` — `/cancel`;`:354` — `/continue`。**`/steer` 与它们并列新增。**
- `chat_routes.py` 的 `sessions` dict 是**进程内 session registry**(`sessions.get(session_id)`),session dict 上已挂 `_cancel_event`、`_current_run`、`agent`、`plan`、`messages`。**steering queue 挂在同一个 session dict 上,与 cancel_event 完全同构。**

### 3.4 runtime 消息注入机制(P2-5 已建好,直接复用)

- `backend/api/orchestration/session/pending_notes.py` — `push_pending_system_note` / pending note 机制。
- P2-5 刚把 soft judge / feasibility / hard_constraint / quality_gate 四处反馈统一改为 `push_pending_system_note`,在 `on_before_llm`(即 `before_llm_call` hook)安全点 flush,**禁止直接 `active_runtime_messages.append`**(`docs/agent/slices/data-flow.md` 已记此不变量)。

**steering 消息可以直接复用这条 pending note 通道**:`/steer` 端点把用户消息 push 成 pending system note(或专用 steering note),drain 点 flush 进 runtime。安全性(不插进 tool_calls 与 tool 响应之间)由现有机制保证。

## 4. 目标设计

### 4.1 数据流

```text
用户(run 进行中)
   │ POST /api/chat/{session_id}/steer  {"text": "第 3 天别排太满"}
   ▼
chat_routes:/steer
   │ session["_steer_queue"].put_nowait(SteerMsg(text, ts))
   ▼
(进行中的 run,两类 drain 点)
   ├── 普通迭代:run_llm_turn 开头 before_llm_call → drain queue → push_pending_system_note → flush
   └── Phase 3 长 run:orchestrator worker 收集循环内 → drain queue → 影响后续 worker 派发/约束
   ▼
SSE 回传 steering_ack 事件(前端显示"已收到,将在下一步调整")
```

### 4.2 组件清单

**C1. Steering 队列(挂 session dict)**

- `run_agent_stream`(stream.py)启动 run 时,`session["_steer_queue"] = asyncio.Queue()`,与 `_cancel_event` 并列;run 结束时 `session.pop("_steer_queue", None)`(对齐 stream.py:395)。
- 传入 `AgentLoop.run(..., steer_queue=...)`,与 `cancel_event` 并列参数(loop.py:95 同款)。

**C2. `/steer` 端点(chat_routes.py,照抄 /cancel)**

```python
@app.post("/api/chat/{session_id}/steer")
async def steer_chat(session_id: str, req: SteerRequest):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    queue = session.get("_steer_queue")
    if queue is None:
        raise HTTPException(status_code=409, detail="No active run to steer")
    queue.put_nowait({"text": req.text, "ts": ...})
    return {"status": "accepted"}
```

**C3. Drain helper(复用 pending note)**

新增 `_drain_steer_queue(queue) -> list[str]`:非阻塞 drain 所有已入队消息。每条 → `push_pending_system_note(session, f"[用户运行中引导] {text}")`。flush 由现有 `before_llm_call` 机制完成——**零新增注入路径**。

**C4. 两个 drain 检查点**

- **普通迭代**:`run_llm_turn` 开头(llm_turn.py:123 之前),drain → push pending note。当前迭代的 LLM 立刻看到。
- **Phase 3 orchestrator**:在 worker 收集循环(`orchestrator.py:911+` 之后的收集处)每收一个 worker 结果后 drain 一次。steering 影响**尚未派发/正在重试**的天(如"第 3 天别排太满"→ 给第 3 天 worker 追加约束或触发该天重派)。已完成的天不回滚。

**C5. SSE ack 事件**

drain 到 steering 消息时 `yield` 一个 `steering_ack` chunk(复用现有 chunk 机制,如 AGENT_STATUS 或新 ChunkType),前端提示"已收到引导,将在下一步调整"。

### 4.3 与 D3 的配合(价值最大化点)

D3 让 Orchestrator 能"改单天骨架 + 只重派受影响天";D4 让用户能在 run 中"喊话某一天"。二者叠加:

- 用户看到第 3 天进度不对 → `/steer "第 3 天别去太远的地方"` → drain 点把它转成第 3 天的追加约束 → 触发 D3 的单天重派(而非整轮重来)。
- 没有 D3:steering 只能作为 pending note 喂给下一个普通 LLM turn(Phase 3 内影响有限)。
- 没有 D4:D3 的再协商用户看不到、插不上手。

**因此建议 D3 先行或并行**;D4 单独上线也有价值(普通迭代的 steering),但 Phase 3 内的完整威力依赖 D3。

## 5. 分期施工步骤

### 阶段 1:普通迭代 steering(不依赖 D3,可独立上线)

1. `stream.py` 启动 run 时建 `session["_steer_queue"]`,传入 `AgentLoop.run`。
2. `chat_routes.py` 加 `/steer` 端点 + `SteerRequest` model(照抄 /cancel)。
3. 新增 `_drain_steer_queue` helper,`run_llm_turn` 开头(llm_turn.py:123 前)drain → `push_pending_system_note`。
4. run 结束清理 `_steer_queue`(对齐 stream.py:395 的 cancel_event 清理)。
5. SSE `steering_ack` 事件 + 前端最小接线(输入框在 run 进行中可发 steering)。

### 阶段 2:Phase 3 长 run steering(与 D3 配合)

6. `orchestrator.run` 接收 `steer_queue`,worker 收集循环内每步 drain。
7. steering 消息映射到"某天追加约束 / 触发该天重派"——**复用 D3 的单天重派通道**(若 D3 未上线,退化为 pending note 喂给 handoff 后的 LLM turn)。
8. SSE 进度里标注"因用户引导调整了第 N 天"。

## 6. 测试策略

- **单元**:`_drain_steer_queue` 非阻塞 drain 多条、空队列不阻塞;`/steer` 端点(无 active run → 409,有 → 202/accepted)。
- **集成(普通迭代)**:注入"run 中途 put steering 消息" → 断言下一个 LLM turn 的 runtime messages 含该 pending note;断言不插进 tool_calls 与 tool 响应之间(复用 P2-5 的 pending note 安全性测试思路,见 `test_hooks_pending_notes_feedback.py`)。
- **集成(Phase 3)**:注入"orchestrator 跑到第 2 天时 put '第 3 天别排太满'" → 断言第 3 天 worker 收到追加约束 / 触发重派,已完成的第 1、2 天不回滚。
- **并发安全**:`/steer` 与主 run 并发(同 session)→ 断言 queue 无竞态、session dict 访问安全(与现有 cancel_event 同并发模型,无新锁需求)。
- **不回归**:无 steering 消息时 run 行为完全不变(drain 到空队列即 no-op)。

## 7. 风险与开放问题

- **R1:Phase 3 内的 steering 语义边界**。"第 3 天别排太满"要转成机器可用的约束需要解析。**折中**:阶段 2 先只支持"重派第 N 天 + 附上用户原文作 repair_hint",不做复杂 NLU;worker 拿到原文自行理解。完整结构化(如 D3 的 SUGGEST_MOVE)是后续。
- **R2:已完成天不可回滚**。steering 只影响未派发/正在跑的天。若用户想改已完成的天,退回到常规 backtrack 通道(P1-3 已选择性清除),不在 D4 范围。文档需向用户明示这一边界。
- **R3:queue 生命周期**。必须与 run 生命周期严格绑定(run 起建、run 终清),否则跨 run 的陈旧 steering 消息会污染下一轮。对齐 stream.py:395 的 `_cancel_event` 清理时机。
- **R4:steering 洪水**。用户狂发 steering → queue 堆积。drain 时可做去重/限流(如同一 run 内保留最近 N 条),`log` 丢弃的条数,不静默吞。
- **R5:与 P2-5 pending note 的优先级**。steering note 与 gate 反馈都走 pending note 通道,flush 顺序需明确(建议 steering 优先,因为是用户显式意图)。实施时复核 `pending_notes.py` 的 flush 顺序语义。

## 8. 与其他项的关系

- **前置**:P2-5(pending note 机制)是 D4 的注入地基;cancel_event 机制是 D4 的跨请求信号模板。二者均已完成。
- **配合**:D3(单天重派)——D4 阶段 2 的 Phase 3 steering 依赖 D3 的单天重派通道才能发挥完整威力(见 §4.3)。
- **独立价值**:D4 阶段 1(普通迭代 steering)不依赖 D3,可独立上线。
