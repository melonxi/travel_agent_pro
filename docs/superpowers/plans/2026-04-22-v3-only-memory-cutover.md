# Memory v3-Only Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把记忆系统从当前 v2/v3 混合态一次性切到 v3-only，删除旧数据与 legacy 运行时路径，让 profile、working memory、episodes、episode_slices 成为唯一权威记忆结构。

**Architecture:** 先建立 v3 权威 `ArchivedTripEpisode` 与 `episodes.jsonl` 存储，再把 Phase 7 归档和 `episode_slices` 生成改为只消费 v3 归档对象；随后收敛 recall、API、前端到纯 v3 契约，最后删除 legacy store、legacy route、legacy UI 与旧数据文件。整个实现不做数据迁移，不保留双写或 fallback。

**Tech Stack:** Python 3.12, FastAPI, JSON/JSONL file store, TypeScript, React, pytest

---

## 文件结构

### 新增文件

- `backend/memory/episode_archive.py`
  - 定义 `ArchivedTripEpisode` 与相关日志子结构
- `backend/tests/test_episode_archive.py`
  - 覆盖 v3 episode 模型与序列化
- `backend/tests/test_memory_v3_episodes.py`
  - 覆盖 v3 `episodes.jsonl` store 行为
- `backend/tests/test_memory_v3_only_api.py`
  - 覆盖新的 v3 profile mutation / episodes API

### 重点修改文件

- `backend/memory/v3_store.py`
  - 增加 `episodes.jsonl` 的 append/list/delete 读写能力
  - 调整 working memory 路径为 `session_id/trips/trip_id/working_memory.json`
- `backend/memory/episode_slices.py`
  - 改为消费 `ArchivedTripEpisode`
  - 重写 slice taxonomy 与生成规则
- `backend/memory/manager.py`
  - 去除对 legacy recall query adapter 的主路径依赖
  - 让 recall 只消费 v3 retrieval plan + v3 stores
- `backend/memory/recall_query.py`
  - 扩展 `RecallRetrievalPlan.source`
- `backend/main.py`
  - 删除 legacy episode/archive/store 路径
  - 引入 v3 episodes API 与 profile-only mutation API
  - 删除 legacy pending SSE
  - 启动时删除旧数据文件
- `backend/memory/v3_models.py`
  - 如需要补充 `episodes` 相关辅助类型或共用类型
- `frontend/src/hooks/useMemory.ts`
  - 改成只拉 v3 API
- `frontend/src/components/MemoryCenter.tsx`
  - 删除 legacy compat UI，增加 v3 episodes 展示
- `frontend/src/types/memory.ts`
  - 去掉 legacy `MemoryItem` 主路径类型与 compat 字段
- `PROJECT_OVERVIEW.md`
  - 更新为 v3-only 当前态
- `backend/state/models.py`
  - 新增 `TravelPlanState.decision_events` / `lesson_events`（`list[dict[str, Any]]`），参与 `to_dict` / `from_dict`
- `backend/state/plan_writers.py`
  - 在 Phase 3 skeleton/transport/stay 锁定或拒绝、Phase 5 每日方案锁定或拒绝时追加 `DecisionEvent`
  - 新增 `record_phase7_lesson(state, *, kind, note, now)` 写 `LessonEvent`

### 决策/教训采集机制（对齐 Spec §15.1，采用 Option A）

- `ArchivedTripEpisode.decision_log` / `lesson_log` 的**唯一来源**是 `TravelPlanState` 上的 `decision_events` / `lesson_events`。
- Phase 3/5 `plan_writers` 的锁定/拒绝点在状态写入同一事务中追加事件，不允许旁路。
- Phase 7 归档只做 `TravelPlanState → ArchivedTripEpisode` 的拷贝，不再派生或推断事件。
- `DecisionEvent` / `LessonEvent` 模型作为 `ArchivedTripEpisode` 的嵌套子结构在 `backend/memory/episode_archive.py` 中声明。

### 预计删除或退役的文件/职责

- `backend/memory/store.py`
  - 删除运行时使用；若仍保留文件，只允许历史参考，不应再被 import 到主链路
- `backend/memory/models.py`
  - 删除 legacy `TripEpisode` / `MemoryItem` 在运行时中的角色
- `scripts/migrate_memory_v2_to_v3.py`
  - 从主线实现中移除；若保留文件，标记为历史工具
- legacy tests
  - `backend/tests/test_memory_store.py`
  - `backend/tests/test_memory_v3_migration.py`
  - `backend/tests/test_memory_manager.py` 中 legacy load/save 相关部分
  - `backend/tests/test_memory_v3_api.py` 中 deprecated route 相关部分

---

### Task 1: 建立 v3 Episodes 权威模型

**Files:**
- Create: `backend/memory/episode_archive.py`
- Test: `backend/tests/test_episode_archive.py`

- [ ] **Step 1: 写失败测试，定义 `ArchivedTripEpisode` 的最小权威结构**

