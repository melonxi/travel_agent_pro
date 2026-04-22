# Memory v3-Only Cutover 设计

> **状态**：草案，待评审
> **日期**：2026-04-22
> **范围**：彻底移除运行时 v2/legacy 记忆兼容层，系统只保留 v3 记忆模型与 v3 API；历史旧数据不迁移，直接删除。

---

## 1. 背景

当前项目的记忆系统已经形成一个明显的混合态：

1. 运行时主召回链路基本已经是 v3
   - `profile.json`
   - `working_memory.json`
   - `episode_slices.jsonl`
2. 但以下关键职责仍依赖 v2/legacy 兼容层
   - `episode_slices` 的上游归档仍来自 `TripEpisode <- MemoryItem`
   - pending/confirm/reject/delete 仍是 legacy item 与 v3 profile 的双栈兼容
   - deprecated memory API 仍对外暴露
   - 前端 `MemoryCenter` 仍在混合读取 v2/v3 资源
   - recall 内部仍通过 `legacy RecallQuery` 适配层驱动一部分逻辑

这意味着当前系统并不是“v3 已完成，只剩清理尾巴”，而是“v3 主链已经出现，但边界还未闭合”。

本设计不再追求兼容保留。用户已经明确允许：

1. 不迁移任何旧记忆数据
2. 直接删除 v2/legacy 旧数据
3. 以一次性 cutover 的方式把系统彻底切到 v3-only

因此，本设计不包含渐进式兼容、不包含双写、不包含回退到 legacy 文件格式的运行时逻辑。

---

## 2. 设计目标

本次 cutover 的目标是一次性把系统收敛到单一 v3 记忆架构。

目标：

1. 删除所有运行时对 `memory.json`、`memory_events.jsonl`、`trip_episodes.jsonl` 的读写依赖。
2. 把完整历史旅行 `episodes` 正式提升为 v3 权威模型，而不是过渡文件。
3. 让 `episode_slices` 只从 v3 `episodes` 生成，不再从 legacy `TripEpisode` 或 `MemoryItem` 派生。
4. 让历史 recall 只依赖 v3 `profile` 与 v3 `episode_slices`。
5. 保持 `working_memory` 只服务当前 session/trip 上下文注入，不纳入历史 recall。
6. 删除 legacy memory API、legacy pending 事件、legacy 前端兼容展示与 legacy store。
7. 在代码、测试、文档三个层面都体现“系统当前只支持 v3”。

非目标：

1. 不迁移任何历史旧数据。
2. 不保留运行时 fallback 到 v2 的能力。
3. 不引入 embedding、向量库或 RAG。
4. 不扩大 working memory 的职责，不让它参与历史 recall。
5. 不保留与 v2 兼容的 API 契约。

---

## 3. 总体结论

这次 cutover 不是“删旧代码”那么简单，而是以下五件事的组合：

1. 新建 v3 权威历史旅行模型 `ArchivedTripEpisode`
2. 用它重写 `episode_slices` 的上游生成源
3. 把 recall 内部从 `legacy RecallQuery` adapter 拔掉
4. 把审批与 pending 统一收敛到 v3 profile
5. 删除 legacy store、legacy API、legacy 前端兼容层和 legacy 数据文件

只有这五件事全部完成，系统才能称为真正的 v3-only。

---

## 4. 目标架构

cutover 完成后，记忆系统只保留四类权威对象：

```text
TravelPlanState = 当前旅行事实唯一权威源
profile.json = 长期画像
sessions/{session_id}/trips/{trip_id}/working_memory.json = 当前 session/trip 的临时工作记忆
episodes.jsonl = 历史旅行完整归档
episode_slices.jsonl = 历史 recall 单元
events.jsonl = 审计事件（仅审计，不参与 recall）
```

读取路径：

```text
chat request
  -> TravelPlanState
  -> working memory direct injection
  -> recall gate
  -> retrieval plan
  -> profile retriever
  -> episode slice retriever
  -> reranker / candidate selection
  -> formatter
  -> system prompt memory block
```

写入路径：

```text
chat request
  -> extraction gate
  -> profile extractor -> profile.json
  -> working memory extractor -> working_memory.json

phase7 completion
  -> build ArchivedTripEpisode from current v3-native state
  -> append episodes.jsonl
  -> build episode_slices from ArchivedTripEpisode
  -> append episode_slices.jsonl
```

---

