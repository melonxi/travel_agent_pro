# Session History 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Travel Agent Pro 加入 SQLite 持久化的 session history，支持断点续聊、历史会话列表和自动归档。

**Architecture:** 新增 `backend/storage/` 模块封装 SQLite 操作，内存 `sessions` dict 退化为热缓存。前端增加侧边栏组件展示会话列表，`App.tsx` 改为先加载会话列表再进入对话。

**Tech Stack:** Python aiosqlite, FastAPI, React, TypeScript

---

## 文件结构

### 新建文件

| 文件 | 职责 |
|------|------|
| `backend/storage/__init__.py` | 模块入口 |
| `backend/storage/database.py` | SQLite 连接管理、建表 |
| `backend/storage/session_store.py` | Session 元数据 CRUD |
| `backend/storage/message_store.py` | 消息追加与批量读取 |
| `backend/storage/archive_store.py` | 归档方案保存与读取 |
| `backend/tests/test_storage_database.py` | database.py 的测试 |
| `backend/tests/test_storage_session.py` | session_store.py 的测试 |
| `backend/tests/test_storage_message.py` | message_store.py 的测试 |
| `backend/tests/test_storage_archive.py` | archive_store.py 的测试 |
| `backend/tests/test_session_restore.py` | 集成测试：session 恢复 |
| `frontend/src/components/SessionSidebar.tsx` | 侧边栏组件 |
| `frontend/src/components/SessionItem.tsx` | 会话条目组件 |
| `frontend/src/types/session.ts` | SessionMeta 类型定义 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `backend/pyproject.toml` | 添加 `aiosqlite` 依赖 |
| `backend/main.py` | 集成 storage 层，新增/改造 API 接口 |
| `frontend/src/App.tsx` | 侧边栏布局、会话列表状态管理 |
| `frontend/src/components/ChatPanel.tsx` | 支持加载历史消息 |
| `frontend/src/styles/index.css` | 侧边栏样式 |

---

### Task 1: 添加 aiosqlite 依赖

**Files:**
- Modify: `backend/pyproject.toml:6-21`

- [ ] **Step 1: 添加 aiosqlite 到 dependencies**

在 `backend/pyproject.toml` 的 `dependencies` 列表中添加 `aiosqlite`：

```toml
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "openai>=1.50.0",
    "anthropic>=0.40.0",
    "pydantic>=2.9.0",
    "pyyaml>=6.0",
    "httpx>=0.27.0",
    "sse-starlette>=2.0.0",
    "tiktoken>=0.7.0",
    "python-dotenv>=1.0.0",
    "opentelemetry-api>=1.20.0",
    "opentelemetry-sdk>=1.20.0",
    "opentelemetry-exporter-otlp>=1.20.0",
    "opentelemetry-instrumentation-fastapi>=0.41b0",
    "aiosqlite>=0.20.0",
]
```

同时在 `[tool.setuptools.packages.find]` 的 include 中添加 `"storage*"`：

```toml
[tool.setuptools.packages.find]
include = ["agent*", "llm*", "state*", "phase*", "tools*", "context*", "memory*", "harness*", "telemetry*", "storage*"]
```

- [ ] **Step 2: 安装依赖**

Run: `cd backend && pip install -e ".[dev]"`

- [ ] **Step 3: Commit**

```bash
git add backend/pyproject.toml
git commit -m "chore: add aiosqlite dependency for session persistence"
```

---

### Task 2: 实现 database.py — SQLite 连接管理和建表

**Files:**
- Create: `backend/storage/__init__.py`
- Create: `backend/storage/database.py`
- Test: `backend/tests/test_storage_database.py`

- [ ] **Step 1: 编写 database.py 的测试**

创建 `backend/tests/test_storage_database.py`：

```python
# backend/tests/test_storage_database.py
import pytest
from storage.database import Database


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


async def test_initialize_creates_tables(db: Database):
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    table_names = [r["name"] for r in rows]
    assert "sessions" in table_names
    assert "messages" in table_names
    assert "plan_snapshots" in table_names
    assert "archives" in table_names


async def test_initialize_is_idempotent(db: Database):
    """Calling initialize twice should not raise."""
    await db.initialize()
    rows = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
    )
    assert len(rows) == 1


async def test_execute_and_fetch(db: Database):
    await db.execute(
        "INSERT INTO sessions (session_id, user_id, title, phase, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("s1", "u1", "test", 1, "active", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    row = await db.fetch_one("SELECT * FROM sessions WHERE session_id = ?", ("s1",))
    assert row is not None
    assert row["session_id"] == "s1"
    assert row["user_id"] == "u1"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_storage_database.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'storage'`

- [ ] **Step 3: 实现 database.py**

创建 `backend/storage/__init__.py`（空文件）。

创建 `backend/storage/database.py`：

```python
# backend/storage/database.py
from __future__ import annotations

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL DEFAULT 'default_user',
    title        TEXT,
    phase        INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    role         TEXT NOT NULL,
    content      TEXT,
    tool_calls   TEXT,
    tool_call_id TEXT,
    created_at   TEXT NOT NULL,
    seq          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    phase        INTEGER NOT NULL,
    plan_json    TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archives (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id),
    plan_json    TEXT NOT NULL,
    summary      TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON plan_snapshots(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_archives_session ON archives(session_id);
"""


class Database:
    def __init__(self, db_path: str = "data/sessions.db"):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_storage_database.py -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/storage/__init__.py backend/storage/database.py backend/tests/test_storage_database.py
git commit -m "feat: add SQLite database layer with schema and connection management"
```

---

### Task 3: 实现 session_store.py — Session 元数据 CRUD

**Files:**
- Create: `backend/storage/session_store.py`
- Test: `backend/tests/test_storage_session.py`

- [ ] **Step 1: 编写测试**

创建 `backend/tests/test_storage_session.py`：