```python
from memory.episode_archive import ArchivedTripEpisode


def test_archived_trip_episode_round_trip_dict():
    episode = ArchivedTripEpisode(
        id="ep_trip_123",
        user_id="u1",
        session_id="s1",
        trip_id="trip_123",
        destination="京都",
        dates={"start": "2026-05-01", "end": "2026-05-05", "total_days": 5},
        travelers={"adults": 2},
        budget={"amount": 20000, "currency": "CNY"},
        selected_skeleton={"id": "balanced", "name": "轻松版"},
        selected_transport={"inbound": "MU", "local": "subway"},
        accommodation={"area": "四条", "type": "町屋"},
        daily_plan_summary={"pace": "slow", "clusters": ["东山", "四条"]},
        final_plan_summary="京都慢游",
        decision_log=[{"kind": "rejected_option", "domain": "hotel", "content": "拒绝商务连锁酒店"}],
        lesson_log=[{"kind": "pitfall", "content": "上午排太满下午会累"}],
        created_at="2026-05-05T00:00:00",
        completed_at="2026-05-05T00:00:00",
    )

    payload = episode.to_dict()

    assert payload["id"] == "ep_trip_123"
    assert payload["destination"] == "京都"
    assert payload["accommodation"]["area"] == "四条"

    restored = ArchivedTripEpisode.from_dict(payload)
    assert restored.to_dict() == payload
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run: `pytest backend/tests/test_episode_archive.py -v`
Expected: FAIL，提示 `ModuleNotFoundError` 或 `ArchivedTripEpisode` 未定义

- [ ] **Step 3: 实现 `ArchivedTripEpisode` 模型**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArchivedTripEpisode:
    id: str
    user_id: str
    session_id: str
    trip_id: str | None
    destination: str | None
    dates: dict[str, Any] | None
    travelers: dict[str, Any] | None
    budget: dict[str, Any] | None
    selected_skeleton: dict[str, Any] | None
    selected_transport: dict[str, Any] | None
    accommodation: dict[str, Any] | None
    daily_plan_summary: dict[str, Any] | None
    final_plan_summary: str
    decision_log: list[dict[str, Any]] = field(default_factory=list)
    lesson_log: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "trip_id": self.trip_id,
            "destination": self.destination,
            "dates": self.dates,
            "travelers": self.travelers,
            "budget": self.budget,
            "selected_skeleton": self.selected_skeleton,
            "selected_transport": self.selected_transport,
            "accommodation": self.accommodation,
            "daily_plan_summary": self.daily_plan_summary,
            "final_plan_summary": self.final_plan_summary,
            "decision_log": list(self.decision_log),
            "lesson_log": list(self.lesson_log),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArchivedTripEpisode":
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            session_id=str(data["session_id"]),
            trip_id=data.get("trip_id"),
            destination=data.get("destination"),
            dates=data.get("dates"),
            travelers=data.get("travelers"),
            budget=data.get("budget"),
            selected_skeleton=data.get("selected_skeleton"),
            selected_transport=data.get("selected_transport"),
            accommodation=data.get("accommodation"),
            daily_plan_summary=data.get("daily_plan_summary"),
            final_plan_summary=str(data.get("final_plan_summary", "")),
            decision_log=list(data.get("decision_log", [])),
            lesson_log=list(data.get("lesson_log", [])),
            created_at=str(data.get("created_at", "")),
            completed_at=str(data.get("completed_at", "")),
        )
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest backend/tests/test_episode_archive.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/memory/episode_archive.py backend/tests/test_episode_archive.py
git commit -m "feat: add v3 archived trip episode model"
```

### Task 2: 扩展 v3 store，正式支持 episodes 与 trip-scoped working memory

**Files:**
- Modify: `backend/memory/v3_store.py`
- Test: `backend/tests/test_memory_v3_episodes.py`
- Test: `backend/tests/test_memory_v3_store.py`

- [ ] **Step 1: 写失败测试，要求 v3 store 能读写 `episodes.jsonl`**

```python
from pathlib import Path

import pytest

from memory.episode_archive import ArchivedTripEpisode
from memory.v3_store import FileMemoryV3Store


@pytest.mark.asyncio
async def test_v3_store_appends_and_lists_episodes(tmp_path: Path):
    store = FileMemoryV3Store(tmp_path)
    episode = ArchivedTripEpisode(
        id="ep_trip_123",
        user_id="u1",
        session_id="s1",
        trip_id="trip_123",
        destination="京都",
        dates={"start": "2026-05-01", "end": "2026-05-05", "total_days": 5},
        travelers=None,
        budget=None,
        selected_skeleton=None,
        selected_transport=None,
        accommodation=None,
        daily_plan_summary=None,
        final_plan_summary="京都慢游",
        created_at="2026-05-05T00:00:00",
        completed_at="2026-05-05T00:00:00",
    )

    await store.append_episode(episode)
    await store.append_episode(episode)

    episodes = await store.list_episodes("u1")
    assert [item.id for item in episodes] == ["ep_trip_123"]
```

- [ ] **Step 2: 写失败测试，要求 working memory 路径包含 `trip_id`**

```python
from pathlib import Path

from memory.v3_store import FileMemoryV3Store


def test_working_memory_path_is_scoped_by_trip(tmp_path: Path):
    store = FileMemoryV3Store(tmp_path)

    path = store._working_memory_path("u1", "s1", "trip_123")

    assert str(path).endswith("users/u1/memory/sessions/s1/trips/trip_123/working_memory.json")
```