## 5. 存储目录

目标目录结构：

```text
data/users/{user_id}/memory/
  profile.json
  events.jsonl
  episodes.jsonl
  episode_slices.jsonl
  sessions/
    {session_id}/
      trips/
        {trip_id}/
          working_memory.json
```

必须删除并不再读写的旧文件：

```text
data/users/{user_id}/memory.json
data/users/{user_id}/memory_events.jsonl
data/users/{user_id}/trip_episodes.jsonl
```

说明：

1. `working_memory` 从旧版 `sessions/{session_id}/working_memory.json` 升级为带 `trip_id` 目录层级。
2. `events.jsonl` 保留，仅作为审计，不参与 recall、prompt 注入、candidate 构建。
3. 系统启动或运行时发现旧 v2 文件时，不迁移、不读取、不提示兼容，而是直接视为无效旧数据并删除。

---

## 6. 数据模型

### 6.1 `UserMemoryProfile`

长期画像继续沿用现有 v3 profile 模型：

```text
constraints
rejections
stable_preferences
preference_hypotheses
```

职责不变：

1. 承载跨旅行长期约束与偏好
2. 允许 pending/active/rejected/obsolete 状态治理
3. 参与历史 recall

边界不变：

1. 不保存当前旅行事实
2. 不保存当前 session 的阶段性临时决定
3. 不保存已完成旅行的完整归档内容

### 6.2 `SessionWorkingMemory`

working memory 保持现有 v3 模型语义，但路径和生命周期更严格：

1. 存储路径升级为 `session_id + trip_id` 双键
2. 只服务当前 session/trip 的上下文注入
3. 不进入历史 recall
4. trip reset 后，新 trip 使用新目录；旧 trip 的 working memory 不复用

### 6.3 新增 `ArchivedTripEpisode`

新增 v3 历史旅行归档模型，替代 `memory.models.TripEpisode` 在运行时中的职责。

建议结构：

```json
{
  "id": "ep_trip_123",
  "user_id": "u1",
  "session_id": "s1",
  "trip_id": "trip_123",
  "destination": "京都",
  "dates": {
    "start": "2026-05-01",
    "end": "2026-05-05",
    "total_days": 5
  },
  "travelers": {"adults": 2},
  "budget": {"amount": 20000, "currency": "CNY"},
  "selected_skeleton": {...},
  "selected_transport": {...},
  "accommodation": {...},
  "daily_plan_summary": {...},
  "final_plan_summary": "京都慢游，重心是东山、四条和岚山。",
  "decision_log": [],
  "lesson_log": [],
  "created_at": "2026-05-05T00:00:00",
  "completed_at": "2026-05-05T00:00:00"
}
```

字段说明：

1. `selected_skeleton`
   - 作为“行程模式”切片的重要输入
2. `selected_transport`
   - 作为交通经验切片输入
3. `accommodation`
   - 作为住宿经验切片输入
4. `daily_plan_summary`
   - 提供路线节奏、区域分配、跨天组织信息
5. `decision_log`
   - 显式记录用户确认/否决过的关键决策，替代 legacy `accepted_items/rejected_items`
6. `lesson_log`
   - 显式记录归档时沉淀出的经验或踩坑，替代 legacy `lessons`

### 6.4 `EpisodeSlice`

`EpisodeSlice` 保持作为历史 recall 单元，但其来源改为 `ArchivedTripEpisode`。

建议 slice taxonomy：

1. `itinerary_pattern`
2. `stay_choice`
3. `transport_choice`
4. `budget_signal`
5. `rejected_option`
6. `pitfall`

生成规则：

1. `itinerary_pattern`
   - 来源：`selected_skeleton + daily_plan_summary`
2. `stay_choice`
   - 来源：`accommodation`
3. `transport_choice`
   - 来源：`selected_transport`
4. `budget_signal`
   - 来源：`budget + final_plan_summary`
5. `rejected_option`
   - 来源：`decision_log` 中显式类型为“否决”的记录
6. `pitfall`
   - 来源：`lesson_log`

明确删除旧语义：

1. 不再从 `accepted_items` 生成切片
2. 不再从 `rejected_items` 生成切片
3. 不再从 `MemoryItem.attributes.reason` 生成切片

---

## 7. 运行时链路设计

### 7.1 同步 recall 链路

同步 recall 只保留以下来源：

1. `working_memory`
   - 直接注入上下文
   - 不经过 retrieval plan