```python
# backend/tests/test_storage_session.py
import pytest
from storage.database import Database
from storage.session_store import SessionStore


@pytest.fixture
async def store():
    db = Database(":memory:")
    await db.initialize()
    s = SessionStore(db)
    yield s
    await db.close()


async def test_create_and_load(store: SessionStore):
    await store.create("sess_abc123def456", "user1", "东京5日游")
    meta = await store.load("sess_abc123def456")
    assert meta is not None
    assert meta["session_id"] == "sess_abc123def456"
    assert meta["user_id"] == "user1"
    assert meta["title"] == "东京5日游"
    assert meta["phase"] == 1
    assert meta["status"] == "active"


async def test_load_nonexistent(store: SessionStore):
    meta = await store.load("sess_nonexistent1")
    assert meta is None


async def test_list_sessions(store: SessionStore):
    await store.create("sess_aaaaaaaaaaaa", "user1", "会话A")
    await store.create("sess_bbbbbbbbbbbb", "user1", "会话B")
    await store.create("sess_cccccccccccc", "user1", "会话C")
    # Soft-delete one
    await store.soft_delete("sess_bbbbbbbbbbbb")
    result = await store.list_sessions()
    # Deleted sessions should not appear
    assert len(result) == 2
    ids = [r["session_id"] for r in result]
    assert "sess_bbbbbbbbbbbb" not in ids


async def test_update_phase_and_title(store: SessionStore):
    await store.create("sess_update123456", "user1", "初始标题")
    await store.update("sess_update123456", phase=3, title="东京 · 5天4晚")
    meta = await store.load("sess_update123456")
    assert meta["phase"] == 3
    assert meta["title"] == "东京 · 5天4晚"


async def test_soft_delete(store: SessionStore):
    await store.create("sess_delete123456", "user1", "待删除")
    await store.soft_delete("sess_delete123456")
    meta = await store.load("sess_delete123456")
    assert meta["status"] == "deleted"


async def test_update_status_to_archived(store: SessionStore):
    await store.create("sess_archive12345", "user1", "待归档")
    await store.update("sess_archive12345", status="archived")
    meta = await store.load("sess_archive12345")
    assert meta["status"] == "archived"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_storage_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'storage.session_store'`

- [ ] **Step 3: 实现 session_store.py**

创建 `backend/storage/session_store.py`：

```python
# backend/storage/session_store.py
from __future__ import annotations

from datetime import datetime, timezone

from storage.database import Database


class SessionStore:
    def __init__(self, db: Database):
        self._db = db

    async def create(
        self,
        session_id: str,
        user_id: str = "default_user",
        title: str | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO sessions (session_id, user_id, title, phase, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, 'active', ?, ?)",
            (session_id, user_id, title, now, now),
        )
        return await self.load(session_id)  # type: ignore[return-value]

    async def load(self, session_id: str) -> dict | None:
        return await self._db.fetch_one(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )

    async def list_sessions(self) -> list[dict]:
        return await self._db.fetch_all(
            "SELECT * FROM sessions WHERE status != 'deleted' ORDER BY updated_at DESC"
        )

    async def update(
        self,
        session_id: str,
        *,
        phase: int | None = None,
        title: str | None = None,
        status: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list = []
        if phase is not None:
            sets.append("phase = ?")
            params.append(phase)
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        params.append(session_id)
        await self._db.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE session_id = ?",
            tuple(params),
        )

    async def soft_delete(self, session_id: str) -> None:
        await self.update(session_id, status="deleted")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_storage_session.py -v`
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/storage/session_store.py backend/tests/test_storage_session.py
git commit -m "feat: add session store with CRUD and soft-delete"
```

---

### Task 4: 实现 message_store.py — 消息持久化

**Files:**
- Create: `backend/storage/message_store.py`
- Test: `backend/tests/test_storage_message.py`

- [ ] **Step 1: 编写测试**

创建 `backend/tests/test_storage_message.py`：

```python
# backend/tests/test_storage_message.py
import json
import pytest
from storage.database import Database
from storage.session_store import SessionStore
from storage.message_store import MessageStore


@pytest.fixture
async def stores():
    db = Database(":memory:")
    await db.initialize()
    ss = SessionStore(db)
    ms = MessageStore(db)
    await ss.create("sess_msg_test_001")
    yield ss, ms
    await db.close()


async def test_append_and_load(stores):
    _, ms = stores
    await ms.append("sess_msg_test_001", "user", "你好", seq=1)
    await ms.append("sess_msg_test_001", "assistant", "你好！有什么可以帮助你的？", seq=2)
    messages = await ms.load_all("sess_msg_test_001")
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "你好"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["seq"] == 2


async def test_load_empty_session(stores):
    _, ms = stores
    messages = await ms.load_all("sess_msg_test_001")
    assert messages == []


async def test_append_with_tool_calls(stores):
    _, ms = stores
    tool_calls = [{"id": "tc1", "name": "search_flights", "arguments": {"origin": "北京"}}]
    await ms.append(
        "sess_msg_test_001",
        "assistant",
        None,
        tool_calls=json.dumps(tool_calls),
        seq=1,
    )
    messages = await ms.load_all("sess_msg_test_001")
    assert len(messages) == 1
    loaded_tc = json.loads(messages[0]["tool_calls"])
    assert loaded_tc[0]["name"] == "search_flights"


async def test_append_with_tool_call_id(stores):
    _, ms = stores
    await ms.append(
        "sess_msg_test_001",
        "tool",
        '{"status": "success"}',
        tool_call_id="tc1",
        seq=1,
    )
    messages = await ms.load_all("sess_msg_test_001")
    assert messages[0]["tool_call_id"] == "tc1"


async def test_seq_ordering(stores):
    _, ms = stores
    # Insert out of order
    await ms.append("sess_msg_test_001", "assistant", "second", seq=2)
    await ms.append("sess_msg_test_001", "user", "first", seq=1)
    await ms.append("sess_msg_test_001", "assistant", "third", seq=3)
    messages = await ms.load_all("sess_msg_test_001")
    assert [m["content"] for m in messages] == ["first", "second", "third"]