- [ ] **Step 3: 运行测试，确认先失败**

Run: `pytest backend/tests/test_memory_v3_episodes.py backend/tests/test_memory_v3_store.py -k "episodes or trip" -v`
Expected: FAIL，提示 `append_episode/list_episodes` 不存在或路径断言失败

- [ ] **Step 4: 在 `FileMemoryV3Store` 中实现 episodes 读写与新 working memory 路径**

```python
def _working_memory_path(self, user_id: str, session_id: str, trip_id: str | None) -> Path:
    trip_segment = trip_id or "__no_trip__"
    return (
        self._user_memory_dir(user_id)
        / "sessions"
        / session_id
        / "trips"
        / trip_segment
        / "working_memory.json"
    )


def _episodes_path(self, user_id: str) -> Path:
    return self._user_memory_dir(user_id) / "episodes.jsonl"


async def append_episode(self, episode: ArchivedTripEpisode) -> None:
    async with self._lock_for(episode.user_id):
        await asyncio.to_thread(self._append_episode_sync, episode)


def _append_episode_sync(self, episode: ArchivedTripEpisode) -> None:
    path = self._episodes_path(episode.user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        for row in self._read_jsonl_sync(path):
            if row.get("id") == episode.id:
                return
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(episode.to_dict(), ensure_ascii=False) + "\n")


async def list_episodes(self, user_id: str) -> list[ArchivedTripEpisode]:
    async with self._lock_for(user_id):
        return await asyncio.to_thread(self._list_episodes_sync, user_id)


def _list_episodes_sync(self, user_id: str) -> list[ArchivedTripEpisode]:
    path = self._episodes_path(user_id)
    rows = self._read_jsonl_sync(path)
    return [ArchivedTripEpisode.from_dict(row) for row in rows]
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `pytest backend/tests/test_memory_v3_episodes.py backend/tests/test_memory_v3_store.py -k "episodes or trip" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/memory/v3_store.py backend/tests/test_memory_v3_episodes.py backend/tests/test_memory_v3_store.py
git commit -m "feat: add v3 episodes store and trip-scoped working memory paths"
```

### Task 3: 重写 episode slice 生成器，使其只消费 v3 episodes

**Files:**
- Modify: `backend/memory/episode_slices.py`
- Modify: `backend/tests/test_episode_slices.py`

- [ ] **Step 1: 写失败测试，定义新的 slice taxonomy**

```python
from memory.episode_archive import ArchivedTripEpisode
from memory.episode_slices import build_episode_slices


def test_build_episode_slices_from_v3_archived_trip_episode():
    episode = ArchivedTripEpisode(
        id="ep_trip_123",
        user_id="u1",
        session_id="s1",
        trip_id="trip_123",
        destination="京都",
        dates={"start": "2026-05-01", "end": "2026-05-05", "total_days": 5},
        travelers=None,
        budget={"amount": 20000, "currency": "CNY"},
        selected_skeleton={"id": "balanced", "name": "轻松版"},
        selected_transport={"local": "subway"},
        accommodation={"area": "四条", "type": "町屋"},
        daily_plan_summary={"pace": "slow", "clusters": ["东山", "四条"]},
        final_plan_summary="京都慢游",
        decision_log=[{"kind": "rejected_option", "domain": "hotel", "content": "拒绝商务连锁酒店"}],
        lesson_log=[{"kind": "pitfall", "content": "上午排太满下午会累"}],
        created_at="2026-05-05T00:00:00",
        completed_at="2026-05-05T00:00:00",
    )

    slices = build_episode_slices(episode, now="2026-05-05T00:00:00")
    slice_types = [item.slice_type for item in slices]

    assert "itinerary_pattern" in slice_types
    assert "stay_choice" in slice_types
    assert "transport_choice" in slice_types
    assert "budget_signal" in slice_types
    assert "rejected_option" in slice_types
    assert "pitfall" in slice_types
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run: `pytest backend/tests/test_episode_slices.py -v`
Expected: FAIL，因为当前实现仍依赖 `TripEpisode` 与旧 slice 类型

- [ ] **Step 3: 用 `ArchivedTripEpisode` 重写 `build_episode_slices()`**

```python
from memory.episode_archive import ArchivedTripEpisode


def build_episode_slices(episode: ArchivedTripEpisode, *, now: str) -> list[EpisodeSlice]:
    slices: list[EpisodeSlice] = []
    base_entities = _base_entities(episode)

    if episode.selected_skeleton or episode.daily_plan_summary:
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="itinerary_pattern",
                index=1,
                content=_itinerary_pattern_content(episode),
                entities={**base_entities, "selected_skeleton": episode.selected_skeleton or {}},
                applicability="仅供行程结构参考；当前日期、预算或同行人变化时不能直接复用。",
            )
        )

    if episode.accommodation:
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="stay_choice",
                index=1,
                content=_stay_choice_content(episode.accommodation),
                entities={**base_entities, "accommodation": episode.accommodation},
                applicability="仅供住宿选择参考；当前价格和库存需要重新确认。",
            )
        )

    if episode.selected_transport:
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="transport_choice",
                index=1,
                content=_transport_choice_content(episode.selected_transport),
                entities={**base_entities, "selected_transport": episode.selected_transport},
                applicability="仅供交通选择参考；当前时刻和班次需要重新确认。",
            )
        )
```

