# Session History 设计文档

## 概述

为 Travel Agent Pro 加入 session history 功能，包括三个核心能力：

1. **对话持久化** — 服务重启或页面刷新后完整恢复对话（断点续聊）
2. **历史会话列表** — 侧边栏展示所有会话，支持切换和删除
3. **规划结果归档** — Phase 7 完成时自动保存最终方案快照

**驱动力**：产品完整性 + 技术展示 + 实际使用需求。

**存储方案**：SQLite（`aiosqlite`），单文件部署，零外部服务依赖。

---

## 1. 数据模型（SQLite Schema）

```sql
-- 会话元数据
CREATE TABLE sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL DEFAULT 'default_user',
    title        TEXT,
    phase        INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'active',  -- active | archived | deleted
    created_at   TEXT NOT NULL,                   -- ISO 8601
    updated_at   TEXT NOT NULL
);

-- 对话消息
CREATE TABLE messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    role         TEXT NOT NULL,                    -- system | user | assistant | tool
    content      TEXT,
    tool_calls   TEXT,                             -- JSON, nullable
    tool_call_id TEXT,
    created_at   TEXT NOT NULL,
    seq          INTEGER NOT NULL
);

-- Plan State 快照（每次 phase 转换时保存）
CREATE TABLE plan_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    phase        INTEGER NOT NULL,
    plan_json    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- 归档方案（Phase 7 完成时生成）
CREATE TABLE archives (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    plan_json    TEXT NOT NULL,
    summary      TEXT,
    created_at   TEXT NOT NULL
);
```

**要点**：
- `messages.seq` 保证恢复时消息顺序正确
- `plan_snapshots` 记录过程快照，`archives` 记录最终成品
- UserMemory 保持现有 JSON 方式不变

---

## 2. 后端架构

### 新增模块：`backend/storage/`

```
backend/storage/
    __init__.py
    database.py        # SQLite 连接管理，建表迁移
    session_store.py   # Session CRUD
    message_store.py   # 消息追加与批量读取
    archive_store.py   # 归档保存与读取
```

### Session 生命周期

```
创建 → 活跃对话 → Phase 7 完成 → 自动归档
         │
         ├── 用户离开/刷新 → 挂起（数据已在 SQLite）
         │       └── 用户回来 → 从 SQLite 恢复
         │
         └── 用户删除 → 标记 deleted（软删除）
```

内存中的 `sessions` dict 仅作为热缓存，SQLite 是数据权威来源。

### 恢复机制

```python
async def _restore_session(session_id: str) -> dict:
    # 1. 从 sessions 表读元数据
    meta = await session_store.load(session_id)
    # 2. 从 messages 表按 seq 顺序加载全部消息
    messages = await message_store.load_all(session_id)
    # 3. 从 plan_snapshots 取最新快照，反序列化为 TravelPlanState
    plan = await snapshot_store.load_latest(session_id)
    # 4. 用 plan + messages 重建 AgentLoop
    agent = _build_agent(plan, meta.user_id)
    return {"plan": plan, "messages": messages, "agent": agent}
```

AgentLoop 本身无状态（状态全在 messages 和 plan 里），重建后等同于从未中断。

---

## 3. 后端 API 变更

### 新增接口

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/sessions` | 会话列表（按 updated_at 倒序） |
| DELETE | `/api/sessions/{session_id}` | 软删除会话 |
| GET | `/api/messages/{session_id}` | 获取会话的全部消息（用于前端恢复对话） |
| GET | `/api/archives/{session_id}` | 获取归档方案 |

### 现有接口改动

| 接口 | 改动 |
|------|------|
| `POST /api/sessions` | 增加 SQLite 写入 |
| `POST /api/chat/{session_id}` | 每轮结束后 append 消息到 SQLite；phase 变化时写 snapshot；Phase 7 时触发自动归档 |
| `GET /api/plan/{session_id}` | 内存未命中时从 SQLite 恢复 |

### 会话标题自动生成

从 `plan.destination` + `plan.dates` 拼接，不调用 LLM。如 `"东京 · 5天4晚"` 或 `"未定 · 新会话"`。plan 更新时同步更新标题。

---

## 4. 前端变更

### 布局

单栏布局改为侧边栏 + 主内容区：

```
┌──────────┬──────────────────────────────┐
│ 侧边栏    │  主内容区（现有布局不变）        │
│          │                              │
│ [+新对话] │  PhaseIndicator / MapView    │
│          │  ChatPanel                   │
│ 东京5日游 │                              │
│ 4/10     │                              │
│          │                              │
│ ── 归档 ──│                              │
│ 北京3日游 │                              │
│ 3/25 ✓   │                              │
└──────────┴──────────────────────────────┘
```

### 新增组件

- `SessionSidebar.tsx` — 侧边栏：新建按钮、会话列表、归档分区
- `SessionItem.tsx` — 会话条目：标题、时间、phase 状态、删除按钮

### 状态管理变更（App.tsx）

```
启动 → GET /api/sessions → 展示列表
  ├─ 点"新对话" → POST /api/sessions → 切换到新 session
  └─ 点历史会话 → GET /api/plan/{id} + GET /api/messages/{id} → 恢复对话
```

新增状态：`sessionList: SessionMeta[]`、`activeSessionId: string | null`

### 交互细节

- 侧边栏默认展开，移动端可折叠
- 活跃会话高亮
- 归档会话分组显示在底部，带 ✓ 标记
- 删除需二次确认

---

## 5. 测试策略

### 后端单元测试（`backend/tests/test_storage/`）

- `test_session_store.py` — 创建、加载、列表、软删除、状态流转
- `test_message_store.py` — 追加消息、按 session 加载、seq 顺序验证
- `test_archive_store.py` — 归档保存与读取

### 后端集成测试

- `test_session_lifecycle.py` — 创建 → 对话 → phase 推进 → 归档
- `test_session_restore.py` — 模拟重启后恢复，验证消息完整性和 plan state 正确性

全部使用内存 SQLite（`:memory:`）。

### E2E 冒烟测试

在 `e2e-test.spec.ts` 追加场景：创建会话 → 发消息 → 刷新页面 → 验证会话列表存在 → 点击恢复 → 验证消息还在。

---

## 6. 不在本次范围

- UserMemory 迁入 SQLite（保持 JSON 不变）
- 会话搜索/筛选（后续迭代）
- 会话自动命名调用 LLM（先用 plan 字段拼接）
- 多用户认证与权限