async def test_append_batch(stores):
    _, ms = stores
    rows = [
        {"role": "user", "content": "msg1", "seq": 1},
        {"role": "assistant", "content": "msg2", "seq": 2},
        {"role": "user", "content": "msg3", "seq": 3},
    ]
    await ms.append_batch("sess_msg_test_001", rows)
    messages = await ms.load_all("sess_msg_test_001")
    assert len(messages) == 3
    assert messages[2]["content"] == "msg3"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_storage_message.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'storage.message_store'`

- [ ] **Step 3: 实现 message_store.py**

创建 `backend/storage/message_store.py`：

```python
# backend/storage/message_store.py
from __future__ import annotations

from datetime import datetime, timezone

from storage.database import Database


class MessageStore:
    def __init__(self, db: Database):
        self._db = db

    async def append(
        self,
        session_id: str,
        role: str,
        content: str | None,
        *,
        tool_calls: str | None = None,
        tool_call_id: str | None = None,
        seq: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, created_at, seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, tool_calls, tool_call_id, now, seq),
        )

    async def append_batch(self, session_id: str, rows: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            await self._db.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, created_at, seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    row["role"],
                    row.get("content"),
                    row.get("tool_calls"),
                    row.get("tool_call_id"),
                    now,
                    row["seq"],
                ),
            )

    async def load_all(self, session_id: str) -> list[dict]:
        return await self._db.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_storage_message.py -v`
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/storage/message_store.py backend/tests/test_storage_message.py
git commit -m "feat: add message store with append, batch append, and ordered loading"
```

---

### Task 5: 实现 archive_store.py — 归档方案

**Files:**
- Create: `backend/storage/archive_store.py`
- Test: `backend/tests/test_storage_archive.py`

- [ ] **Step 1: 编写测试**

创建 `backend/tests/test_storage_archive.py`：

```python
# backend/tests/test_storage_archive.py
import json
import pytest
from storage.database import Database
from storage.session_store import SessionStore
from storage.archive_store import ArchiveStore


@pytest.fixture
async def stores():
    db = Database(":memory:")
    await db.initialize()
    ss = SessionStore(db)
    arc = ArchiveStore(db)
    await ss.create("sess_arc_test_001")
    await ss.create("sess_arc_test_002")
    yield ss, arc
    await db.close()


async def test_save_and_load_archive(stores):
    _, arc = stores
    plan = {"destination": "东京", "phase": 7, "daily_plans": []}
    await arc.save("sess_arc_test_001", json.dumps(plan), summary="东京 · 5天4晚")
    result = await arc.load("sess_arc_test_001")
    assert result is not None
    assert result["summary"] == "东京 · 5天4晚"
    loaded_plan = json.loads(result["plan_json"])
    assert loaded_plan["destination"] == "东京"


async def test_load_nonexistent(stores):
    _, arc = stores
    result = await arc.load("sess_arc_test_002")
    assert result is None


async def test_save_snapshot_and_load_latest(stores):
    _, arc = stores
    plan_v1 = {"phase": 1, "destination": None}
    plan_v3 = {"phase": 3, "destination": "东京"}
    await arc.save_snapshot("sess_arc_test_001", 1, json.dumps(plan_v1))
    await arc.save_snapshot("sess_arc_test_001", 3, json.dumps(plan_v3))
    latest = await arc.load_latest_snapshot("sess_arc_test_001")
    assert latest is not None
    loaded = json.loads(latest["plan_json"])
    assert loaded["phase"] == 3
    assert loaded["destination"] == "东京"


async def test_load_latest_snapshot_empty(stores):
    _, arc = stores
    latest = await arc.load_latest_snapshot("sess_arc_test_002")
    assert latest is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_storage_archive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'storage.archive_store'`

- [ ] **Step 3: 实现 archive_store.py**

创建 `backend/storage/archive_store.py`：

```python
# backend/storage/archive_store.py
from __future__ import annotations

from datetime import datetime, timezone

from storage.database import Database


class ArchiveStore:
    def __init__(self, db: Database):
        self._db = db

    async def save(
        self,
        session_id: str,
        plan_json: str,
        summary: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO archives (session_id, plan_json, summary, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, plan_json, summary, now),
        )

    async def load(self, session_id: str) -> dict | None:
        return await self._db.fetch_one(
            "SELECT * FROM archives WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )

    async def save_snapshot(
        self,
        session_id: str,
        phase: int,
        plan_json: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO plan_snapshots (session_id, phase, plan_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, phase, plan_json, now),
        )

    async def load_latest_snapshot(self, session_id: str) -> dict | None:
        return await self._db.fetch_one(
            "SELECT * FROM plan_snapshots WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_storage_archive.py -v`
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/storage/archive_store.py backend/tests/test_storage_archive.py
git commit -m "feat: add archive store with plan snapshots and final archive"
```

---

### Task 6: 集成 storage 层到 main.py — 初始化和 create_session

**Files:**
- Modify: `backend/main.py:120-160` (create_app 函数头部)
- Modify: `backend/main.py:402-415` (create_session 接口)

- [ ] **Step 1: 在 create_app 中初始化 Database 和各 Store**

在 `backend/main.py` 的 `create_app` 函数中，在 `state_mgr` 等初始化之后，添加 storage 层初始化：

```python
# 在现有 import 块末尾添加
from storage.database import Database
from storage.session_store import SessionStore
from storage.message_store import MessageStore
from storage.archive_store import ArchiveStore
```

在 `create_app` 函数中，`sessions: dict[str, dict] = {}` 之后添加：

```python
    # SQLite persistent storage
    db = Database(db_path=str(Path(config.data_dir) / "sessions.db"))
    session_store = SessionStore(db)
    message_store = MessageStore(db)
    archive_store = ArchiveStore(db)