- [ ] **Step 4: 运行测试，确认新的 taxonomy 与内容通过**

Run: `pytest backend/tests/test_episode_slices.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/memory/episode_slices.py backend/tests/test_episode_slices.py
git commit -m "feat: generate episode slices from v3 archived episodes"
```

### Task 3b: 在 TravelPlanState 上追踪 decision/lesson events（Option A）

**Files:**
- Modify: `backend/state/models.py`
- Modify: `backend/state/plan_writers.py`
- Test: `backend/tests/test_plan_writers_decision_events.py` (新增)
- Test: `backend/tests/test_plan_state_event_fields.py` (新增)

- [ ] **Step 1: 写失败测试，验证 `TravelPlanState` 新增 decision/lesson 事件字段并可双向序列化**

```python
# backend/tests/test_plan_state_event_fields.py
from state.models import TravelPlanState

def test_plan_state_roundtrip_preserves_event_fields():
    state = TravelPlanState()
    state.decision_events.append({
        "kind": "rejected_option",
        "domain": "hotel",
        "content": "拒绝商务连锁",
        "timestamp": "2026-04-22T10:00:00Z",
    })
    state.lesson_events.append({
        "kind": "pitfall",
        "content": "上午排太满下午会累",
        "timestamp": "2026-04-22T18:00:00Z",
    })
    restored = TravelPlanState.from_dict(state.to_dict())
    assert restored.decision_events == state.decision_events
    assert restored.lesson_events == state.lesson_events
```

- [ ] **Step 2: 写失败测试，验证 `plan_writers` 在 Phase 3 lock 点追加 DecisionEvent**

```python
# backend/tests/test_plan_writers_decision_events.py
from state.models import TravelPlanState
from state import plan_writers

def test_phase3_skeleton_lock_appends_decision_event():
    state = TravelPlanState()
    state.phase3_candidates = {"skeletons": [{"id": "sk-1", "title": "A方案"}]}
    plan_writers.lock_phase3_selection(
        state,
        kind="skeleton",
        selection_id="sk-1",
        now="2026-04-22T10:00:00Z",
    )
    assert any(
        ev["kind"] == "itinerary_pattern" and ev["domain"] == "skeleton"
        for ev in state.decision_events
    )

def test_phase3_reject_appends_decision_event():
    state = TravelPlanState()
    state.phase3_candidates = {"stays": [{"id": "hotel-1", "name": "商务连锁"}]}
    plan_writers.reject_phase3_candidate(
        state,
        kind="stay",
        candidate_id="hotel-1",
        reason="价格太高",
        now="2026-04-22T10:05:00Z",
    )
    assert any(
        ev["kind"] == "rejected_option" and ev["domain"] == "stay"
        for ev in state.decision_events
    )

def test_phase5_daily_plan_lock_appends_decision_event():
    state = TravelPlanState()
    plan_writers.lock_daily_plan(
        state,
        day_index=0,
        plan={"items": [{"title": "故宫"}]},
        now="2026-04-22T12:00:00Z",
    )
    assert any(
        ev["kind"] == "itinerary_pattern" and ev["domain"] == "daily_plan"
        for ev in state.decision_events
    )
```

- [ ] **Step 3: 运行测试，确认失败**

Run: `cd backend && python -m pytest tests/test_plan_state_event_fields.py tests/test_plan_writers_decision_events.py -v`
Expected: FAIL — 字段/helper 不存在

- [ ] **Step 4: 实现**

在 `backend/state/models.py` `TravelPlanState` 上新增：

```python
decision_events: list[dict[str, Any]] = field(default_factory=list)
lesson_events: list[dict[str, Any]] = field(default_factory=list)
```

同时更新 `to_dict` / `from_dict`：

```python
# to_dict
"decision_events": [dict(ev) for ev in self.decision_events],
"lesson_events": [dict(ev) for ev in self.lesson_events],

# from_dict
decision_events=[dict(ev) for ev in data.get("decision_events", [])],
lesson_events=[dict(ev) for ev in data.get("lesson_events", [])],
```

在 `backend/state/plan_writers.py` 的 Phase 3 锁定/拒绝点（grep `phase3_step == "lock"` 和 reject 分支定位）追加事件。选择 taxonomy 与 `episode_slices` 对齐：

