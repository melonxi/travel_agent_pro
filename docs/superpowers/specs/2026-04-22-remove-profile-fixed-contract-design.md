# Fixed Profile Contract 清理设计

> 记录时间：2026-04-22
> 范围：移除 recall-first 之后遗留的 `fixed profile` 运行时链路与对外 contract
> 状态：设计中，待评审

---

## 1. 背景与问题定义

当前记忆系统已经收敛到 recall-first 主路径：

- Profile / EpisodeSlice 只在本轮请求命中后，以 `RecallCandidate` 形式进入 prompt
- Working memory 仍作为当前 session 的直接上下文注入
- `fixed profile` 固定注入链路已经不再实际生效

但代码与对外 contract 中仍残留一层历史语义：

- `MemoryManager.generate_context()` 里仍保留 `fixed_profile_items` 空变量与透传逻辑
- `formatter` 仍保留 `## 长期用户画像` 的固定渲染入口
- telemetry / trace / SSE / 前端类型里仍保留 `sources.profile_fixed`
- 前端和测试仍把 `profile_fixed` 当成一等 recall 来源理解

这导致两个问题：

1. 代码真实行为与可观测 contract 不一致，阅读 trace 会误以为系统还存在“长期画像固定注入”机制。
2. 后续维护者很难从类型和测试中看出真实主链路，只会继续围绕一个已失效的概念打补丁。

本次改造目标不是重做 recall telemetry，而是把 `fixed profile` 相关残留清理到与当前真实架构一致。

---

## 2. 目标与非目标

### 2.1 目标

- 删除 `fixed profile` 的空运行时链路
- 删除 `sources.profile_fixed` 这一对外来源维度
- 删除固定画像 markdown 渲染入口，确保 prompt 中只保留真实会出现的记忆块
- 收敛前端、trace、stats、测试和文档中的 `profile_fixed` 旧语义
- 保持现有 recall-first 主链路行为不变

### 2.2 非目标

- 不改写 recall gate / retrieval plan / symbolic recall / reranker 主流程
- 不把 working memory 改造成 recall-gated 机制
- 不在本轮重新设计整个 `MemoryRecallTelemetry` 结构
- 不删除顶层 `profile_ids`
- 不重做前端记忆展示交互，只做语义收口和兼容调整

---

## 3. 现状回顾

当前主链路中，`MemoryManager.generate_context()` 的关键行为是：

1. 读取当前 session 的 working memory，并直接注入 prompt
2. 根据 recall gate / retrieval plan 召回 profile 与 episode slice
3. 把选中的 `RecallCandidate[]` 交给 formatter 渲染为 `## 本轮请求命中的历史记忆`

与此同时，代码里还残留一条已失效的固定画像链路：

- `fixed_profile_items` 被初始化为空列表
- 该空列表继续传入 telemetry 构建函数
- 该空列表继续传入 formatter 的 `profile_items` 参数

因此当前运行时事实是：

- `## 长期用户画像` 在真实路径上不会出现
- `sources.profile_fixed` 在真实路径上应稳定为 `0`
- 真正有效的 recall 来源只有：
  - `query_profile`
  - `episode_slice`
  - `working_memory`

这说明 `profile_fixed` 已不是“向后兼容字段”，而是“错误表达当前架构的字段”。

---

## 4. 方案对比

### 方案 A：只删除运行时空链路，保留现有对外 contract

做法：

- 删除 `fixed_profile_items` 相关内部变量
- 保留 `sources.profile_fixed`、前端类型与相关测试字段

优点：

- 后端改动最小

缺点：

- 对外 contract 仍然表达错误语义
- 前端和 trace 继续暴露无业务意义的来源维度
- 后续仍会误导维护者

### 方案 B：删除 `profile_fixed` contract，但保留 `profile_ids`

做法：

- 删除 `fixed_profile_items` 运行时链路
- 删除 formatter 的固定画像渲染入口
- 删除 telemetry / trace / 前端中的 `sources.profile_fixed`
- 保留顶层 `profile_ids`，其语义收敛为“本轮最终命中的 profile recall item ids”

优点：

- 与当前 recall-first 架构一致
- 变更面可控，不会扩大为 telemetry 全量重设计
- 前端和测试迁移成本适中

缺点：

- `profile_ids` 仍然是聚合字段，不是完全最小的 telemetry 结构

### 方案 C：顺势重做 telemetry，删除 `profile_fixed` 与 `profile_ids`

做法：

- 删除 `profile_fixed`
- 同时删除 `profile_ids`，改成完全按来源拆分的 payload

优点：

- 语义最纯粹

缺点：

- 本次改造范围显著膨胀
- 需要同步重构 trace API、前端读取逻辑和大量测试
- 偏离“清理 fixed profile 残留”的最小目标

### 结论

本次采用 **方案 B**。

理由：它是最小且正确的收口方式，既能删除已经失效的 `fixed profile` 概念，又不会把工作扩大成一次 recall telemetry 重构。

---

## 5. 目标架构

本次改造后的记忆读取结构应明确为：

```text
memory_context
  ├─ 当前会话工作记忆（working memory，直接注入）
  └─ 本轮请求命中的历史记忆（selected RecallCandidate）
       ├─ query_profile
       └─ episode_slice
```

对外 telemetry 来源维度同步收敛为：

- `query_profile`
- `working_memory`
- `episode_slice`

系统不再保留 `profile_fixed` 这一来源维度。

---

## 6. 模块设计

### 6.1 `backend/memory/manager.py`

调整方向：

- 删除 `fixed_profile_items` 变量
- 删除 `_build_v3_telemetry()` 对 fixed profile ids 的处理
- `profile_ids` 只从最终 `recall_candidates` 中提取 profile 来源的 item id

