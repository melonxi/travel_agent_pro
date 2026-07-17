# Memory Embedding Sidecar

Stage 3 semantic recall 可选地把候选项向量持久化到每用户的 SQLite 文件，避免跨召回的重复 embedding。实现：`backend/memory/embedding_sidecar.py`。

## 启用

`config.yaml`：

```yaml
memory:
  retrieval:
    stage3:
      semantic:
        embedding_index:
          enabled: true
          warm_on_write: false
          warm_buckets:
            - constraints
            - stable_preferences
          max_records_per_user: 20000
```

`warm_on_write` 控制 profile / slice 写入成功后是否立即预热向量；保持 `false` 时仅依赖召回路径 lazy 写。

## 文件位置

每用户一个 SQLite 文件：

```
backend/data/users/{user_id}/memory/embeddings/index.db
```

WAL 模式会生成同目录下的 `index.db-wal` / `index.db-shm`，备份时需一并带上。

## 运维兜底

- **怀疑 sidecar 行为异常**：删除该用户的 `embeddings/` 目录是永远安全的操作；下次召回会自动重建并按 lazy 路径填充。
- **切换 embedding model 后**：旧记录因 `embedding_model` 不匹配自动判 stale；可选删 `index.db` 强制清空，或保留让覆盖逐步发生。
- **DELETE profile item 路由**：当前是软删除（状态置 obsolete，文本不变），sidecar 行保持有效复用。如果未来改为 hard delete，需要在删除路径调用 `SidecarStore.delete_for_item`。

## Telemetry

`stage3.semantic_embedding_index` 字段：

- `hit_count + stale_count + miss_count == candidate_count`
- `write_count + write_error_count == miss_count + stale_count`
- `write_error_count > 0` 表示有 sidecar 写失败但召回结果未受影响。

## 设计取舍备注

- **Dispatcher 在 lane 错误路径也写入 `semantic_embedding_index`**：partial counters 反映流水线在哪一步失败，对 ops 诊断更有价值；错误本身通过 `lane_errors["semantic"]` 单独上报。
- **Embedding provider 顺序契约**：`embedding_provider.embed(texts)` 必须返回与 `texts` 同序的向量列表。Sidecar 写入信任这个契约。如果未来引入并行 / 重排的 provider 实现，必须先校验顺序。