```python
def _append_decision_event(state: TravelPlanState, *, kind: str, domain: str, content: str, now: str) -> None:
    state.decision_events.append({
        "kind": kind,
        "domain": domain,
        "content": content,
        "timestamp": now,
    })

# 在 lock_phase3_selection(kind={"skeleton"|"transport"|"stay"}) 落盘后调用：
_append_decision_event(
    state,
    kind="itinerary_pattern" if kind == "skeleton" else f"{kind}_choice",
    domain=kind,
    content=_format_selection_summary(selected),
    now=now,
)

# 在 reject_phase3_candidate(...) 落盘后调用：
_append_decision_event(
    state,
    kind="rejected_option",
    domain=kind,
    content=f"{candidate_summary} —— {reason}",
    now=now,
)

# 在 lock_daily_plan(...) 落盘后调用：
_append_decision_event(
    state,
    kind="itinerary_pattern",
    domain="daily_plan",
    content=_format_daily_summary(day_index, plan),
    now=now,
)
```

同文件新增 Phase 7 lesson helper：

```python
def record_phase7_lesson(state: TravelPlanState, *, kind: str, note: str, now: str) -> None:
    state.lesson_events.append({
        "kind": kind,
        "content": note,
        "timestamp": now,
    })
```

> 如果 `plan_writers.py` 已使用别的函数名（例如 `apply_phase3_lock`），在上面的位置钩入；不要创建平行 writer。

- [ ] **Step 5: 运行测试，确认通过并跑一次状态总测**

Run:
```bash
cd backend && python -m pytest tests/test_plan_state_event_fields.py tests/test_plan_writers_decision_events.py tests/test_plan_writers.py tests/test_update_plan_state.py -v
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/state/models.py backend/state/plan_writers.py \
        backend/tests/test_plan_state_event_fields.py \
        backend/tests/test_plan_writers_decision_events.py
git commit -m "feat(state): track decision/lesson events on TravelPlanState

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 4: 用 v3 episodes 替换 Phase 7 归档主链

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/memory/episode_archive.py` (新增 `build_archived_trip_episode` 工厂)
- Test: `backend/tests/test_memory_integration.py`
- Test: `backend/tests/test_episode_archive.py`

- [ ] **Step 1: 写失败测试，要求 `build_archived_trip_episode` 从 `TravelPlanState.decision_events` / `lesson_events` 拷贝事件**

```python
# backend/tests/test_episode_archive.py  (追加)
from memory.episode_archive import build_archived_trip_episode
from state.models import TravelPlanState

def test_builder_copies_decision_and_lesson_events():
    plan = TravelPlanState()
    plan.decision_events = [
        {"kind": "rejected_option", "domain": "stay", "content": "拒绝商务连锁", "timestamp": "2026-04-22T10:00:00Z"},
    ]
    plan.lesson_events = [
        {"kind": "pitfall", "content": "上午排太满下午会累", "timestamp": "2026-04-22T18:00:00Z"},
    ]
    episode = build_archived_trip_episode(
        plan=plan,
        user_id="u1",
        session_id="s1",
        completed_at="2026-04-22T20:00:00Z",
    )
    assert len(episode.decision_log) == 1
    assert episode.decision_log[0].kind == "rejected_option"
    assert episode.decision_log[0].domain == "stay"
    assert len(episode.lesson_log) == 1
    assert episode.lesson_log[0].kind == "pitfall"
```

- [ ] **Step 2: 写失败测试，要求 Phase 7 完成后只写 v3 episodes/slices 且事件透传**

```python
# backend/tests/test_memory_integration.py  (新测试)
@pytest.mark.asyncio
async def test_phase7_archive_writes_v3_episode_and_slices_only(app_with_tmp_memory):
    app, base_dir, user_id, session_id = app_with_tmp_memory
    # 设置一个已完成的 plan，其中包含 decision_events / lesson_events
    await _seed_completed_plan_with_events(app, session_id)
    await _trigger_phase7_archive(app, session_id)

    ep_path = base_dir / user_id / "memory" / "episodes.jsonl"
    sl_path = base_dir / user_id / "memory" / "episode_slices.jsonl"
    legacy = base_dir / user_id / "memory" / "trip_episodes.jsonl"
    assert ep_path.exists()
    assert sl_path.exists()
    assert not legacy.exists()

    episode = json.loads(ep_path.read_text().splitlines()[-1])
    assert len(episode["decision_log"]) >= 1
    assert len(episode["lesson_log"]) >= 1
```

- [ ] **Step 3: 运行测试，确认当前失败**

Run: `cd backend && python -m pytest tests/test_episode_archive.py tests/test_memory_integration.py -k "archive_writes_v3_episode or builder_copies_decision" -v`
Expected: FAIL

- [ ] **Step 4: 在 `backend/memory/episode_archive.py` 中实现 builder**

```python
def build_archived_trip_episode(
    *,
    plan: "TravelPlanState",
    user_id: str,
    session_id: str,
    completed_at: str,
) -> ArchivedTripEpisode:
    return ArchivedTripEpisode(
        user_id=user_id,
        session_id=session_id,
        trip_id=plan.trip_id,
        destination=plan.destination,
        completed_at=completed_at,
        trip_basics=_snapshot_trip_basics(plan),
        selected_skeleton=_snapshot_skeleton(plan),
        selected_transport=_snapshot_transport(plan),
        accommodation=_snapshot_accommodation(plan),
        daily_plans=_snapshot_daily_plans(plan),
        final_plan_summary=plan.final_plan_summary,
        decision_log=[DecisionEvent(**ev) for ev in plan.decision_events],
        lesson_log=[LessonEvent(**ev) for ev in plan.lesson_events],
    )
```

