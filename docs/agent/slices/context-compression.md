# Context Compression Slice

## 什么时候读

当任务涉及 prompt 构造、token 预算、上下文压缩、phase rebuild、append-only history、`context_epoch` 或 session 恢复时读取。

## 最小事实

- 每轮真正发给 LLM 的 runtime input 都是临时构造，不直接等同于 SQLite 完整历史。
- SQLite `messages` 是 append-only 历史事实源；`session["messages"]` 是前端/进程内的非 system、非 transient history view。
- Phase forward、Phase 2 step change、backtrack 都是 runtime-context rebuild boundary。
- `context_epoch` 用于诊断不同 runtime segment，不单独建 context segment 表。
- 恢复 session 时加载完整内部 history，并重建下一轮 runtime input；旧 system prompt 和 transient tail 不 replay。

## 压缩策略

- `before_llm_call` 预留 `context_window - max_output_tokens - safety_margin`。
- 超预算时渐进压缩：先压工具结果，最后才摘要历史。
- 工具结果按类型裁剪，例如 web / 小红书搜索和详情保留信息骨架。
- 阶段前进时用确定性 handoff note 交接职责，不用自由摘要替代事实。

## 关键代码

- `backend/context/manager.py`
- `backend/agent/compaction.py`
- `backend/agent/execution/message_rebuild.py`
- `backend/api/orchestration/session/persistence.py`
- `backend/api/orchestration/session/context_segments.py`
- `backend/storage/message_store.py`

## 深入阅读

- 数据流：`data-flow.md`
- SQLite 历史：`../deep/sqlite-schema.md`