```

在 `lifespan` 函数中，`yield` 之前添加 `await db.initialize()`，`yield` 之后添加 `await db.close()`：

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.initialize()
        await _probe_context_window()
        yield
        await db.close()
```

- [ ] **Step 2: 改造 create_session 接口**

将 `POST /api/sessions` 改为同时写入 SQLite：

```python
    @app.post("/api/sessions")
    async def create_session():
        plan = await state_mgr.create_session()
        compression_events: list[dict] = []
        agent = _build_agent(plan, "default_user", compression_events=compression_events)
        sessions[plan.session_id] = {
            "plan": plan,
            "messages": [],
            "agent": agent,
            "needs_rebuild": False,
            "user_id": "default_user",
            "compression_events": compression_events,
        }
        # Persist to SQLite
        await session_store.create(plan.session_id, "default_user")
        return {"session_id": plan.session_id, "phase": plan.phase}
```

- [ ] **Step 3: 运行现有测试确认不破坏**

Run: `cd backend && python -m pytest tests/test_api.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/main.py
git commit -m "feat: integrate SQLite storage layer into app initialization and session creation"
```

---

### Task 7: 集成 storage 层到 main.py — 消息持久化和 session 恢复

**Files:**
- Modify: `backend/main.py:449-603` (chat 接口和新增接口)

- [ ] **Step 1: 添加消息持久化辅助函数**

在 `create_app` 函数内（`_build_agent` 之后、路由定义之前）添加：

```python
    def _generate_title(plan: TravelPlanState) -> str:
        """Generate session title from plan state."""
        dest = plan.destination or "未定"
        if plan.dates:
            days = plan.dates.total_days
            nights = days - 1 if days > 1 else 0
            return f"{dest} · {days}天{nights}晚"
        return f"{dest} · 新会话"

    async def _persist_messages(session_id: str, messages: list[Message]) -> None:
        """Persist all messages for a session (replace strategy: delete + re-insert)."""
        await db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        rows = []
        for i, msg in enumerate(messages):
            tool_calls_json = None
            if msg.tool_calls:
                tool_calls_json = json.dumps(
                    [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls],
                    ensure_ascii=False,
                )
            tool_call_id = None
            if msg.tool_result:
                tool_call_id = msg.tool_result.tool_call_id
            rows.append({
                "role": msg.role.value,
                "content": msg.content,
                "tool_calls": tool_calls_json,
                "tool_call_id": tool_call_id,
                "seq": i,
            })
        await message_store.append_batch(session_id, rows)

    async def _restore_session(session_id: str) -> dict | None:
        """Restore a session from SQLite into memory."""
        meta = await session_store.load(session_id)
        if meta is None or meta["status"] == "deleted":
            return None

        # Load plan from existing state manager (JSON files)
        try:
            plan = await state_mgr.load(session_id)
        except FileNotFoundError:
            # Try from latest snapshot
            snapshot = await archive_store.load_latest_snapshot(session_id)
            if snapshot is None:
                return None
            plan = TravelPlanState.from_dict(json.loads(snapshot["plan_json"]))

        # Load messages
        raw_messages = await message_store.load_all(session_id)
        messages: list[Message] = []
        for rm in raw_messages:
            role = Role(rm["role"])
            tool_calls = None
            if rm.get("tool_calls"):
                tc_list = json.loads(rm["tool_calls"])
                tool_calls = [
                    ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                    for tc in tc_list
                ]
            tool_result = None
            if rm.get("tool_call_id"):
                tool_result = ToolResult(
                    tool_call_id=rm["tool_call_id"],
                    status="success",
                    data=rm.get("content"),
                )
            messages.append(Message(
                role=role,
                content=rm.get("content") if not tool_result else None,
                tool_calls=tool_calls,
                tool_result=tool_result,
            ))

        compression_events: list[dict] = []
        agent = _build_agent(plan, meta["user_id"], compression_events=compression_events)
        return {
            "plan": plan,
            "messages": messages,
            "agent": agent,
            "needs_rebuild": False,
            "user_id": meta["user_id"],
            "compression_events": compression_events,
        }
```

- [ ] **Step 2: 修改 chat 接口 — event_stream 结束后持久化**

在 `chat` 接口的 `event_stream` 函数末尾，`await state_mgr.save(plan)` 之后添加：

```python
            # Persist messages to SQLite
            await _persist_messages(plan.session_id, messages)

            # Update session metadata
            title = _generate_title(plan)
            await session_store.update(
                plan.session_id,
                phase=plan.phase,
                title=title,
            )

            # Save plan snapshot on phase change
            phase_after = plan.phase
            if phase_after != phase_before_run:
                await archive_store.save_snapshot(
                    plan.session_id,
                    phase_after,
                    json.dumps(plan.to_dict(), ensure_ascii=False),
                )

            # Auto-archive on Phase 7
            if plan.phase == 7:
                summary = _generate_title(plan)
                await archive_store.save(
                    plan.session_id,
                    json.dumps(plan.to_dict(), ensure_ascii=False),
                    summary=summary,
                )
                await session_store.update(plan.session_id, status="archived")
```

- [ ] **Step 3: 修改 get_plan 接口 — 支持从 SQLite 恢复**

将 `GET /api/plan/{session_id}` 改为：

```python
    @app.get("/api/plan/{session_id}")
    async def get_plan(session_id: str):
        session = sessions.get(session_id)
        if not session:
            # Try to restore from SQLite
            restored = await _restore_session(session_id)
            if restored:
                sessions[session_id] = restored
                session = restored
            else:
                try:
                    plan = await state_mgr.load(session_id)
                    phase_router.sync_phase_state(plan)
                    return plan.to_dict()
                except (FileNotFoundError, ValueError):
                    raise HTTPException(status_code=404, detail="Session not found")
        phase_router.sync_phase_state(session["plan"])
        return session["plan"].to_dict()
```

- [ ] **Step 4: 修改 chat 接口 — 支持从 SQLite 恢复**

在 `POST /api/chat/{session_id}` 开头，将找不到 session 时直接报 404 改为尝试恢复：