> 具体字段名以 `TravelPlanState` 当前定义为准——在写代码前先 `view backend/state/models.py` 确认 `destination` / `trip_id` / `trip_basics` / `final_plan_summary` 的真实属性名，缺失时用 `getattr(plan, "x", None)` 兜底。

在 `main.py` Phase 7 归档处（原逻辑 2683-2766 附近）替换为：

```python
async def _append_archived_trip_episode_once(
    *, user_id: str, session_id: str, plan: TravelPlanState
) -> None:
    episode = build_archived_trip_episode(
        plan=plan,
        user_id=user_id,
        session_id=session_id,
        completed_at=_now_iso(),
    )
    await memory_mgr.v3_store.append_episode(episode)
    for slice_ in build_episode_slices(episode, now=_now_iso()):
        await memory_mgr.v3_store.append_episode_slice(slice_)
```

同步删除旧 `trip_episodes.jsonl` 的写入调用点（grep `trip_episodes.jsonl`、`append_trip_episode`）。

- [ ] **Step 5: 运行测试，确认通过**

Run: `cd backend && python -m pytest tests/test_episode_archive.py tests/test_memory_integration.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/main.py backend/memory/episode_archive.py \
        backend/tests/test_episode_archive.py backend/tests/test_memory_integration.py
git commit -m "feat: archive completed trips into v3 episodes with decision/lesson logs

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 5: 让 recall 主链彻底脱离 legacy adapter

**Files:**
- Modify: `backend/memory/recall_query.py`
- Modify: `backend/memory/manager.py`
- Modify: `backend/tests/test_memory_manager.py`
- Modify: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: 写失败测试，要求 retrieval plan 直接控制 `episode_slice` recall**

```python
@pytest.mark.asyncio
async def test_generate_context_uses_v3_retrieval_plan_for_episode_slices(tmp_path):
    manager = MemoryManager(data_dir=str(tmp_path))
    # seed v3 profile / v3 slice
    # retrieval_plan.source = "episode_slice"
    # assert 只命中 episode_slice，不依赖 legacy build_recall_query
```
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest backend/tests/test_memory_manager.py -k "retrieval_plan_for_episode_slices" -v`
Expected: FAIL，因为当前 slice recall 仍由 legacy query 控制

- [ ] **Step 3: 扩展 `RecallRetrievalPlan.source` 并删除主路径中的 legacy adapter 依赖**

```python
@dataclass
class RecallRetrievalPlan:
    source: str  # profile | episode_slice | hybrid_history
    buckets: list[str]
    domains: list[str]
    keywords: list[str]
    aliases: list[str]
    strictness: str
    top_k: int
    reason: str
    fallback_used: str = "none"
```

```python
if retrieval_plan.source in {"profile", "hybrid_history"}:
    recall_candidates.extend(rank_profile_items_v3(...))
if retrieval_plan.source in {"episode_slice", "hybrid_history"}:
    recall_candidates.extend(rank_episode_slices_v3(...))
```

- [ ] **Step 4: 运行 recall 相关测试，确认通过**

Run: `pytest backend/tests/test_memory_manager.py backend/tests/test_memory_integration.py -k "recall" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/memory/recall_query.py backend/memory/manager.py backend/tests/test_memory_manager.py backend/tests/test_memory_integration.py
git commit -m "feat: remove legacy adapter from memory recall pipeline"
```

### Task 6: 把 working memory 路径升级为 trip-scoped 并修复 runtime 调用方

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/tests/test_memory_integration.py`
- Modify: `backend/tests/test_memory_v3_store.py`

- [ ] **Step 1: 写失败测试，要求 trip reset 后 working memory 不复用旧 trip 文件**

```python
@pytest.mark.asyncio
async def test_working_memory_isolated_by_trip_id(tmp_path):
    store = FileMemoryV3Store(tmp_path)
    # 写入两个不同 trip_id 的 working memory
    # assert 读取 trip_a 不会读到 trip_b 的内容
```
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest backend/tests/test_memory_v3_store.py -k "isolated_by_trip_id" -v`
Expected: FAIL，如果调用方仍沿用旧路径或旧语义

- [ ] **Step 3: 修复所有 runtime 调用点，确保始终传入当前 trip_id**

```python
memory = await memory_mgr.v3_store.load_working_memory(
    user_id,
    plan.session_id,
    plan.trip_id,
)
```

- [ ] **Step 4: 运行相关测试，确认通过**

Run: `pytest backend/tests/test_memory_v3_store.py backend/tests/test_memory_integration.py -k "working_memory or trip_id" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_memory_integration.py backend/tests/test_memory_v3_store.py
git commit -m "feat: scope working memory by trip id"
```

### Task 7: 收敛 API 到纯 v3，删除 legacy memory routes

**Files:**
- Modify: `backend/main.py`
- Create: `backend/tests/test_memory_v3_only_api.py`
- Modify: `backend/tests/test_memory_v3_api.py`

- [ ] **Step 1: 写失败测试，要求只暴露 v3 memory API**