2. `profile`
   - 通过 recall gate + retrieval plan + symbolic retriever 进入历史 recall
3. `episode_slices`
   - 通过 recall gate + retrieval plan + symbolic retriever 进入历史 recall

结论：

```text
working_memory != historical recall source
historical recall sources = profile + episode_slice
```

### 7.2 Recall Gate

`Stage 0 + Stage 1` 保留当前职责：

1. `Stage 0` 负责极明显的 `skip_recall / force_recall`
2. `Stage 1` 负责判定本轮是否需要历史 recall

但输出 contract 必须面向 v3-only 主链路：

1. 不再引用 fixed profile 注入语义
2. 不再把后续执行路径描述成 legacy query recall

### 7.3 Retrieval Plan

`RecallRetrievalPlan.source` 必须升级为只描述历史 recall 源：

1. `profile`
2. `episode_slice`
3. `hybrid_history`

明确禁止：

1. `working_memory`
2. `legacy`
3. `profile_fixed`

`retrieval plan` 的职责：

1. 说明要查哪类历史记忆
2. 说明允许的 bucket/domain/keyword/entity 条件
3. 不负责决定最终命中项

### 7.4 去除 legacy recall adapter

当前 recall 内部仍存在两个 legacy 残留：

1. `plan_to_legacy_recall_query()`
2. `build_recall_query()` 在 slice recall 启用上的控制权

cutover 后必须删除这两种主路径依赖：

1. retrieval plan 直接驱动 profile recall
2. retrieval plan 直接驱动 episode slice recall
3. `rank_profile_items()` 与 `rank_episode_slices()` 若继续保留，应改为直接消费 v3 retrieval contract，而不是 legacy `RecallQuery`

### 7.5 Formatter

formatter 继续消费统一 `RecallCandidate[]`，但要配合新的 slice taxonomy：

1. `itinerary_pattern` 需突出节奏/区域结构
2. `stay_choice` 需突出住宿区域或类型
3. `transport_choice` 需突出交通方式偏好与条件
4. `pitfall` 需突出 lesson 边界

原则：

1. prompt 中只放短而明确的 recall 片段
2. 不把完整 `episodes` 注入 prompt
3. `episodes` 只作为 `episode_slices` 的上游权威存储

---

## 8. 提取与归档链路设计

### 8.1 Profile / Working Memory 提取

当前 split extraction 主体可以保留，但 contract 要纯 v3：

1. `memory_extraction_gate` 只输出 route-aware v3 payload
2. 删除对旧布尔 `should_extract` payload 的兼容解析
3. 保留两个 extractor：
   - `extract_profile_memory`
   - `extract_working_memory`
4. 删除兼容聚合任务 `memory_extraction`

### 8.2 Phase 7 完成归档

当前链路：

```text
TravelPlanState
  -> TripEpisode
  -> trip_episodes.jsonl
  -> build_episode_slices(TripEpisode)
```

目标链路：

```text
TravelPlanState + v3-native archive summary inputs
  -> ArchivedTripEpisode
  -> memory/episodes.jsonl
  -> build_episode_slices(ArchivedTripEpisode)
  -> memory/episode_slices.jsonl
```

必须做的调整：

1. 删除 `_build_trip_episode()`
2. 删除 `_append_trip_episode_once()` 对 legacy episode store 的依赖
3. 新增 v3 episode append/list store 能力
4. `build_episode_slices()` 改为接受 `ArchivedTripEpisode`

### 8.3 `decision_log` 与 `lesson_log` 的来源

为了避免再次滑回 legacy item 兼容思维，归档阶段必须显式定义这两类输入来源。

建议来源：

1. `decision_log`
   - 来自用户明确确认/否决的重要方案决策
   - 包括骨架选择、住宿选择、交通选择、被排除方案
2. `lesson_log`
   - 来自 Phase 7 收口时提炼出的经验、注意事项、踩坑摘要

要求：

1. 这两类信息必须是显式结构字段或显式归档步骤产物
2. 不能再由 `MemoryItem` 反推

---

## 9. API 设计

### 9.1 保留的 v3 API

1. `GET /api/memory/{user_id}/profile`
2. `GET /api/memory/{user_id}/episode-slices`
3. `GET /api/memory/{user_id}/sessions/{session_id}/working-memory`
4. `GET /api/memory/{user_id}/episodes`

说明：

