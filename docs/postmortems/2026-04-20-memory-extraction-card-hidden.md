# Memory Extraction 卡片不显示事故复盘

- 事故日期：2026-04-20
- 问题 session：`sess_0cf69bb370e5`、`sess_ce81931fed5d`
- 事故范围：`backend/main.py`、`frontend/src/components/ChatPanel.tsx`
- 事故类型：后台 internal task 可见性断链 + React StrictMode state updater 副作用
- 事故等级：中
- 事故状态：已修复

---

## 1. 事故摘要

用户测试记忆提取时发现：前端聊天流里没有显示 `memory_extraction` 卡片，但打开记忆管理又能看到记忆已经提取成功。

本次排查确认后端后台任务链路本身能跑通，`memory_extraction_gate` 和 `memory_extraction` 都会进入 `/api/internal-tasks/{session_id}` 快照，也能通过 background SSE 重放；真正导致卡片不显示的是前端 `ChatPanel` 的 upsert 逻辑在 React StrictMode 下被 state updater 重放时吞掉了新卡片。

---

## 2. 用户可见现象

用户发送明确长期偏好，例如：

```text
请帮我记住：我以后旅行长期偏好住安静酒店，不吃辣，也不喜欢红眼航班。这些请作为长期偏好保留。
```

实际表现：

1. 聊天流里能看到 `memory_recall`、真实工具卡和 assistant 回复。
2. 聊天流里看不到 `memory_extraction_gate` / `memory_extraction` 系统内部任务卡。
3. 记忆管理面板里能看到新增或待确认记忆，说明提取和写入并没有完全失败。

---

## 3. 影响评估

### 3.1 用户影响

- 用户无法确认“记忆提取正在发生 / 已成功 / 已跳过 / 已失败”。
- 记忆管理出现新条目但聊天流没有卡片，造成“后端偷偷写了记忆”的不透明感。
- 若提取超时或失败，用户只能在后端日志里间接发现，前端没有即时反馈。

### 3.2 系统影响

- background internal-task SSE 与聊天 UI 的合并契约被破坏。
- 后端原有日志不足以快速判断事件卡在哪一层：任务是否提交、是否发布、是否有订阅者、是否投递成功都缺少边界日志。
- 开发环境 React StrictMode 放大了 updater 副作用，但这类副作用本身也会让状态逻辑变脆。

---

## 4. 排查过程

### 4.1 初始检查

先检查 `.codex-run/backend.log`，没有看到可用的 `memory_extraction` 生命周期日志。代码中虽然已有部分“记忆提取开始调用模型 / 完成 / 超时”日志，但缺少以下边界信息：

- 用户消息进入后是否提交 memory job snapshot。
- 后台任务是否发布到 `memory_active_tasks`。
- 发布时是否存在 SSE subscribers。
- SSE 队列是否投递成功。
- `/api/internal-tasks/{session_id}` 快照是否能列出任务。
- `/api/internal-tasks/{session_id}/stream` 是否重放已有任务。

因此先在后端补充诊断日志，再用 Playwright 复现。

### 4.2 后端证据

Playwright 发送偏好消息后，直接拉取：

```text
GET /api/internal-tasks/sess_0cf69bb370e5
```

返回里可以看到：

```json
{
  "tasks": [
    {
      "kind": "memory_extraction_gate",
      "status": "success"
    },
    {
      "kind": "memory_extraction",
      "status": "warning"
    }
  ]
}
```

同时手工打开 background SSE：

```text
GET /api/internal-tasks/sess_0cf69bb370e5/stream
```

也能收到 `data: {"type": "internal_task", ...}` 格式的重放事件。

这说明后端不是“没有产生 memory_extraction 事件”，而是前端没有把这些后台事件稳定渲染成卡片。

### 4.3 前端证据

临时加入浏览器 console 探针后，发现：

```text
[internal-task-ui] upsert memory_extraction_gate ... success
[internal-task-ui] upsert memory_extraction ... warning
[internal-task-ui] setMessages prev 0 ...
[internal-task-ui] insert ... nextLength 1
[internal-task-ui] setMessages prev 0 ...
```

这段输出非常关键：`upsertInternalTaskMessage()` 明明被调用了，也进入了插入路径，但最后 DOM 中 `.system-internal-task` 仍然是 0。

进一步观察发现，React StrictMode 在开发环境会重复调用 state updater。旧逻辑在 updater 内部先写入：

```ts
internalTaskMessageIdsRef.current.set(task.id, messageId)
```

第一次 updater 调用尚未真正提交消息列表，第二次 updater 调用已经能从 ref 里拿到 `mappedMessageId`，于是误以为卡片存在，进入“更新已有消息”的路径；但当前 `prev` 里仍找不到对应 message，最终没有插入新卡片。

---

## 5. 根因分析

### 5.1 直接根因

`ChatPanel.upsertInternalTaskMessage()` 把 `internalTaskMessageIdsRef` 当成了“消息已经存在”的权威依据，但这个 ref 是在 React state updater 内部写入的。

在 React StrictMode 下，updater 可能被重复执行；第二次执行时 ref 已经有映射，而 React 当前传入的 `prev` 仍可能没有对应消息。旧逻辑没有验证 mapped message 是否真的存在于 `prev`，导致新任务卡被吞掉。

### 5.2 上游诱因