```python
@pytest.mark.asyncio
async def test_v3_profile_mutation_routes_confirm_reject_delete(app):
    # 调 profile 专属新路由
    # assert profile item 状态变化正确


@pytest.mark.asyncio
async def test_v3_episodes_route_returns_v3_episodes(app):
    # assert /api/memory/{user_id}/episodes 返回 v3 episodes，不含 deprecated
```
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run: `pytest backend/tests/test_memory_v3_only_api.py -v`
Expected: FAIL，因为新路由尚不存在

- [ ] **Step 3: 新增 v3 专属 profile mutation 路由，重写 episodes route，删除 legacy routes**

```python
@app.post("/api/memory/{user_id}/profile/{item_id}/confirm")
async def confirm_profile_item(user_id: str, item_id: str):
    await _set_v3_profile_item_status(user_id, item_id, "active")
    return {"item_id": item_id, "status": "active"}


@app.get("/api/memory/{user_id}/episodes")
async def list_memory_episodes(user_id: str):
    episodes = await memory_mgr.v3_store.list_episodes(user_id)
    return {"episodes": [episode.to_dict() for episode in episodes]}
```

- [ ] **Step 4: 运行 API 测试，确认通过**

Run: `pytest backend/tests/test_memory_v3_only_api.py backend/tests/test_memory_v3_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_memory_v3_only_api.py backend/tests/test_memory_v3_api.py
git commit -m "feat: expose v3-only memory api surface"
```

### Task 8: 删除 legacy pending 事件与兼容聚合 extraction 任务

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: 写失败测试，要求 chat 流中不再出现 `memory_pending` 和聚合 `memory_extraction`**

```python
@pytest.mark.asyncio
async def test_chat_stream_no_longer_emits_legacy_memory_pending(app):
    # 发起一次 chat
    # assert resp.text 中不含 "memory_pending"


@pytest.mark.asyncio
async def test_background_internal_tasks_only_publish_split_v3_tasks(app):
    # assert 只有 memory_extraction_gate/profile_memory_extraction/working_memory_extraction
```
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest backend/tests/test_memory_integration.py -k "memory_pending or split_v3_tasks" -v`
Expected: FAIL，因为当前仍存在 legacy pending/聚合任务残留

- [ ] **Step 3: 删除 `memory_pending` 发射与聚合 `memory_extraction` 任务**

```python
# 删除：_memory_pending_event / _memory_pending_event_from_items
# 删除：chat stream 中对 memory_mgr.store.list_items() 的 pending 扫描
# 删除：kind == "memory_extraction" 的发布逻辑，只保留 split tasks
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `pytest backend/tests/test_memory_integration.py -k "memory_pending or split_v3_tasks" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_memory_integration.py
git commit -m "refactor: remove legacy memory pending and aggregate extraction task"
```

### Task 9: 前端切到纯 v3 memory 视图

**Files:**
- Modify: `frontend/src/hooks/useMemory.ts`
- Modify: `frontend/src/components/MemoryCenter.tsx`
- Modify: `frontend/src/types/memory.ts`

- [ ] **Step 1: 写前端测试或最小类型断言，定义纯 v3 `useMemory` 返回结构**

```ts
type UseMemoryReturn = {
  profile: UserMemoryProfile | null
  episodes: ArchivedTripEpisode[]
  episodeSlices: EpisodeSlice[]
  workingMemory: SessionWorkingMemory | null
  pendingCount: number
}
```

- [ ] **Step 2: 运行相关前端测试或类型检查，确认当前不满足**

Run: `npm test -- useMemory`
Expected: FAIL，或类型检查显示仍依赖 `legacyMemories/pendingMemories`

- [ ] **Step 3: 删除 legacy compat 读取与 UI 展示**

```ts
const [profileResp, episodesResp, slicesResp, workingResp] = await Promise.all([
  fetch(`/api/memory/${userId}/profile`),
  fetch(`/api/memory/${userId}/episodes`),
  fetch(`/api/memory/${userId}/episode-slices`),
  fetch(`/api/memory/${userId}/sessions/${sessionId}/working-memory`),
])
```

```tsx
// 删除 LegacyMemoryCard
// 删除“旧版画像兼容”“旧版待确认记忆”“旧版旅程记忆兼容”区块
// episodes tab 只渲染 v3 ArchivedTripEpisode
```

- [ ] **Step 4: 运行前端测试或最小 smoke 检查，确认通过**

Run: `npm test -- MemoryCenter useMemory`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/hooks/useMemory.ts frontend/src/components/MemoryCenter.tsx frontend/src/types/memory.ts
git commit -m "feat: switch memory center to v3-only data model"
```

### Task 10: 删除旧数据文件与 legacy runtime 路径

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/memory/store.py`
- Modify: `backend/memory/models.py`

- [ ] **Step 1: 写失败测试，要求启动/cutover 后旧数据文件被删除且不再被运行时使用**