1. `episodes` 接口升级为正式 v3 API
2. 不再返回 `deprecated`

### 9.2 新的 v3 profile mutation API

统一改成 profile 专属语义：

1. `POST /api/memory/{user_id}/profile/{item_id}/confirm`
2. `POST /api/memory/{user_id}/profile/{item_id}/reject`
3. `DELETE /api/memory/{user_id}/profile/{item_id}`

说明：

1. 只作用于 v3 `profile` 项
2. 不再“先查 legacy item，再 fallback v3”

### 9.3 删除的 API

1. `GET /api/memory/{user_id}`
2. 兼容型 `confirm/reject/delete` 接口
3. 旧语义的 deprecated episodes route 行为
4. `POST /api/memory/{user_id}/events`

说明：

1. 若 `events.jsonl` 仍需保留审计写入，应另设计内部或运维向接口，不继续沿用 legacy memory route。

---

## 10. Pending 与审批设计

### 10.1 目标状态

只有 v3 profile 支持 pending 审批：

1. `constraints`
2. `rejections`
3. `stable_preferences`
4. `preference_hypotheses`

不参与审批的对象：

1. `working_memory`
2. `episodes`
3. `episode_slices`

### 10.2 删除 legacy pending 事件

当前 `memory_pending` SSE 来自 legacy `MemoryItem` store，且前端已不消费。

cutover 后必须删除：

1. `memory_pending` 事件构造函数
2. chat 流中的 legacy pending 扫描
3. 与 `memory_pending_seen` 相关的兼容缓存

### 10.3 前端 pending 统计

前端 `pendingCount` 只统计 v3 profile 中 `status == "pending"` 的条目，不再混入 legacy memories。

---

## 11. 前端设计

### 11.1 `useMemory`

`useMemory` 改成纯 v3：

加载内容：

1. `profile`
2. `episodes`
3. `episode_slices`
4. `working_memory`

删除内容：

1. `legacyMemories`
2. `pendingMemories`
3. `/api/memory/{user_id}` 请求
4. legacy `episodes` 请求语义

### 11.2 `MemoryCenter`

`MemoryCenter` 只保留以下区块：

1. 长期画像
2. 待确认画像
3. 历史旅行
4. 历史切片
5. 当前工作记忆

必须删除：

1. `LegacyMemoryCard`
2. 旧版画像兼容区块
3. 旧版待确认记忆区块
4. 旧版旅程记忆兼容区块

### 11.3 SideBar / Pending Badge

侧边栏 pending badge 只统计 v3 profile pending。

### 11.4 SSE / Trace

保留：

1. `memory_recall`
2. `internal_task`
3. trace 中的 `memory_hits` / `memory_recall`

删除：

1. 对 `memory_pending` 的任何概念残留

---

## 12. 数据删除与切换策略

本次 cutover 不迁移旧数据，而是直接删除旧数据。

### 12.1 删除策略

在新的 v3-only runtime 生效时，启动或切换脚本直接删除：

1. `memory.json`
2. `memory_events.jsonl`
3. `trip_episodes.jsonl`

原则：

1. 不备份到新运行时路径
2. 不写 `legacy_ignored.jsonl`
3. 不保留运行时可见的兼容层文件

### 12.2 这样带来的简化

允许直接删除旧数据后，可以显著简化方案：

1. 不需要 v2 -> v3 迁移脚本作为主路径
2. 不需要兼容旧 `UserMemory` / v2 envelope / `MemoryItem(scope=trip)`
3. 不需要双写或灰度读写切换
4. 不需要 legacy API 的长期保留窗口
5. 不需要前端同时兼容两套数据源

### 12.3 风险声明

这是一个显式破坏性切换：

1. 历史 legacy 记忆将永久丢失
2. cutover 之后，系统只保留新的 v3 运行时产生的数据
3. 该选择由用户明确授权，不视为事故或 bug

---

## 13. 实施顺序

### 阶段 A：定义 v3-only 模型与存储

1. 新增 `ArchivedTripEpisode`
2. 在 v3 store 中新增 `episodes` 读写能力
3. 调整 working memory 路径为 `session_id + trip_id`
4. 更新 `EpisodeSlice` 生成器以消费 `ArchivedTripEpisode`

### 阶段 B：改 runtime 主链