```python
    @app.post("/api/chat/{session_id}")
    async def chat(session_id: str, req: ChatRequest):
        session = sessions.get(session_id)
        if not session:
            restored = await _restore_session(session_id)
            if not restored:
                raise HTTPException(status_code=404, detail="Session not found")
            sessions[session_id] = restored
            session = restored
        # ... rest unchanged
```

- [ ] **Step 5: Commit**

```bash
git add backend/main.py
git commit -m "feat: add message persistence, session restore, and auto-archive on Phase 7"
```

---

### Task 8: 新增 API 接口 — 会话列表、删除、消息加载、归档

**Files:**
- Modify: `backend/main.py` (在路由区域添加新接口)

- [ ] **Step 1: 添加 GET /api/sessions 接口**

在 `create_session` 路由之后添加：

```python
    @app.get("/api/sessions")
    async def list_sessions():
        rows = await session_store.list_sessions()
        return [
            {
                "session_id": r["session_id"],
                "title": r["title"],
                "phase": r["phase"],
                "status": r["status"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
```

- [ ] **Step 2: 添加 DELETE /api/sessions/{session_id} 接口**

```python
    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        meta = await session_store.load(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Session not found")
        await session_store.soft_delete(session_id)
        # Remove from memory cache
        sessions.pop(session_id, None)
        return {"status": "deleted"}
```

- [ ] **Step 3: 添加 GET /api/messages/{session_id} 接口**

```python
    @app.get("/api/messages/{session_id}")
    async def get_messages(session_id: str):
        meta = await session_store.load(session_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="Session not found")
        raw = await message_store.load_all(session_id)
        return [
            {
                "role": m["role"],
                "content": m["content"],
                "tool_calls": json.loads(m["tool_calls"]) if m.get("tool_calls") else None,
                "tool_call_id": m.get("tool_call_id"),
                "seq": m["seq"],
            }
            for m in raw
        ]
```

- [ ] **Step 4: 添加 GET /api/archives/{session_id} 接口**

```python
    @app.get("/api/archives/{session_id}")
    async def get_archive(session_id: str):
        result = await archive_store.load(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Archive not found")
        return {
            "session_id": result["session_id"],
            "plan": json.loads(result["plan_json"]),
            "summary": result["summary"],
            "created_at": result["created_at"],
        }
```

- [ ] **Step 5: Commit**

```bash
git add backend/main.py
git commit -m "feat: add API endpoints for session list, delete, messages, and archives"
```

---

### Task 9: 后端集成测试 — Session 恢复验证

**Files:**
- Create: `backend/tests/test_session_restore.py`

- [ ] **Step 1: 编写集成测试**

创建 `backend/tests/test_session_restore.py`：

```python
# backend/tests/test_session_restore.py
import json
import pytest
from storage.database import Database
from storage.session_store import SessionStore
from storage.message_store import MessageStore
from storage.archive_store import ArchiveStore
from state.models import TravelPlanState


@pytest.fixture
async def full_storage():
    db = Database(":memory:")
    await db.initialize()
    ss = SessionStore(db)
    ms = MessageStore(db)
    arc = ArchiveStore(db)
    yield db, ss, ms, arc
    await db.close()


async def test_full_session_roundtrip(full_storage):
    """Simulate: create session -> chat -> persist -> restore -> verify."""
    _, ss, ms, arc = full_storage

    session_id = "sess_roundtrip01"
    await ss.create(session_id, "user1", "东京5日游")

    # Simulate messages
    await ms.append(session_id, "system", "你是旅行规划助手。", seq=0)
    await ms.append(session_id, "user", "我想去东京玩5天", seq=1)
    await ms.append(session_id, "assistant", "好的！让我为你规划东京5日游。", seq=2)

    # Simulate plan snapshot
    plan = TravelPlanState(session_id=session_id, phase=3, destination="东京")
    await arc.save_snapshot(session_id, 3, json.dumps(plan.to_dict(), ensure_ascii=False))
    await ss.update(session_id, phase=3, title="东京 · 5天4晚")

    # --- Simulate server restart: clear memory ---
    # Now restore
    meta = await ss.load(session_id)
    assert meta is not None
    assert meta["title"] == "东京 · 5天4晚"
    assert meta["phase"] == 3

    messages = await ms.load_all(session_id)
    assert len(messages) == 3
    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "我想去东京玩5天"
    assert messages[2]["role"] == "assistant"

    snapshot = await arc.load_latest_snapshot(session_id)
    assert snapshot is not None
    restored_plan = TravelPlanState.from_dict(json.loads(snapshot["plan_json"]))
    assert restored_plan.destination == "东京"
    assert restored_plan.phase == 3


async def test_archived_session_has_archive(full_storage):
    _, ss, ms, arc = full_storage

    session_id = "sess_archive_001"
    await ss.create(session_id, "user1")
    plan = TravelPlanState(session_id=session_id, phase=7, destination="大阪")
    await arc.save(session_id, json.dumps(plan.to_dict(), ensure_ascii=False), summary="大阪 · 3天2晚")
    await ss.update(session_id, status="archived")

    meta = await ss.load(session_id)
    assert meta["status"] == "archived"

    archive = await arc.load(session_id)
    assert archive is not None
    assert archive["summary"] == "大阪 · 3天2晚"


async def test_deleted_session_not_in_list(full_storage):
    _, ss, _, _ = full_storage

    await ss.create("sess_visible_001", "user1", "可见会话")
    await ss.create("sess_deleted_001", "user1", "已删除")
    await ss.soft_delete("sess_deleted_001")

    result = await ss.list_sessions()
    ids = [r["session_id"] for r in result]
    assert "sess_visible_001" in ids
    assert "sess_deleted_001" not in ids
```

- [ ] **Step 2: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_session_restore.py -v`
Expected: 3 tests PASS

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_session_restore.py
git commit -m "test: add integration tests for session persistence and restore"
```

---