改造后 `generate_context()` 的上下文输入应只包含：

- `working_items`
- `selected_candidates`

### 6.2 `backend/memory/formatter.py`

调整方向：

- 删除 `format_v3_memory_context()` 的 `profile_items` 参数
- 删除 `## 长期用户画像` 段落与 `_format_v3_profile_item()` 在固定注入路径上的使用
- 保留 `RecallCandidate` 的 profile 渲染逻辑，因为 query profile recall 仍然存在

改造后 formatter 只负责两类块：

- `## 当前会话工作记忆`
- `## 本轮请求命中的历史记忆`

### 6.3 `MemoryRecallTelemetry` / stats / trace / API

调整方向：

- 删除 `sources.profile_fixed`
- 保留 `profile_ids`，但其值只来自最终命中的 profile recall candidate
- 继续保留 `query_profile`、`working_memory`、`episode_slice` 三个来源计数

这意味着：

- 零命中时 `sources` 里不再出现 `profile_fixed: 0`
- 前端来源拆解不再显示 profile fixed 维度

### 6.4 前端类型与展示

调整方向：

- 删除 `frontend/src/types/trace.ts` 中 `profile_fixed` 类型
- `ChatPanel` 保留 `profile_ids` 聚合逻辑，但不再显示 `profile_fixed`
- `TraceViewer` 来源分解只显示真实来源维度

本次不改变 UI 布局，只改变来源语义和文案。

---

## 7. 数据契约变化

### 7.1 删除字段

从以下 payload / 类型 /展示语义中删除：

- `sources.profile_fixed`

### 7.2 保留字段但收敛语义

- `profile_ids`
  - 旧语义：fixed profile + query profile 的合并 id
  - 新语义：本轮最终命中的 profile recall item id

### 7.3 不变字段

- `working_memory_ids`
- `slice_ids`
- `matched_reasons`
- `candidate_count`
- `reranker_selected_ids`
- `reranker_final_reason`
- `reranker_fallback`
- `query_plan`
- `query_plan_fallback`

---

## 8. 兼容性与风险

### 8.1 兼容性影响

这是一次明确的 contract 收口，因此会影响：

- 后端测试中手工构造 `profile_fixed`
- trace API 和 stats 的序列化断言
- 前端类型与来源展示文案
- `PROJECT_OVERVIEW.md` 与 memory 设计文档中的字段说明

### 8.2 主要风险

1. 前端仍假设 `sources.profile_fixed` 一定存在，导致展示异常。
2. 某些测试仍用旧 payload 构造对象，导致断言失败。
3. 文档未同步，后续又把 `profile_fixed` 当真实来源加回去。

### 8.3 风险控制

- 先从类型层删除 `profile_fixed`，让残留引用在测试阶段暴露
- 统一更新 trace / stats / formatter / 前端断言，不保留双语义过渡层
- 同步更新 `PROJECT_OVERVIEW.md`，明确 recall 来源只剩三类

---

## 9. 测试策略

本次不新增复杂功能测试，重点是把现有测试断言收口到真实语义：

- `backend/tests/test_memory_manager.py`
  - 确认不再渲染 `## 长期用户画像`
  - 确认 `sources` 不含 `profile_fixed`
  - 确认 `profile_ids` 只来自 recall candidate

- `backend/tests/test_memory_formatter.py`
  - 更新 telemetry `to_dict()` 的预期结构
  - 删除固定画像段落的旧断言

- `backend/tests/test_stats.py`
  - 删除 `profile_fixed` 相关断言

- `backend/tests/test_trace_api.py`
  - 更新 trace / memory hit payload 的来源结构

- `backend/tests/test_memory_v3_api.py`
  - 更新 API payload 里 `sources` 与 `profile_ids` 的预期

- 前端相关测试或类型检查覆盖范围内
  - 删除 `profile_fixed` 类型与展示残留

---

## 10. 文件影响范围

预计会修改：

- `backend/memory/manager.py`
- `backend/memory/formatter.py`
- `backend/main.py`
- `backend/api/trace.py`
- `backend/telemetry/stats.py`
- `backend/tests/test_memory_manager.py`
- `backend/tests/test_memory_formatter.py`
- `backend/tests/test_stats.py`
- `backend/tests/test_trace_api.py`
- `backend/tests/test_memory_v3_api.py`
- `backend/tests/test_memory_integration.py`
- `frontend/src/types/trace.ts`
- `frontend/src/types/plan.ts`
- `frontend/src/components/ChatPanel.tsx`
- `frontend/src/components/TraceViewer.tsx`
- `PROJECT_OVERVIEW.md`
- `docs/TODO.md`

如扫描后发现还有 `profile_fixed` 残留引用，也一并纳入清理。

---

## 11. 成功标准

完成后应满足：

1. 运行时代码中不再存在 `fixed_profile_items` 透传链路。
2. prompt formatter 不再支持固定画像段落输入。
3. `MemoryRecallTelemetry.sources` 不再包含 `profile_fixed`。
4. 前端 recall 来源展示只保留真实来源。
5. 测试、trace、stats、文档不再把 `profile_fixed` 当作有效来源。
6. `profile_ids` 语义清晰，且只代表本轮命中的 profile recall items。

---

## 12. 开放问题

本次有意保留一个后续问题，不在本轮解决：

- `profile_ids` 是否应该继续作为顶层聚合字段存在，还是未来进一步收敛到完全来源化的 telemetry 结构。

这属于 recall telemetry 的后续演进问题，不阻塞本次 fixed profile contract 清理。
