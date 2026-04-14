# 前端重试与中断恢复体验设计

## 概述

当前聊天面板只在流式响应长时间无事件时展示一条静态灰字："连接可能已断开，可尝试停止后重新发送"。这个反馈过于单薄，无法区分短暂抖动、可继续生成、可重试失败、不可恢复错误和用户主动停止，导致用户不知道下一步该做什么。

本设计在不引入新依赖、不改动后端协议的前提下，围绕现有 `SSEEvent` 字段建立一个前端最小状态层，把流式异常反馈收敛为统一的内联状态条。

## 设计目标

1. 让用户明确知道当前是"继续等"、"继续生成"、"重新发送"还是"只能修改后重试"。
2. 保留已有 Solstice 视觉语言，避免高干扰的大块错误 UI。
3. 尽量少改：主要修改 `ChatPanel.tsx` 和 `index.css`，不新增依赖。
4. 同时兼顾桌面和移动端，不破坏现有消息流布局。

## 用户状态机

前端维护单一 `streamFeedback` 状态，取值如下：

| 状态 | 进入条件 | 退出条件 | 用户动作 |
|------|----------|----------|----------|
| `waiting` | `streaming=true` 且超过 30 秒无 SSE 事件 | 收到任意 SSE 事件 / 流结束 | 继续等待或点击已有停止按钮 |
| `continue` | 收到 `event.type === "error"` 且 `can_continue=true` | 点击继续生成 / 新发送 | `继续生成` |
| `retry` | 收到 `event.type === "error"` 且 `retryable=true`，或流异常结束但未收到 `done/error` | 点击重新发送 / 新发送 | `重新发送` |
| `fatal` | 收到 `event.type === "error"` 且 `retryable=false` 且 `can_continue=false` | 新发送 | 修改输入后重新发送 |
| `stopped` | 用户点击停止 | 新发送 / 重新发送 | `重新发送` |

## 交互设计

### 统一状态条

在消息区底部、输入框上方保留一个单一的内联状态条 `chat-status`，取代当前分散的：

- `connectionWarning` 灰字提示
- 裸露的 `继续生成` 按钮

状态条结构：

1. 左侧图标
2. 主文案
3. 次级说明文案
4. 右侧动作按钮（可选）

### 状态条视觉层级

| 状态 | 颜色语义 | 视觉原则 |
|------|----------|----------|
| `waiting` | 柔和 amber | 像提醒，不像报错 |
| `continue` | 柔和 teal/amber | 重点突出可恢复操作 |
| `retry` | 柔和 coral | 明确失败，但仍有出路 |
| `fatal` | 柔和 coral | 仅展示说明，不诱导错误操作 |
| `stopped` | 柔和灰色 | 明确是用户行为，不是系统故障 |

状态条不使用强烈实色背景，只用半透明背景、细边框和图标强化语义，保持 Solstice 克制感。

## 文案矩阵

### waiting

- 主文案：`连接似乎不稳定，正在等待模型继续响应。`
- 次文案：`如果长时间没有恢复，可先停止，再重新发送上一条消息。`
- 按钮：无

### continue

- 主文案：`回复已中断，可从当前位置继续生成。`
- 次文案：优先使用后端 `message`；若有 `failure_phase`，前缀补充中文阶段名
- 按钮：`继续生成`

### retry

- 主文案：`本轮生成失败，可重新发送上一条消息。`
- 次文案：优先使用后端 `message`；若有 `failure_phase`，前缀补充中文阶段名
- 按钮：`重新发送`

### fatal

- 主文案：`本轮生成未完成，请调整后重新发送。`
- 次文案：优先使用后端 `message`；若有 `failure_phase`，前缀补充中文阶段名
- 按钮：无

### stopped

- 主文案：`已停止生成。`
- 次文案：`可以重新发送上一条消息，或修改内容后再发。`
- 按钮：`重新发送`

## 数据状态设计

`ChatPanel.tsx` 内新增：

```ts
type StreamFeedbackKind = 'waiting' | 'continue' | 'retry' | 'fatal' | 'stopped'
type StreamFeedbackTone = 'muted' | 'warning' | 'error'

interface StreamFeedback {
  kind: StreamFeedbackKind
  tone: StreamFeedbackTone
  message: string
  detail?: string
  action?: 'continue' | 'retry'
}
```

额外的本地引用：

- `lastUserMessageRef`：保存最近一次真实发送的用户消息，供 `重新发送` 使用
- `userStoppedRef`：区分"用户主动停止"和"连接异常中断"

## 最小实现范围

### 修改文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `frontend/src/components/ChatPanel.tsx` | 修改 | 引入统一 `streamFeedback` 状态、重试动作、停止后的重新发送、异常结束兜底 |
| `frontend/src/styles/index.css` | 修改 | 增加 `chat-status` 与动作按钮样式，删除旧的单句提示依赖 |

### 不改动项

- `useSSE.ts`：本轮不扩展 hook 状态，继续沿用现有 `sendMessage/cancel/continueGeneration`
- `types/plan.ts`：现有字段已满足本轮前端展示需要
- 后端接口：不新增 API，不改 SSE 协议

## 风险与取舍

1. 不做前端自动重试。
原因：自动重试可能重复消耗 token，也容易与后端 provider 层重试叠加。

2. `重新发送` 走"重新提交上一条用户消息"，而不是恢复到中断前的精确游标。
原因：这是现有协议下最小可实施方案，成本远小于真正的断点续传。

3. 不把错误文本直接覆盖到 assistant 气泡正文。
原因：保留已生成的部分内容，把错误与操作放在状态条中，信息结构更清晰。