后台 memory extraction 已经从 chat 主 SSE 解耦，改走 `/api/internal-tasks/{session_id}/stream`。这要求前端维护一套跨流共享的 `task.id -> message.id` 映射，合并 pending / success / warning / error 生命周期。

这个设计本身合理，但映射更新放在 updater 内部，且没有做到幂等，使 StrictMode 的重放行为变成了真实 bug。

### 5.3 下游放大因素

后端缺少边界日志，导致一开始无法直接判断：

- 后端是否提交了 snapshot。
- 后台 job 是否开始。
- task 是否发布。
- SSE 是否有订阅者。
- 发布事件是否投递到队列。
- 前端是否可通过快照兜底拉到任务。

因此问题表面看起来像“后端没发 memory_extraction”，实际是“后端发了，前端 upsert 吞了”。

---

## 6. 修复内容

### 6.1 后端补充诊断日志

在 `backend/main.py` 中补充 memory background task 的边界日志，主要覆盖：

- `_submit_memory_snapshot()`：提交 / 跳过原因、用户消息数量、scheduler 是否已有运行任务。
- `_run_memory_job()`：后台 job 开始、gate 结果、extraction window、最终保存数量。
- `_publish_memory_task()`：task kind/status、subscriber 数、delivered/dropped 数、active task 数。
- `_memory_task_stream()`：SSE 订阅打开、已有任务重放、订阅关闭。
- `/api/internal-tasks/{session_id}`：快照任务数量和 kind 列表。
- `/api/internal-tasks/{session_id}/stream`：请求进入时的 active task / subscriber 状态。

为了避免把模型返回的完整记忆内容打到日志里，`记忆提取模型返回` 日志只保留：

```text
has_arguments
argument_keys
```

不再打印完整 arguments 原文。

### 6.2 前端修复 upsert 幂等性

修复 `frontend/src/components/ChatPanel.tsx` 中的任务卡 upsert 逻辑：

```ts
const mappedMessageId = internalTaskMessageIdsRef.current.get(task.id)
const mappedMessage = mappedMessageId
  ? prev.find((message) => message.id === mappedMessageId)
  : undefined
const fallbackMessage = prev.find((message) => message.role === 'system' && message.internalTaskId === task.id)
const targetMessageId = mappedMessage?.id ?? fallbackMessage?.id
```

只有当 mapped id 对应的 message 真的存在于当前 `prev` 中，才走更新路径；否则当作新卡片插入。

这样即使 StrictMode 重放 updater，ref 中的预写映射也不会再让新卡片被误判为已存在。

---

## 7. 验证结果

### 7.1 自动化验证

后端 memory 相关筛选测试通过：

```text
PYTHONPATH=backend pytest backend/tests/test_memory_async_jobs.py backend/tests/test_memory_integration.py -k 'memory_extraction or internal_task or coalesces_pending_snapshots' -q

8 passed, 16 deselected
```

前端构建通过：

```text
npm run build
```

结果：

```text
✓ built
```

仅保留 Vite chunk size warning。

### 7.2 Playwright 真实前端验证

使用 Playwright 新建会话 `sess_ce81931fed5d`，发送长期偏好消息后，页面上成功显示两张后台系统任务卡：

```text
系统内部任务
记忆提取判定
完成
memory_extraction_gate

系统内部任务
记忆提取
完成
已提取 2 条记忆，其中 1 条待确认
memory_extraction
```

后端日志同时显示：

```text
记忆提取快照提交 ...
后台记忆任务发布 ... kind=memory_extraction_gate status=pending subscribers=1 delivered=1
记忆提取判定完成 ... should_extract=True
后台记忆任务发布 ... kind=memory_extraction status=pending
记忆提取解析完成 ... profile_items=2 working_items=0
后台记忆任务发布 ... kind=memory_extraction status=success
后台记忆任务 SSE 重放 ... kind=memory_extraction_gate status=success
后台记忆任务 SSE 重放 ... kind=memory_extraction status=success
```

### 7.3 截图处理

本次 Playwright 调试截图均存放在 `screenshots/` 下；验证结束后确认它们只是临时调试截图，已删除，未提交。

---

## 8. 经验教训

1. React state updater 必须是幂等且尽量无副作用；在 updater 内写 ref 很容易被 StrictMode 重放放大成真实状态错乱。
2. 对跨流合并类 UI，`id -> messageId` 映射只能作为加速索引，不能作为“消息已经存在”的事实来源；事实来源必须回查当前 state。
3. 后台任务系统必须在每个边界打日志：提交、调度、发布、订阅、投递、重放、快照兜底。否则“卡片不显示”会很难区分是后端没发、SSE 没到、还是前端没渲染。
4. 记忆系统日志要有足够诊断信息，但不能把用户原文或模型提取出的完整记忆内容直接写进日志；用数量、状态、key 列表和布尔标记更安全。

---

## 9. 后续建议

1. 为 `upsertInternalTaskMessage()` 抽出纯函数或 reducer，补一条 StrictMode / 重放场景的前端单测。
2. 把 memory background task 的日志级别后续改为可配置，开发环境默认开启，生产环境可降噪。
3. 在前端加一个轻量的 internal-task 调试面板或 trace 入口，直接展示 `/api/internal-tasks/{session_id}` 快照，避免每次靠 Playwright 脚本手拉接口。
4. 对所有跨 SSE 流合并的卡片类型做一次同类审计，重点检查是否还有 updater 内写 ref / map 的非幂等逻辑。