1. 改 Phase 7 归档逻辑，只写 v3 `episodes` 与 `episode_slices`
2. 去掉 `_build_trip_episode()` 与 legacy episode store 路径
3. 去掉 recall 内部对 `legacy RecallQuery` adapter 的依赖
4. 把 retrieval plan 升级为 `profile | episode_slice | hybrid_history`

### 阶段 C：改 API 与前端

1. 新增正式 v3 `episodes` API
2. 新增 profile 专属 mutation API
3. 删除 legacy memory API
4. 改 `useMemory`、`MemoryCenter`、`SessionSidebar`
5. 删除 `memory_pending` 残留

### 阶段 D：删除旧数据与旧代码

1. 删除旧数据文件
2. 删除 `FileMemoryStore` 运行时依赖
3. 删除 legacy `MemoryItem` / `TripEpisode` 运行时路径
4. 删除迁移脚本或将其降级为纯历史工具

### 阶段 E：测试与文档收尾

1. 删除或改写 legacy 测试
2. 补齐 v3-only integration tests
3. 更新 `PROJECT_OVERVIEW.md`
4. 更新 memory 相关文档，明确系统当前只支持 v3

---

## 14. 测试策略

### 14.1 后端

必须覆盖：

1. v3 `episodes` store 读写
2. `ArchivedTripEpisode -> EpisodeSlice` 生成
3. recall pipeline 只依赖 v3 profile + v3 slices
4. working memory 路径按 `trip_id` 隔离
5. profile pending confirm/reject/delete 的新 API
6. startup / cutover 脚本删除旧数据文件后的系统行为

### 14.2 前端

必须覆盖：

1. `useMemory` 只请求 v3 API
2. `MemoryCenter` 不再出现 legacy 卡片
3. pending badge 只统计 v3 profile pending
4. `episodes` tab 展示 v3 历史旅行

### 14.3 删除的测试

以下测试不再属于主线契约：

1. legacy store 测试
2. deprecated route 测试
3. v2 migration 测试
4. legacy pending 事件测试

---

## 15. 风险与约束

### 15.1 主要风险

1. `decision_log` / `lesson_log` 如果没有及时设计清楚，`rejected_option` 与 `pitfall` slice 会失去权威来源。
2. working memory 路径调整若遗漏，会引入 session 内 trip 切换污染。
3. 前端若仍残留对 legacy route 的请求，会在 cutover 后直接报错。

### 15.2 缓解策略

1. 先落地 `ArchivedTripEpisode` 和 slice 生成器，再删 legacy episode 路径。
2. 先让前端只读 v3 API，再删除 legacy route。
3. 用全局 grep 和 integration tests 确认 `memory.json` / `trip_episodes.jsonl` / `memory_pending` 不再被运行时引用。

---

## 16. 影响面

高影响文件：

1. `backend/main.py`
2. `backend/memory/manager.py`
3. `backend/memory/v3_store.py`
4. `backend/memory/v3_models.py`
5. `backend/memory/episode_slices.py`
6. `backend/memory/recall_query.py`
7. `frontend/src/hooks/useMemory.ts`
8. `frontend/src/components/MemoryCenter.tsx`
9. `frontend/src/types/memory.ts`
10. `PROJECT_OVERVIEW.md`

预计退役：

1. `backend/memory/store.py` 的运行时角色
2. `backend/memory/models.py` 中 legacy memory/episode 角色
3. `scripts/migrate_memory_v2_to_v3.py`

---

## 17. 最终状态定义

只有在以下条件全部满足时，才认为 cutover 完成：

1. 运行时不再读写任何 v2 文件
2. 运行时不再暴露任何 legacy memory API
3. 前端不再请求或展示任何 legacy memory 数据
4. recall 内部不再依赖 legacy recall adapter
5. `episode_slices` 只从 v3 `episodes` 生成
6. 旧数据文件在 cutover 时被直接删除
7. 文档与测试明确表述系统当前只支持 v3

---

## 18. 决策摘要

本设计做出以下明确决策：

1. 接受“删除旧数据，不迁移”的破坏性 cutover。
2. 把 `memory/episodes.jsonl` 提升为 v3 权威历史旅行存储。
3. 不让 `working_memory` 进入历史 recall。
4. 不保留 runtime legacy fallback。
5. 不保留前端 legacy 兼容展示。

一句话总结：

```text
本次不是在 v2 上继续修补，而是把记忆系统彻底收敛为一个单一、无兼容层、无旧数据负担的 v3-only 架构。
```