### Task 10: 前端类型定义 — SessionMeta

**Files:**
- Create: `frontend/src/types/session.ts`

- [ ] **Step 1: 创建 SessionMeta 类型**

创建 `frontend/src/types/session.ts`：

```typescript
// frontend/src/types/session.ts
export interface SessionMeta {
  session_id: string
  title: string | null
  phase: number
  status: 'active' | 'archived' | 'deleted'
  updated_at: string
}

export interface SessionMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string | null
  tool_calls?: Array<{
    id: string
    name: string
    arguments: Record<string, unknown>
  }> | null
  tool_call_id?: string | null
  seq: number
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/types/session.ts
git commit -m "feat: add SessionMeta and SessionMessage TypeScript types"
```

---

### Task 11: 前端 — SessionItem 组件

**Files:**
- Create: `frontend/src/components/SessionItem.tsx`

- [ ] **Step 1: 实现 SessionItem**

创建 `frontend/src/components/SessionItem.tsx`：

```tsx
// frontend/src/components/SessionItem.tsx
import type { SessionMeta } from '../types/session'

interface Props {
  session: SessionMeta
  isActive: boolean
  onSelect: (sessionId: string) => void
  onDelete: (sessionId: string) => void
}

const PHASE_LABELS: Record<number, string> = {
  1: '需求收集',
  3: '方案设计',
  5: '最终确认',
  7: '已完成',
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffMin = Math.floor(diffMs / 60000)
  if (diffMin < 1) return '刚刚'
  if (diffMin < 60) return `${diffMin}分钟前`
  const diffHours = Math.floor(diffMin / 60)
  if (diffHours < 24) return `${diffHours}小时前`
  return `${d.getMonth() + 1}/${d.getDate()}`
}

export default function SessionItem({ session, isActive, onSelect, onDelete }: Props) {
  return (
    <div
      className={`session-item${isActive ? ' is-active' : ''}${session.status === 'archived' ? ' is-archived' : ''}`}
      onClick={() => onSelect(session.session_id)}
    >
      <div className="session-item-content">
        <div className="session-item-title">
          {session.status === 'archived' && <span className="session-archive-mark">&#10003;</span>}
          {session.title || '新会话'}
        </div>
        <div className="session-item-meta">
          <span className="session-item-phase">{PHASE_LABELS[session.phase] ?? `Phase ${session.phase}`}</span>
          <span className="session-item-time">{formatTime(session.updated_at)}</span>
        </div>
      </div>
      <button
        className="session-item-delete"
        onClick={(e) => {
          e.stopPropagation()
          onDelete(session.session_id)
        }}
        title="删除会话"
      >
        &times;
      </button>
    </div>
  )
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/SessionItem.tsx
git commit -m "feat: add SessionItem component for session list entries"
```

---

### Task 12: 前端 — SessionSidebar 组件

**Files:**
- Create: `frontend/src/components/SessionSidebar.tsx`

- [ ] **Step 1: 实现 SessionSidebar**

创建 `frontend/src/components/SessionSidebar.tsx`：

```tsx
// frontend/src/components/SessionSidebar.tsx
import { useState } from 'react'
import SessionItem from './SessionItem'
import type { SessionMeta } from '../types/session'

interface Props {
  sessions: SessionMeta[]
  activeSessionId: string | null
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
  onDeleteSession: (sessionId: string) => void
}

export default function SessionSidebar({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
}: Props) {
  const [collapsed, setCollapsed] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)

  const activeSessions = sessions.filter((s) => s.status === 'active')
  const archivedSessions = sessions.filter((s) => s.status === 'archived')

  const handleDelete = (sessionId: string) => {
    if (confirmDelete === sessionId) {
      onDeleteSession(sessionId)
      setConfirmDelete(null)
    } else {
      setConfirmDelete(sessionId)
    }
  }

  if (collapsed) {
    return (
      <div className="session-sidebar is-collapsed">
        <button className="sidebar-toggle" onClick={() => setCollapsed(false)} title="展开侧边栏">
          &#9654;
        </button>
      </div>
    )
  }

  return (
    <div className="session-sidebar">
      <div className="sidebar-header">
        <button className="sidebar-toggle" onClick={() => setCollapsed(true)} title="收起侧边栏">
          &#9664;
        </button>
        <button className="sidebar-new-btn" onClick={onNewSession}>
          + 新对话
        </button>
      </div>
      <div className="sidebar-list">
        {activeSessions.map((s) => (
          <SessionItem
            key={s.session_id}
            session={s}
            isActive={s.session_id === activeSessionId}
            onSelect={onSelectSession}
            onDelete={handleDelete}
          />
        ))}
        {archivedSessions.length > 0 && (
          <>
            <div className="sidebar-divider">归档</div>
            {archivedSessions.map((s) => (
              <SessionItem
                key={s.session_id}
                session={s}
                isActive={s.session_id === activeSessionId}
                onSelect={onSelectSession}
                onDelete={handleDelete}
              />
            ))}
          </>
        )}
      </div>
      {confirmDelete && (
        <div className="sidebar-confirm-toast">
          再次点击确认删除
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/SessionSidebar.tsx
git commit -m "feat: add SessionSidebar component with active/archived sections"
```

---

### Task 13: 前端 — App.tsx 集成侧边栏和会话管理

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/ChatPanel.tsx`

- [ ] **Step 1: 修改 App.tsx — 添加会话列表状态和侧边栏**

将 `frontend/src/App.tsx` 改为：

```tsx
import { useEffect, useState, useCallback, useRef } from 'react'
import ChatPanel from './components/ChatPanel'
import PhaseIndicator from './components/PhaseIndicator'
import MapView from './components/MapView'
import Timeline from './components/Timeline'
import BudgetChart from './components/BudgetChart'
import Phase3Workbench from './components/Phase3Workbench'
import SessionSidebar from './components/SessionSidebar'
import type { TravelPlanState } from './types/plan'
import type { SessionMeta } from './types/session'

