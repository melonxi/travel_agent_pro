# Architecture Slice

## 什么时候读

当任务需要理解系统边界、Phase 主路径、后端/前端模块职责时读取。

## 最小事实

- Travel Agent Pro 是基于 LLM 的旅行规划 Agent：模糊意图收敛、方案设计、日程组装、出发前查漏。
- 生产主路径是 Phase 1/2/3/4：
  - Phase 1：目的地和基础行程收敛。
  - Phase 2：旅行画像、候选池、骨架方案、交通住宿锁定。
  - Phase 3：逐日日程详排；可走串行 LLM，也可走并行 Orchestrator-Workers。
  - Phase 4：出发前查漏并冻结 `travel_plan.md` / `checklist.md`。
- 后端是 FastAPI + async Python。`backend/main.py` 负责应用装配；`backend/api/` 承载 HTTP route 与请求编排；`backend/agent/` 承载 AgentLoop、执行 helper 和 Phase 3 子系统。
- 前端是 React + Vite。`frontend/src/App.tsx` 是三栏应用壳，`components/` 承载 Chat、Trace、Memory、Phase2Workbench、Map、Timeline 等视图。
- 状态权威是 `TravelPlanState`。长期/历史记忆只能作为召回上下文，不替代当前旅行事实。
- 工具写状态必须走 plan writer 路径；不要在编排层或 worker 里直接改 `TravelPlanState`。

## 关键目录

- `backend/agent/`：AgentLoop facade、LLM turn、工具批处理、阶段转换、Phase 3 orchestrator/workers。
- `backend/phase/`：阶段路由、prompt、red flags、backtrack。
- `backend/state/`：旅行状态模型、manager、纯 mutation writer。
- `backend/tools/`：工具声明、执行引擎、plan tools、外部搜索/交通/POI 工具。
- `backend/api/orchestration/`：chat、agent、memory、session、common 编排。
- `backend/storage/`：SQLite 和 session/message/archive/trace store。
- `backend/memory/`：v3 profile / working memory / episode / episode slice / recall。
- `frontend/src/components/`：主要用户界面组件。

## 深入阅读

- Phase 细节：`../deep/phase-flow.md`
- Phase 3 并行：`../deep/phase3-parallel.md`
- 工具写状态：`tools.md`、`../deep/tool-state-writes.md`
- 数据流：`data-flow.md`
