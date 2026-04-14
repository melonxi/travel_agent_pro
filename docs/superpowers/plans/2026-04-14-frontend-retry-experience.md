# 前端重试与中断恢复体验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 ChatPanel 当前简陋的连接断开提示升级为统一的流式状态条，清晰覆盖等待恢复、可继续生成、可重新发送、不可恢复错误和用户主动停止五类状态。

**Architecture:** 在 `ChatPanel.tsx` 中新增单一 `streamFeedback` 状态，统一承接 keepalive 超时、SSE error 事件、用户停止和异常结束兜底。状态条渲染在消息区底部，动作按钮直接调用已有 `continueGeneration` 和 `sendMessage` 能力。样式全部落在 `index.css`，沿用现有 Solstice 设计 token。

**Tech Stack:** React 19, TypeScript, CSS，无新依赖。

**Spec:** `docs/superpowers/specs/2026-04-14-frontend-retry-experience-design.md`

**Worktree:** `.worktrees/frontend-retry-experience` (branch `feature/frontend-retry-experience`)

---

## File Structure

| 文件 | 操作 | 责任 |
|------|------|------|
| `frontend/src/components/ChatPanel.tsx` | 修改 | 统一流式反馈状态、继续生成、重新发送、停止后重发 |
| `frontend/src/styles/index.css` | 修改 | 状态条与动作按钮样式 |

---

### Task 1: ChatPanel 统一流式反馈状态

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`

- [x] **Step 1: 新增本地状态与帮助函数**

新增：

- `StreamFeedback` 类型
- `lastUserMessageRef`
- `userStoppedRef`
- 错误阶段中文映射
- `createErrorFeedback()` 等状态工厂函数

- [x] **Step 2: 用 `streamFeedback` 取代分散提示**

移除：

- `connectionWarning`
- `canContinue`
- 裸露的 `continue-btn`

改为统一在消息区底部渲染 `chat-status`。

- [x] **Step 3: 把发送与重发收敛为同一套流程**

抽出公共发送函数，支持：

- 正常发送
- 重新发送上一条消息

要求：重发时不清空用户当前输入框内容。

- [x] **Step 4: 处理五类状态来源**

覆盖：

- keepalive 超时 -> `waiting`
- `error.can_continue=true` -> `continue`
- `error.retryable=true` -> `retry`
- 不可重试错误 -> `fatal`
- 用户点击停止 -> `stopped`
- 流意外结束但未收到 `done/error` -> `retry` 兜底

- [x] **Step 5: 保留已生成内容，不用错误文案覆盖 assistant 消息**

错误和动作仅显示在 `chat-status`，assistant 已产出的文本保持原样。

---

### Task 2: 样式实现

**Files:**
- Modify: `frontend/src/styles/index.css`

- [x] **Step 1: 新增 `chat-status` 相关样式**

包括：

- 容器
- 图标区域
- 主文案 / 次文案
- 动作区
- `warning/error/muted` 三种语义

- [x] **Step 2: 新增状态条按钮样式**

要求：

- 小尺寸次级按钮
- `继续生成` 使用常规强调样式
- `重新发送` 使用更明确的 coral 语义

- [x] **Step 3: 补移动端适配**

在窄屏下让状态条纵向排列，按钮换行不挤压消息区。

---

### Task 3: 自检

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`
- Modify: `frontend/src/styles/index.css`

- [x] **Step 1: 人工检查以下场景的代码路径**

确认代码路径覆盖：

- 正常发送成功
- 流式无事件超时
- 服务端 error 且可继续
- 服务端 error 且可重试
- 用户点击停止

- [x] **Step 2: 更新本计划勾选状态**

实现完成后，把本计划对应步骤改为 `- [x]`。