function useTheme() {
  const [dark, setDark] = useState(() => {
    const saved = localStorage.getItem('theme')
    return saved ? saved === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches
  })

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light')
    localStorage.setItem('theme', dark ? 'dark' : 'light')
  }, [dark])

  return { dark, toggle: useCallback(() => setDark((d) => !d), []) }
}

function ThemeToggle({ dark, onToggle }: { dark: boolean; onToggle: () => void }) {
  return (
    <button className="theme-toggle" onClick={onToggle} title={dark ? '切换浅色' : '切换深色'}>
      {dark ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="5" />
          <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  )
}

function BrandMark() {
  return (
    <svg width="48" height="48" viewBox="0 0 48 48" fill="none" style={{ marginBottom: 20, opacity: 0.6 }}>
      <circle cx="24" cy="24" r="22" stroke="currentColor" strokeWidth="0.5" opacity="0.3" />
      <path d="M24 6 L24 42 M6 24 L42 24" stroke="currentColor" strokeWidth="0.3" opacity="0.2" />
      <circle cx="24" cy="24" r="3" fill="var(--accent-amber)" opacity="0.8" />
    </svg>
  )
}

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [plan, setPlan] = useState<TravelPlanState | null>(null)
  const [sessionList, setSessionList] = useState<SessionMeta[]>([])
  const { dark, toggle: toggleTheme } = useTheme()
  const initializedRef = useRef(false)
  const [chatKey, setChatKey] = useState(0)
  const showPhase3Workbench = Boolean(
    plan && (
      plan.phase === 3 ||
      plan.trip_brief ||
      plan.candidate_pool?.length ||
      plan.shortlist?.length ||
      plan.skeleton_plans?.length ||
      plan.risks?.length ||
      plan.alternatives?.length
    )
  )

  const handlePlanUpdate = (newPlan: TravelPlanState) => {
    setPlan(newPlan)
    // Refresh session list to reflect title/phase changes
    fetchSessionList()
  }

  const fetchSessionList = useCallback(async () => {
    try {
      const r = await fetch('/api/sessions')
      const data: SessionMeta[] = await r.json()
      setSessionList(data)
    } catch {
      // Silently fail — list will be empty
    }
  }, [])

  const handleNewSession = useCallback(async () => {
    const r = await fetch('/api/sessions', { method: 'POST' })
    const data = await r.json()
    setSessionId(data.session_id)
    const planR = await fetch(`/api/plan/${data.session_id}`)
    const planData = await planR.json()
    setPlan(planData)
    setChatKey((k) => k + 1)
    await fetchSessionList()
  }, [fetchSessionList])

  const handleSelectSession = useCallback(async (id: string) => {
    if (id === sessionId) return
    setSessionId(id)
    const planR = await fetch(`/api/plan/${id}`)
    const planData = await planR.json()
    setPlan(planData)
    setChatKey((k) => k + 1)
  }, [sessionId])

  const handleDeleteSession = useCallback(async (id: string) => {
    await fetch(`/api/sessions/${id}`, { method: 'DELETE' })
    await fetchSessionList()
    if (id === sessionId) {
      setSessionId(null)
      setPlan(null)
    }
  }, [sessionId, fetchSessionList])

  useEffect(() => {
    if (initializedRef.current) return
    initializedRef.current = true

    fetchSessionList().then(async () => {
      // Auto-create a new session if none exist
      const r = await fetch('/api/sessions')
      const list: SessionMeta[] = await r.json()
      if (list.length === 0) {
        await handleNewSession()
      } else {
        // Select the most recent active session
        const active = list.find((s) => s.status === 'active')
        const target = active || list[0]
        setSessionId(target.session_id)
        const planR = await fetch(`/api/plan/${target.session_id}`)
        const planData = await planR.json()
        setPlan(planData)
      }
    })
  }, [fetchSessionList, handleNewSession])

  if (!sessionId) {
    return (
      <div className="loading-screen">
        <BrandMark />
        <div className="loading-title">旅行者</div>
        <div className="loading-subtitle">travel agent pro</div>
        <div className="loading-dots">
          <span /><span /><span />
        </div>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <span className="brand-name">旅行者</span>
          <span className="brand-tag">travel agent</span>
        </div>
        <div className="header-right">
          {plan && <PhaseIndicator currentPhase={plan.phase} />}
          <ThemeToggle dark={dark} onToggle={toggleTheme} />
          <span className="session-badge">#{sessionId.slice(0, 8)}</span>
        </div>
      </header>
      <div className="app-body">
        <SessionSidebar
          sessions={sessionList}
          activeSessionId={sessionId}
          onSelectSession={handleSelectSession}
          onNewSession={handleNewSession}
          onDeleteSession={handleDeleteSession}
        />
        <ChatPanel key={chatKey} sessionId={sessionId} onPlanUpdate={handlePlanUpdate} />
        <div className="right-panel">
          {plan && plan.destination && (
            <div className="destination-banner">
              <div className="dest-label">目的地</div>
              <div className="dest-name">{plan.destination}</div>
              {plan.dates && (
                <div className="dest-dates">{plan.dates.start} → {plan.dates.end}</div>
              )}
              <div className="dest-meta">
                {plan.budget && (
                  <div className="dest-chip">
                    预算 ¥{plan.budget.total.toLocaleString()}
                  </div>
                )}
                {plan.accommodation && (
                  <div className="dest-chip">
                    住宿 {plan.accommodation.hotel ?? plan.accommodation.area}
                  </div>
                )}
              </div>
            </div>
          )}
          {plan && (
            <>
              {showPhase3Workbench && (
                <div className="sidebar-section">
                  <Phase3Workbench plan={plan} />
                </div>
              )}
              <div className="sidebar-section">
                <BudgetChart plan={plan} />
              </div>
              <div className="sidebar-section">
                <MapView dailyPlans={plan.daily_plans} dark={dark} />
              </div>
              <div className="sidebar-section">
                <Timeline dailyPlans={plan.daily_plans} />
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
```

关键变更点：
- 新增 `sessionList` 状态和 `fetchSessionList` 函数
- 启动时先 `GET /api/sessions`，有会话则恢复最近的，无会话则新建
- `chatKey` 用于切换会话时强制 ChatPanel 重新挂载，清空本地消息状态
- 侧边栏组件插入 `app-body` 最前面

- [ ] **Step 2: 修改 ChatPanel — 支持加载历史消息**

在 `ChatPanel.tsx` 中添加一个 `useEffect`，当组件挂载时从后端加载历史消息。在 `const { sendMessage } = useSSE()` 之后添加：

```tsx
  // Load persisted messages on mount
  useEffect(() => {
    let cancelled = false
    fetch(`/api/messages/${sessionId}`)
      .then((r) => r.json())
      .then((data: Array<{ role: string; content: string | null; tool_calls?: unknown[] | null; tool_call_id?: string | null }>) => {
        if (cancelled) return
        const restored: ChatMessage[] = []
        for (const m of data) {
          if (m.role === 'system') continue
          if (m.role === 'tool') {
            restored.push({
              id: createMessageId(),
              role: 'tool',
              content: m.content || '',
              toolCallId: m.tool_call_id || undefined,
              toolName: m.tool_call_id || undefined,
              toolStatus: 'success',
            })
          } else {
            restored.push({
              id: createMessageId(),
              role: m.role as 'user' | 'assistant',
              content: m.content || '',
            })
          }
        }
        if (restored.length > 0) {
          setMessages(restored)
        }
      })
      .catch(() => {})
    return () => { cancelled = true }
  }, [sessionId])
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/App.tsx frontend/src/components/ChatPanel.tsx
git commit -m "feat: integrate SessionSidebar into App and support message history loading"
```

---

### Task 14: 前端 — 侧边栏样式

**Files:**
- Modify: `frontend/src/styles/index.css`

- [ ] **Step 1: 添加侧边栏 CSS**

在 `frontend/src/styles/index.css` 末尾添加：

```css
/* ── Session Sidebar ────────────────────────────── */
.session-sidebar {
  width: 240px;
  min-width: 240px;
  background: var(--bg-surface);
  border-right: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.session-sidebar.is-collapsed {
  width: 40px;
  min-width: 40px;
  align-items: center;
  padding-top: 12px;
}

.sidebar-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px;
  border-bottom: 1px solid var(--border-subtle);
}

.sidebar-toggle {
  background: none;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 12px;
  padding: 4px 6px;
  border-radius: var(--radius-sm);
  transition: color var(--transition-smooth);
}

.sidebar-toggle:hover {
  color: var(--text-primary);
  background: var(--bg-elevated);
}

.sidebar-new-btn {
  flex: 1;
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  color: var(--text-primary);
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 13px;
  font-family: var(--font-body);
  transition: all var(--transition-smooth);
}

.sidebar-new-btn:hover {
  background: var(--bg-card);
  border-color: var(--border-accent);
}

.sidebar-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.sidebar-divider {
  font-size: 11px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 12px 8px 4px;
  border-top: 1px solid var(--border-subtle);
  margin-top: 8px;
}

/* ── Session Item ────────────────────────────────── */
.session-item {
  display: flex;
  align-items: center;
  padding: 10px 8px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--transition-smooth);
  gap: 4px;
}

.session-item:hover {
  background: var(--bg-elevated);
}

.session-item.is-active {
  background: var(--bg-card);
  border-left: 2px solid var(--accent-amber);
}

.session-item.is-archived {
  opacity: 0.7;
}

.session-item-content {
  flex: 1;
  min-width: 0;
}

.session-item-title {
  font-size: 13px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.session-archive-mark {
  color: var(--accent-teal);
  margin-right: 4px;
  font-size: 11px;
}

.session-item-meta {
  display: flex;
  gap: 8px;
  margin-top: 2px;
}

.session-item-phase,
.session-item-time {
  font-size: 11px;
  color: var(--text-muted);
}

.session-item-delete {
  background: none;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 16px;
  padding: 2px 4px;
  border-radius: var(--radius-sm);
  opacity: 0;
  transition: all var(--transition-smooth);
}

.session-item:hover .session-item-delete {
  opacity: 1;
}

.session-item-delete:hover {
  color: var(--accent-coral);
  background: rgba(217, 106, 75, 0.1);
}

.sidebar-confirm-toast {
  position: absolute;
  bottom: 12px;
  left: 12px;
  right: 12px;
  background: var(--bg-card);
  color: var(--accent-coral);
  font-size: 12px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid rgba(217, 106, 75, 0.3);
  text-align: center;
}

/* ── Layout adjustment for sidebar ───────────────── */
.app-body {
  display: flex;
}

@media (max-width: 768px) {
  .session-sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    z-index: 100;
    box-shadow: var(--shadow-ambient);
  }

  .session-sidebar.is-collapsed {
    box-shadow: none;
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/styles/index.css
git commit -m "feat: add session sidebar styles with dark/light theme support"
```

---

### Task 15: 最终集成验证

**Files:** (no new files)

- [ ] **Step 1: 运行全部后端测试**

Run: `cd backend && python -m pytest tests/ -v --tb=short`
Expected: 全部 PASS

- [ ] **Step 2: 手动验证前端**

启动前后端，验证以下流程：
1. 打开页面 — 侧边栏显示，自动创建新会话
2. 发送一条消息 — 侧边栏标题更新
3. 点"+新对话" — 创建新会话，切换过去
4. 点回第一个会话 — 消息历史恢复
5. 刷新页面 — 会话列表和消息都还在
6. 删除一个会话 — 需二次确认，删除后从列表消失
7. 侧边栏折叠/展开 — 正常工作

- [ ] **Step 3: 最终 Commit**

如果有任何微调，commit 它们：

```bash
git add -A
git commit -m "chore: final integration tweaks for session history"
```