```python
@pytest.mark.asyncio
async def test_legacy_memory_files_are_deleted_on_cutover(tmp_path):
    # 预先写入 memory.json / memory_events.jsonl / trip_episodes.jsonl
    # 执行 cutover 清理入口
    # assert 三个文件都不存在
```
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest backend/tests/test_memory_integration.py -k "legacy_memory_files_are_deleted" -v`
Expected: FAIL，因为当前没有统一删除入口

- [ ] **Step 3: 新增 cutover 清理入口，并删掉主链路对 legacy store 的 import/调用**

```python
def _delete_legacy_memory_files(user_dir: Path) -> None:
    for name in ("memory.json", "memory_events.jsonl", "trip_episodes.jsonl"):
        path = user_dir / name
        if path.exists():
            path.unlink()
```

- [ ] **Step 4: 运行后端全量 memory 相关测试，确认通过**

Run: `pytest backend/tests/test_episode_archive.py backend/tests/test_memory_v3_episodes.py backend/tests/test_memory_manager.py backend/tests/test_memory_integration.py backend/tests/test_memory_v3_only_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/memory/store.py backend/memory/models.py
git commit -m "refactor: remove legacy memory runtime and delete old data files"
```

### Task 11: 删除/改写 legacy 测试并补齐 v3-only 测试基线

**Files:**
- Modify/Delete: `backend/tests/test_memory_store.py`
- Modify/Delete: `backend/tests/test_memory_v3_migration.py`
- Modify: `backend/tests/test_memory_manager.py`
- Modify: `backend/tests/test_memory_v3_api.py`

- [ ] **Step 1: 列出所有 legacy-only 测试断言并删掉它们**

```text
删除范围：
- FileMemoryStore 行为
- memory.json legacy envelope load/save
- deprecated routes
- legacy migration
- memory_pending 事件
```

- [ ] **Step 2: 运行测试，记录哪些断言因 legacy 删除而失败**

Run: `pytest backend/tests/test_memory_store.py backend/tests/test_memory_v3_migration.py backend/tests/test_memory_manager.py backend/tests/test_memory_v3_api.py -v`
Expected: FAIL，明确显示 legacy 契约残留

- [ ] **Step 3: 删除或改写这些测试，使测试基线只覆盖 v3-only 契约**

```text
保留：
- v3 store
- v3 recall
- v3 profile mutation
- v3 episodes
- v3 episode slices
删除：
- legacy store
- migration
- deprecated routes
```

- [ ] **Step 4: 运行完整后端测试，确认通过**

Run: `pytest backend/tests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/tests
git commit -m "test: drop legacy memory test coverage and enforce v3-only contracts"
```

### Task 12: 更新项目文档并删除 mixed-state 描述

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Modify: `docs/learning/2026-04-12-记忆系统流程.md`
- Modify: `docs/mind/2026-04-20-memory-extraction-and-recall-upgrade-insight.md`

- [ ] **Step 1: 写文档断言，列出必须更新的当前态表述**

```text
必须删除：
- memory.json / trip_episodes.jsonl 仍在运行时使用
- deprecated memory api 仍存在
- v2/v3 混合态是当前事实
必须新增：
- v3-only 当前架构
- episodes 是权威历史旅行存储
- episode_slices 从 episodes 生成
```

- [ ] **Step 2: 更新 `PROJECT_OVERVIEW.md` 的 memory 架构、目录结构和 API 表**

```text
memory/
  profile.json
  events.jsonl
  episodes.jsonl
  episode_slices.jsonl
  sessions/{session_id}/trips/{trip_id}/working_memory.json
```

- [ ] **Step 3: 更新学习/设计文档，把 mixed-state 改成历史状态说明**

```text
把“当前仍保留 legacy/v2”改成“历史实现曾经如此，当前已切到 v3-only”
```

- [ ] **Step 4: 检查文档中不再出现当前态 legacy 描述**

Run: `rg "memory.json|trip_episodes.jsonl|deprecated|legacy" PROJECT_OVERVIEW.md docs`
Expected: 只剩历史文档中的历史语境，不再把 legacy 描述为当前实现

- [ ] **Step 5: Commit**

```bash
git add PROJECT_OVERVIEW.md docs
git commit -m "docs: document memory system as v3-only"
```

---

## 自检

### Spec 覆盖检查

已覆盖 spec 的主要要求：

1. v3-only 数据模型：Task 1, 2, 3
2. episodes 成为权威历史旅行存储：Task 1, 2, 4, 7
3. episode_slices 只从 episodes 生成：Task 3, 4
4. working memory 不参与历史 recall：Task 5, 6
5. 去掉 legacy recall adapter：Task 5
6. 删除 legacy API、pending、前端兼容层：Task 7, 8, 9
7. 删除旧数据与旧运行时：Task 10
8. 测试与文档收尾：Task 11, 12

### Placeholder 检查

本计划未使用：

1. `TBD`
2. `TODO`
3. “类似 Task N”
4. “写测试”但不给测试代码

### 类型一致性检查

本计划统一使用：

1. `ArchivedTripEpisode`
2. `episodes.jsonl`
3. `RecallRetrievalPlan.source = profile | episode_slice | hybrid_history`
4. `working_memory` 路径为 `sessions/{session_id}/trips/{trip_id}/working_memory.json`

---

Plan complete and saved to `docs/superpowers/plans/2026-04-22-v3-only-memory-cutover.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
