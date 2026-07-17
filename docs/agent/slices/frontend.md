# Frontend Slice

## 什么时候读

当任务涉及 React UI、SSE 消费、ChatPanel、Trace、Memory、Phase2Workbench、deliverables 或 E2E 可见行为时读取。

## 最小事实

- 前端是 React 19 + Vite 6 + TypeScript。
- 应用壳是三栏布局：
  - `SessionSidebar`：会话列表和记忆入口。
  - `ChatPanel`：聊天、工具卡、内部任务卡、停止/继续/重发。
  - `RightPanel`：Plan / Trace / Memory 标签页，含 Phase2Workbench、地图、时间线、预算、Trace、MemoryTracePanel。
- `useSSE` 消费 chat SSE；`useTrace` 拉 trace；`useMemory` 管理 v3 memory API。
- 设计系统默认是 Craft Paper：纸质文档流、hairline 边框、中性 ink/accent，无玻璃/琥珀光晕。
- 产品壳固定为 Craft Paper；仅保留 `html[data-theme="light"|"dark"]` 主题切换，不再提供公开 shell 切换器。
- `main.tsx` 在 React 渲染前写入固定 `data-shell="craft-paper"` 和 localStorage 中的 `data-theme`，避免 FOUC。

## 关键组件

- `ChatPanel`：SSE 主消费、工具卡和 internal task 生命周期合并。
- `MessageBubble`：文本、工具、系统任务卡渲染。
- `TraceViewer`：按阶段分组展示 trace。
- `Phase2Workbench`：旅行画像、候选池、骨架、锁定、风险。
- `MemoryCenter`：profile / working memory / episodes / episode slices 管理。
- `MemoryTracePanel`：本轮 memory recall 和 extraction 的只读诊断视图。
- `DeliverablesCard`：Phase 4 双 markdown 下载入口。

## 容易踩坑

- `phase_transition` 可先于 `state_update` 到达，UI 不能假设顺序相反。
- chat SSE 和 background internal-task SSE 会更新同一个 task id，必须合并生命周期。
- `memory_recall` 是结构化诊断事件，不等同于真实 memory hit 计数。

## 关键代码

- `frontend/src/App.tsx`
- `frontend/src/components/`
- `frontend/src/hooks/useSSE.ts`
- `frontend/src/hooks/useTrace.ts`
- `frontend/src/hooks/useMemory.ts`
- `frontend/src/types/`
- `frontend/src/styles/`

## 深入阅读

- SSE 协议：`../deep/sse-events.md`
- API：`api.md`
- Trace：`observability.md`
