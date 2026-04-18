# Phase 7 双文档交付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Phase 7 末尾一次性向用户交付两份可下载的 Markdown 文档——完整旅行计划（`travel_plan.md`）与出发前准备清单（`checklist.md`）。

**Architecture:** 后端新增纯函数 markdown 渲染器将 `TravelPlanState` 确定性渲染为行程文档；Phase 7 末尾 LLM 调用改造后的 `generate_summary` 工具并把扫描出来的清单以 `checklist_markdown` 字符串参数传入；工具返回文档内容由 `main.py` 统一落盘到 `backend/data/sessions/sess_*/deliverables/` 并更新 `plan.deliverables` 元数据；新增 SSE `deliverable_ready` 事件与下载端点，前端 `DeliverablesCard` 组件展示下载链接，切回历史会话时从 `plan.deliverables` 回显。

**Tech Stack:** Python 3.12 / FastAPI / dataclasses / pytest / pytest-asyncio / React 19 / TypeScript / Vite / ReactMarkdown

**Context:** 当前 Phase 5 完成只写 `daily_plans[]` 到状态，Phase 7 的 `generate_summary`（`backend/tools/generate_summary.py:18-105`）只返回一段纯文本摘要，存在"交付断层"——用户离开前端就失去所有沉淀。本次引入两份 Markdown 落盘文档作为正式交付物。

**设计决策（已与用户对齐）：**
- 交付时机：Phase 7 末尾一次性交付两份
- 形态：Markdown 落盘 + 前端可下载
- 清单生成：沿用现有 Phase 7 prompt 逻辑，由 LLM 把清单作为参数提交
- 计划粒度：只渲染现有 plan 字段，不新增 state 模型收集预订号/票号

---

## File Structure

**后端（新建 / 修改）：**
- Create: `backend/state/markdown_renderer.py` —— 纯函数 `render_travel_plan_markdown(plan)`，单一职责：把 state 渲染成 Markdown（**只做容错渲染，不做业务拒绝**）
- Modify: `backend/state/manager.py` —— 新增 `save_deliverable(session_id, filename, content) -> str` 与 `clear_deliverables(session_id) -> None`（物理删除 md 文件）
- Modify: `backend/state/models.py` —— `TravelPlanState` 新增 `deliverables: dict | None` 字段并在 `to_dict` / `from_dict` 对称持久化；把 `deliverables` 加入 `_PHASE_DOWNSTREAM` 的 phase 1/3/5 下游，`clear_downstream` 负责把字段重置为 `None`
- Modify: `backend/phase/backtrack.py` —— 执行回退时同步调用 `state_mgr.clear_deliverables(session_id)`，物理删除旧文件
- Modify: `backend/tools/generate_summary.py` —— 新增 `checklist_markdown` 必填参数 + 严格校验（长度 + 结构）；工具层 **precondition check**：若 daily_plans 覆盖数 < `dates.total_days` 则直接 fail 不生成任何文档；返回值含两份文档原始内容
- Modify: `backend/phase/prompts.py` —— Phase 7 prompt 段落补充 `checklist_markdown` 使用说明与模板
- Modify: `backend/main.py` —— 在 `generate_summary` 成功后按**严格顺序**：`save_deliverable(travel_plan.md)` → `save_deliverable(checklist.md)` → `plan.deliverables = {...}` → `state_mgr.save(plan)` → 发送 `deliverable_ready` SSE 事件；任意一步失败则不发事件，直接冒泡错误。新增下载端点 `GET /api/sessions/{session_id}/deliverables/{filename}`（白名单 + session 存在/未软删校验）

**后端测试：**
- Create: `backend/tests/test_markdown_renderer.py`
- Modify: `backend/tests/test_state_manager.py`（新增 `save_deliverable` 用例）
- Modify: `backend/tests/test_generate_summary.py`（新增 `checklist_markdown` 参数用例）
- Modify: `backend/tests/test_main.py`（新增下载端点用例；若无此文件则新建 `backend/tests/test_deliverable_api.py`）

**前端：**
- Modify: `frontend/src/types/plan.ts` —— SSE 事件联合类型新增 `deliverable_ready`；`TravelPlanState` 接口新增 `deliverables?: { travel_plan_md, checklist_md, ready_at }`
- Create: `frontend/src/components/DeliverablesCard.tsx` —— 展示两张下载卡片
- Modify: `frontend/src/components/ChatPanel.tsx` —— 处理 `deliverable_ready` 事件
- Modify: `frontend/src/App.tsx` —— `openSession` 回显已存在的 deliverables

**文档：**
- Modify: `PROJECT_OVERVIEW.md` —— 更新 Phase 7 段、SSE 事件表、API 端点表、目录结构

---

## Task 1：Markdown 渲染器纯函数（TDD）

**Files:**
- Create: `backend/state/markdown_renderer.py`
- Test: `backend/tests/test_markdown_renderer.py`

- [ ] **Step 1: 写失败测试**

文件：`backend/tests/test_markdown_renderer.py`

```python
# backend/tests/test_markdown_renderer.py
from state.markdown_renderer import render_travel_plan_markdown
from state.models import (
    Accommodation,
    Activity,
    Budget,
    DateRange,
    DayPlan,
    Location,
    Preference,
    TravelPlanState,
    Travelers,
)


def _minimal_plan() -> TravelPlanState:
    return TravelPlanState(
        session_id="sess_aaaaaaaaaaaa",
        phase=7,
        destination="东京",
        dates=DateRange(start="2026-05-01", end="2026-05-05"),
        travelers=Travelers(adults=2, children=1),
        budget=Budget(total=20000, currency="CNY"),
        accommodation=Accommodation(area="新宿", hotel="Park Hyatt"),
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-05-01",
                activities=[
                    Activity(
                        name="浅草寺",
                        location=Location(name="浅草寺", address="东京都台东区"),
                        start_time="09:00",
                        end_time="11:30",
                        category="文化",
                        cost=0,
                        notes="早去避开人潮",
                    )
                ],
            )
        ],
        preferences=[Preference(key="pace", value="轻松")],
    )


def test_renders_header_with_core_fields():
    md = render_travel_plan_markdown(_minimal_plan())
    assert "# 东京" in md
    assert "2026-05-01" in md and "2026-05-05" in md
    assert "2 成人" in md
    assert "1 儿童" in md
    assert "20000" in md


def test_renders_daily_sections_with_activities():
    md = render_travel_plan_markdown(_minimal_plan())
    assert "## 第 1 天" in md
    assert "2026-05-01" in md
    assert "浅草寺" in md
    assert "09:00" in md and "11:30" in md
    assert "早去避开人潮" in md


def test_renders_accommodation_and_preferences():
    md = render_travel_plan_markdown(_minimal_plan())
    assert "新宿" in md
    assert "Park Hyatt" in md
    assert "pace" in md and "轻松" in md


def test_missing_fields_are_skipped_silently():
    plan = TravelPlanState(session_id="sess_bbbbbbbbbbbb", phase=7, destination="大阪")
    md = render_travel_plan_markdown(plan)
    assert "# 大阪" in md
    assert "## 第" not in md  # no daily_plans → 无逐日 section


def test_partial_coverage_is_rendered_verbatim():
    """Renderer 保持纯函数容错：即使只覆盖部分天数也完整渲染，
    业务拒绝由 generate_summary 工具层 precondition check 负责，不在这里判断。
    """
    plan = _minimal_plan()  # total_days=5 但只填了 1 天
    md = render_travel_plan_markdown(plan)
    assert "## 第 1 天" in md
    assert "## 第 2 天" not in md
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_markdown_renderer.py -v
```

Expected: `ModuleNotFoundError: No module named 'state.markdown_renderer'`

- [ ] **Step 3: 实现 renderer**

文件：`backend/state/markdown_renderer.py`

```python
# backend/state/markdown_renderer.py
from __future__ import annotations

from state.models import TravelPlanState


def render_travel_plan_markdown(plan: TravelPlanState) -> str:
    """Render TravelPlanState into a human-readable Markdown travel plan.

    缺失字段静默跳过，不输出"暂无"占位。
    """
    lines: list[str] = []

    title = plan.destination or "旅行计划"
    lines.append(f"# {title}")
    lines.append("")

    meta: list[str] = []
    if plan.dates:
        meta.append(
            f"- **日期**：{plan.dates.start} ~ {plan.dates.end}"
            f"（共 {plan.dates.total_days} 天）"
        )
    if plan.travelers:
        parts = [f"{plan.travelers.adults} 成人"]
        if plan.travelers.children:
            parts.append(f"{plan.travelers.children} 儿童")
        meta.append(f"- **出行人数**：{'，'.join(parts)}")
    if plan.budget:
        allocated = sum(
            a.cost for d in plan.daily_plans for a in d.activities
        )
        meta.append(
            f"- **预算**：{plan.budget.total} {plan.budget.currency}"
            f"（已分配 {allocated}）"
        )
    if plan.accommodation:
        hotel = f" / {plan.accommodation.hotel}" if plan.accommodation.hotel else ""
        meta.append(f"- **住宿**：{plan.accommodation.area}{hotel}")
    if plan.selected_transport:
        meta.append(f"- **交通**：{plan.selected_transport}")
    if meta:
        lines.extend(meta)
        lines.append("")

    if plan.trip_brief:
        lines.append("## 旅行画像")
        for key, val in plan.trip_brief.items():
            lines.append(f"- **{key}**：{val}")
        lines.append("")

    if plan.preferences:
        lines.append("## 偏好")
        for p in plan.preferences:
            if p.key:
                lines.append(f"- {p.key}：{p.value}")
        lines.append("")

    if plan.constraints:
        lines.append("## 约束")
        for c in plan.constraints:
            lines.append(f"- [{c.type}] {c.description}")
        lines.append("")

    if plan.daily_plans:
        lines.append("## 逐日行程")
        lines.append("")
        for dp in sorted(plan.daily_plans, key=lambda d: d.day):
            lines.append(f"### 第 {dp.day} 天（{dp.date}）")
            if dp.notes:
                lines.append(f"> {dp.notes}")
            if not dp.activities:
                lines.append("- _无活动_")
            for a in dp.activities:
                location_str = a.location.name if a.location and a.location.name else ""
                if a.location and getattr(a.location, "address", None):
                    location_str += f"（{a.location.address}）"
                tail_bits = []
                if a.category:
                    tail_bits.append(f"[{a.category}]")
                if a.cost:
                    tail_bits.append(f"¥{a.cost}")
                tail = "  ".join(tail_bits)
                lines.append(
                    f"- **{a.start_time}–{a.end_time}** {a.name}"
                    f"{' @ ' + location_str if location_str else ''}"
                    f"{'  ' + tail if tail else ''}"
                )
                if a.transport_from_prev:
                    lines.append(
                        f"  - 交通：{a.transport_from_prev}"
                        f"（{a.transport_duration_min} 分钟）"
                    )
                if a.notes:
                    lines.append(f"  - 备注：{a.notes}")
            lines.append("")

    if plan.risks:
        lines.append("## 风险")
        for r in plan.risks:
            lines.append(f"- {r}")
        lines.append("")

    if plan.alternatives:
        lines.append("## 备选方案")
        for alt in plan.alternatives:
            lines.append(f"- {alt}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && pytest tests/test_markdown_renderer.py -v
```

Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add backend/state/markdown_renderer.py backend/tests/test_markdown_renderer.py
git commit -m "feat(state): add travel plan markdown renderer for Phase 7 deliverables"
```

---

## Task 2：StateManager.save_deliverable

**Files:**
- Modify: `backend/state/manager.py`
- Test: `backend/tests/test_state_manager.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_state_manager.py` 追加：

```python
# --- new tests for deliverables ---
import pytest


@pytest.mark.asyncio
async def test_save_deliverable_writes_file(tmp_path):
    mgr = StateManager(data_dir=str(tmp_path))
    plan = await mgr.create_session()
    path = await mgr.save_deliverable(
        plan.session_id, "travel_plan.md", "# hello\n"
    )
    saved = (tmp_path / "sessions" / plan.session_id / "deliverables" / "travel_plan.md")
    assert saved.read_text() == "# hello\n"
    assert str(saved) == path


@pytest.mark.asyncio
async def test_save_deliverable_rejects_non_whitelisted_filename(tmp_path):
    mgr = StateManager(data_dir=str(tmp_path))
    plan = await mgr.create_session()
    with pytest.raises(ValueError):
        await mgr.save_deliverable(plan.session_id, "../etc/passwd", "x")
    with pytest.raises(ValueError):
        await mgr.save_deliverable(plan.session_id, "random.txt", "x")


@pytest.mark.asyncio
async def test_save_deliverable_overwrites_existing(tmp_path):
    mgr = StateManager(data_dir=str(tmp_path))
    plan = await mgr.create_session()
    await mgr.save_deliverable(plan.session_id, "checklist.md", "v1")
    await mgr.save_deliverable(plan.session_id, "checklist.md", "v2")
    saved = (tmp_path / "sessions" / plan.session_id / "deliverables" / "checklist.md")
    assert saved.read_text() == "v2"


@pytest.mark.asyncio
async def test_clear_deliverables_removes_files(tmp_path):
    mgr = StateManager(data_dir=str(tmp_path))
    plan = await mgr.create_session()
    await mgr.save_deliverable(plan.session_id, "travel_plan.md", "x")
    await mgr.save_deliverable(plan.session_id, "checklist.md", "y")
    await mgr.clear_deliverables(plan.session_id)
    deliverables_dir = (
        tmp_path / "sessions" / plan.session_id / "deliverables"
    )
    assert not (deliverables_dir / "travel_plan.md").exists()
    assert not (deliverables_dir / "checklist.md").exists()


@pytest.mark.asyncio
async def test_clear_deliverables_is_idempotent(tmp_path):
    """目录不存在 / 文件已清空时不应抛错。"""
    mgr = StateManager(data_dir=str(tmp_path))
    plan = await mgr.create_session()
    await mgr.clear_deliverables(plan.session_id)  # 目录不存在
    await mgr.save_deliverable(plan.session_id, "travel_plan.md", "x")
    await mgr.clear_deliverables(plan.session_id)
    await mgr.clear_deliverables(plan.session_id)  # 再次清理仍 OK
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_state_manager.py -k deliverable -v
```

Expected: `AttributeError: 'StateManager' object has no attribute 'save_deliverable'`

- [ ] **Step 3: 实现 save_deliverable**

在 `backend/state/manager.py` 文件顶部常量区加入白名单：

```python
ALLOWED_DELIVERABLE_NAMES = {"travel_plan.md", "checklist.md"}
```

在类末尾追加：

```python
    async def save_deliverable(
        self, session_id: str, filename: str, content: str
    ) -> str:
        if filename not in ALLOWED_DELIVERABLE_NAMES:
            raise ValueError(
                f"deliverable filename not allowed: {filename!r}; "
                f"must be one of {sorted(ALLOWED_DELIVERABLE_NAMES)}"
            )
        deliverables_dir = self._session_dir(session_id) / "deliverables"
        deliverables_dir.mkdir(parents=True, exist_ok=True)
        path = deliverables_dir / filename
        path.write_text(content, encoding="utf-8")
        return str(path)

    async def clear_deliverables(self, session_id: str) -> None:
        """物理删除该 session 下所有白名单交付文件。幂等：文件/目录不存在不抛。"""
        deliverables_dir = self._session_dir(session_id) / "deliverables"
        if not deliverables_dir.exists():
            return
        for name in ALLOWED_DELIVERABLE_NAMES:
            target = deliverables_dir / name
            if target.exists():
                target.unlink()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && pytest tests/test_state_manager.py -v
```

Expected: all tests pass（含既有用例 + 3 个新用例）

- [ ] **Step 5: 提交**

```bash
git add backend/state/manager.py backend/tests/test_state_manager.py
git commit -m "feat(state): add save_deliverable + clear_deliverables for Phase 7"
```

---

## Task 3：TravelPlanState.deliverables 字段 + backtrack 清理集成

**Files:**
- Modify: `backend/state/models.py`
- Modify: `backend/phase/backtrack.py`
- Test: `backend/tests/test_state_manager.py`（追加）
- Test: `backend/tests/test_backtrack.py`（追加；若无此文件则新建）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_state_manager.py` 追加：

```python
def test_travel_plan_state_deliverables_roundtrip():
    from state.models import TravelPlanState

    plan = TravelPlanState(
        session_id="sess_cccccccccccc",
        deliverables={
            "travel_plan_md": "travel_plan.md",
            "checklist_md": "checklist.md",
            "ready_at": "2026-04-18T12:00:00+08:00",
        },
    )
    d = plan.to_dict()
    assert d["deliverables"]["travel_plan_md"] == "travel_plan.md"
    restored = TravelPlanState.from_dict(d)
    assert restored.deliverables == plan.deliverables


def test_travel_plan_state_deliverables_default_none():
    from state.models import TravelPlanState

    plan = TravelPlanState(session_id="sess_dddddddddddd")
    assert plan.deliverables is None
    assert plan.to_dict()["deliverables"] is None


def test_clear_downstream_from_phase5_resets_deliverables():
    from state.models import TravelPlanState

    plan = TravelPlanState(
        session_id="sess_eeeeeeeeeeee",
        deliverables={
            "travel_plan_md": "travel_plan.md",
            "checklist_md": "checklist.md",
            "ready_at": "2026-04-18T12:00:00+08:00",
        },
    )
    plan.clear_downstream(5)
    assert plan.deliverables is None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_state_manager.py -k "deliverables_roundtrip or default_none or clear_downstream_from_phase5" -v
```

Expected: `TypeError: __init__() got an unexpected keyword argument 'deliverables'` + `clear_downstream` 对新字段无感知。

- [ ] **Step 3: 在 TravelPlanState 增加字段 + 纳入 downstream**

修改 `backend/state/models.py`：

1. 在 `TravelPlanState` dataclass 字段区（`version: int = 1` 之前）插入：

```python
    deliverables: dict[str, Any] | None = None
```

2. 在 `to_dict` 返回字典中（`"version": self.version,` 之前）插入：

```python
            "deliverables": self.deliverables,
```

3. 在 `from_dict` 构造 `cls(...)` 参数中（靠近末尾，`version=...` 之前）插入：

```python
            deliverables=d.get("deliverables"),
```

4. 把 `deliverables` 加入 `_PHASE_DOWNSTREAM` 中 phase 1 / 3 / 5 三个列表的末尾：

```python
_PHASE_DOWNSTREAM: dict[int, list[str]] = {
    1: [
        # ...existing fields...
        "daily_plans",
        "deliverables",
    ],
    3: [
        # ...existing fields...
        "daily_plans",
        "deliverables",
    ],
    5: ["daily_plans", "deliverables"],
}
```

5. 在 `_FIELD_DEFAULTS` 添加：

```python
    "deliverables": None,
```

- [ ] **Step 4: 写 backtrack 物理删除测试**

文件：`backend/tests/test_backtrack.py`（追加或新建）

```python
import pytest

from phase.backtrack import execute_backtrack
from state.manager import StateManager


@pytest.mark.asyncio
async def test_backtrack_clears_deliverable_files(tmp_path):
    mgr = StateManager(data_dir=str(tmp_path))
    plan = await mgr.create_session()
    plan.phase = 7
    plan.deliverables = {
        "travel_plan_md": "travel_plan.md",
        "checklist_md": "checklist.md",
        "ready_at": "2026-04-18T12:00:00+08:00",
    }
    await mgr.save(plan)
    await mgr.save_deliverable(plan.session_id, "travel_plan.md", "# x\n")
    await mgr.save_deliverable(plan.session_id, "checklist.md", "# y\n")

    await execute_backtrack(plan, to_phase=5, reason="need more days", state_mgr=mgr)

    deliverables_dir = (
        tmp_path / "sessions" / plan.session_id / "deliverables"
    )
    assert not (deliverables_dir / "travel_plan.md").exists()
    assert not (deliverables_dir / "checklist.md").exists()
    assert plan.deliverables is None
```

> **Note**：若 `execute_backtrack` 的签名与上述不同，请在实施时按现有签名对齐。关键是"回退流程调用 `state_mgr.clear_deliverables(plan.session_id)`"这条语义。

- [ ] **Step 5: 在 backtrack.py 中调用物理删除**

修改 `backend/phase/backtrack.py`：在执行回退、`plan.clear_downstream(to_phase)` 之后、调用 `state_mgr.save(plan)` 之前（或之后，都在同一事务里），插入：

```python
await state_mgr.clear_deliverables(plan.session_id)
```

- [ ] **Step 6: 运行测试确认通过**

```bash
cd backend && pytest tests/test_state_manager.py tests/test_backtrack.py -v
```

Expected: all pass

- [ ] **Step 7: 提交**

```bash
git add backend/state/models.py backend/phase/backtrack.py \
        backend/tests/test_state_manager.py backend/tests/test_backtrack.py
git commit -m "feat(state): track deliverables in state + clear on backtrack"
```

---

## Task 4：改造 generate_summary 工具（Phase 7 Finalizer）

**Files:**
- Modify: `backend/tools/generate_summary.py`
- Test: `backend/tests/test_generate_summary.py`

> **注意**：工具是纯函数，不持有 state_manager 句柄。职责拆分：工具负责**校验前置条件 + 产出两份文档的原始内容字符串**；`main.py` 负责落盘、更新 `plan.deliverables`、发送 SSE 事件（Task 6）。
>
> 工具层必须拒绝以下场景（业务 gate，不做静默降级）：
> - `checklist_markdown` 缺失、长度不足、格式漂移（至少含 2 个指定二级标题）
> - `daily_plans` 覆盖数 ≠ `dates.total_days`（不完整行程不允许交付）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_generate_summary.py` 追加：

```python
import pytest


@pytest.fixture
def summary_tool():
    from tools.generate_summary import make_generate_summary_tool

    return make_generate_summary_tool()


@pytest.mark.asyncio
async def test_returns_both_markdown_documents(summary_tool):
    plan_data = {
        "session_id": "sess_eeeeeeeeeeee",
        "destination": "京都",
        "dates": {"start": "2026-07-01", "end": "2026-07-03"},
        "daily_plans": [
            {
                "day": 1,
                "date": "2026-07-01",
                "activities": [
                    {
                        "name": "清水寺",
                        "location": {"name": "清水寺"},
                        "start_time": "09:00",
                        "end_time": "11:00",
                        "category": "文化",
                        "cost": 100,
                    }
                ],
            }
        ],
        "budget": {"total": 8000, "currency": "CNY"},
    }
    checklist_md = "## 证件签证\n- [ ] 有效护照\n"
    result = await summary_tool(plan_data=plan_data, checklist_markdown=checklist_md)

    assert "travel_plan_markdown" in result
    assert "checklist_markdown" in result
    assert "# 京都" in result["travel_plan_markdown"]
    assert "清水寺" in result["travel_plan_markdown"]
    assert result["checklist_markdown"] == checklist_md
    # 向后兼容：原有字段保留
    assert "summary" in result and "total_days" in result


@pytest.mark.asyncio
async def test_missing_checklist_markdown_raises(summary_tool):
    from tools.base import ToolError

    with pytest.raises((TypeError, ToolError)):
        await summary_tool(plan_data={"destination": "东京"})


@pytest.mark.asyncio
async def test_checklist_markdown_is_passed_through_verbatim(summary_tool):
    raw = (
        "## 证件签证\n- [x] 护照已核查\n"
        "## 天气\n- [ ] 下载离线地图\n"
    )
    result = await summary_tool(
        plan_data={
            "destination": "东京",
            "dates": {"start": "2026-05-01", "end": "2026-05-01"},
            "daily_plans": [
                {"day": 1, "date": "2026-05-01", "activities": []}
            ],
        },
        checklist_markdown=raw,
    )
    assert result["checklist_markdown"] == raw


@pytest.mark.asyncio
async def test_checklist_too_short_is_rejected(summary_tool):
    from tools.base import ToolError

    with pytest.raises(ToolError) as exc:
        await summary_tool(
            plan_data={"destination": "东京"},
            checklist_markdown="short",
        )
    assert exc.value.error_code == "INVALID_ARGUMENTS"


@pytest.mark.asyncio
async def test_checklist_missing_required_sections_is_rejected(summary_tool):
    from tools.base import ToolError

    bad_md = "## 购物\n- [ ] 买纪念品\n" * 3
    with pytest.raises(ToolError) as exc:
        await summary_tool(
            plan_data={"destination": "东京"},
            checklist_markdown=bad_md,
        )
    assert exc.value.error_code == "INVALID_ARGUMENTS"
    assert "证件签证" in (exc.value.suggestion or "")


@pytest.mark.asyncio
async def test_refuses_when_daily_plans_incomplete(summary_tool):
    from tools.base import ToolError

    plan_data = {
        "destination": "东京",
        "dates": {"start": "2026-05-01", "end": "2026-05-05"},  # 5 天
        "daily_plans": [
            {"day": 1, "date": "2026-05-01", "activities": []},
            {"day": 2, "date": "2026-05-02", "activities": []},
        ],  # 只覆盖 2 天
    }
    checklist = (
        "## 证件签证\n- [ ] 护照\n"
        "## 天气\n- [ ] 离线地图\n"
    )
    with pytest.raises(ToolError) as exc:
        await summary_tool(plan_data=plan_data, checklist_markdown=checklist)
    assert exc.value.error_code == "INCOMPLETE_ITINERARY"
    assert "request_backtrack" in (exc.value.suggestion or "")
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_generate_summary.py -v
```

Expected: 新增用例失败（旧用例仍通过）

- [ ] **Step 3: 改造工具实现**

覆盖 `backend/tools/generate_summary.py`：

```python
# backend/tools/generate_summary.py
from __future__ import annotations

from state.markdown_renderer import render_travel_plan_markdown
from state.models import TravelPlanState
from tools.base import ToolError, tool

REQUIRED_CHECKLIST_SECTIONS = (
    "证件签证",
    "天气",
    "预订确认",
    "交通接驳",
    "应急预案",
)
MIN_REQUIRED_SECTIONS = 2

_PARAMETERS = {
    "type": "object",
    "properties": {
        "plan_data": {
            "type": "object",
            "description": "完整旅行计划数据（通常传入当前 plan 的 dict 快照）",
        },
        "checklist_markdown": {
            "type": "string",
            "description": (
                "Phase 7 扫描产出的完整出发前准备清单，必须是 Markdown 字符串。"
                "必须包含至少 2 个以下二级标题：证件签证 / 天气 / 预订确认 / "
                "交通接驳 / 应急预案；每项以 `- [ ]` 或 `- [x]` 勾选。"
                "工具会把这段内容原样写入 checklist.md。"
            ),
            "minLength": 80,
            "maxLength": 20000,
        },
    },
    "required": ["plan_data", "checklist_markdown"],
}


def _count_required_sections(md: str) -> int:
    return sum(1 for s in REQUIRED_CHECKLIST_SECTIONS if f"## {s}" in md)


def _daily_plans_coverage(plan_data: dict) -> tuple[int, int]:
    """Return (planned_days, total_days). 0/0 表示无从判断（缺 dates）。"""
    dates = plan_data.get("dates") or {}
    start, end = dates.get("start"), dates.get("end")
    if not start or not end:
        return 0, 0
    from datetime import date

    try:
        total = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    except ValueError:
        return 0, 0
    daily = plan_data.get("daily_plans") or plan_data.get("days") or []
    planned = len(daily) if isinstance(daily, list) else 0
    return planned, max(total, 0)


def make_generate_summary_tool():
    @tool(
        name="generate_summary",
        description="""Phase 7 Finalizer：生成 travel_plan.md 与 checklist.md 双交付物。
Use when: 用户处于 Phase 7，扫描已完成，daily_plans 已覆盖全部出行日期，准备交付最终文档。
Don't use when: Phase 7 扫描未完成、checklist 内容未准备好、daily_plans 不完整。
必须同时传入 plan_data 与 checklist_markdown；工具返回两份文档内容，由 runtime 负责落盘与通知前端。
调用后整个 Phase 7 不应再次调用本工具；如需修改，使用 request_backtrack(to_phase=5, ...) 回退后重做。""",
        phases=[7],
        parameters=_PARAMETERS,
        side_effect="write",
        human_label="Phase 7 Finalizer",
    )
    async def generate_trip_summary(
        plan_data: dict, checklist_markdown: str
    ) -> dict:
        if not isinstance(plan_data, dict):
            plan_data = {}

        # === Precondition 1: checklist 长度 ===
        if not isinstance(checklist_markdown, str) or len(
            checklist_markdown.strip()
        ) < 80:
            raise ToolError(
                "checklist_markdown must be a markdown string of at least 80 chars",
                error_code="INVALID_ARGUMENTS",
                suggestion=(
                    "先完成 Phase 7 全部扫描维度再组织完整清单；"
                    "至少覆盖 2 个二级标题（证件签证 / 天气 / 预订确认 / "
                    "交通接驳 / 应急预案）。"
                ),
            )

        # === Precondition 2: checklist 结构 ===
        found = _count_required_sections(checklist_markdown)
        if found < MIN_REQUIRED_SECTIONS:
            raise ToolError(
                f"checklist_markdown must contain at least {MIN_REQUIRED_SECTIONS} "
                f"of the required sections; found {found}",
                error_code="INVALID_ARGUMENTS",
                suggestion=(
                    "清单至少含 2 个二级标题，从：证件签证 / 天气 / 预订确认 / "
                    "交通接驳 / 应急预案 中挑选实际有待办的维度。"
                ),
            )

        # === Precondition 3: daily_plans 覆盖率 ===
        planned, total = _daily_plans_coverage(plan_data)
        if total and planned != total:
            raise ToolError(
                f"daily_plans covers {planned}/{total} days; cannot finalize",
                error_code="INCOMPLETE_ITINERARY",
                suggestion=(
                    f"当前逐日行程只覆盖 {planned}/{total} 天。"
                    "调用 request_backtrack(to_phase=5, reason=...) 回退 Phase 5 "
                    "补齐剩余日期后再调用本工具。"
                ),
            )

        # Render travel plan from state snapshot (best-effort reconstruction).
        try:
            plan_obj = TravelPlanState.from_dict(plan_data)
        except Exception:
            plan_obj = TravelPlanState(
                session_id=plan_data.get("session_id", "sess_000000000000"),
            )
            for attr in ("destination",):
                if plan_data.get(attr):
                    setattr(plan_obj, attr, plan_data[attr])
        travel_plan_md = render_travel_plan_markdown(plan_obj)

        # Legacy summary fields (kept for backward compat with existing UI/tests).
        destination = plan_data.get("destination", "未知目的地")
        raw_days = plan_data.get("days") or plan_data.get("daily_plans")
        days = raw_days if isinstance(raw_days, list) else []
        total_days = len(days) if days else int(plan_data.get("total_days") or 0)
        budget_raw = plan_data.get("budget", {}) or {}
        if isinstance(budget_raw, dict):
            total_budget = budget_raw.get("total", 0) or sum(
                budget_raw.get(k, 0) or 0
                for k in ("flights", "hotels", "activities", "food")
            )
        else:
            total_budget = budget_raw if isinstance(budget_raw, (int, float)) else 0

        summary = f"🗺️ {destination} · {total_days} 天 · ¥{total_budget}"

        return {
            "summary": summary,
            "total_days": total_days,
            "total_budget": total_budget,
            "travel_plan_markdown": travel_plan_md,
            "checklist_markdown": checklist_markdown,
        }

    return generate_trip_summary
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && pytest tests/test_generate_summary.py -v
```

Expected: 所有用例通过（含新老）

- [ ] **Step 5: 提交**

```bash
git add backend/tools/generate_summary.py backend/tests/test_generate_summary.py
git commit -m "feat(tools): generate_summary emits travel_plan and checklist markdown"
```

---

## Task 5：Phase 7 prompt 补充 checklist_markdown 使用说明

**Files:**
- Modify: `backend/phase/prompts.py`
- Test: `backend/tests/test_phase_prompts.py`（或追加到已有 prompts 测试文件；若无则新建）

- [ ] **Step 1: 写失败测试**

文件：`backend/tests/test_phase7_prompt.py`（新建）

```python
# backend/tests/test_phase7_prompt.py
from phase.prompts import PHASE7_PROMPT


def test_phase7_prompt_mentions_checklist_markdown_param():
    assert "checklist_markdown" in PHASE7_PROMPT


def test_phase7_prompt_contains_checklist_sections_template():
    for section in ("证件签证", "天气", "预订确认", "交通接驳", "应急预案"):
        assert section in PHASE7_PROMPT


def test_phase7_prompt_instructs_single_final_generate_summary_call():
    # 必须明确"末尾调用一次"+"同时传入 plan_data 与 checklist_markdown"
    assert "generate_summary" in PHASE7_PROMPT
    assert "plan_data" in PHASE7_PROMPT and "checklist_markdown" in PHASE7_PROMPT
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_phase7_prompt.py -v
```

Expected: 失败（检查字符串未全部出现）

- [ ] **Step 3: 更新 Phase 7 prompt 文案**

在 `backend/phase/prompts.py` 中 `PHASE7_PROMPT` 末尾追加以下内容（若已有"完成 Gate"段落则替换为以下段落）：

```text

## 完成 Gate（必须按序执行）
1. 依照扫描维度逐项核查，善用 `check_weather` / `check_availability` / `web_search`。
2. 汇总最终清单为一段 Markdown 字符串，结构如下：

   ```
   ## 证件签证
   - [ ] <具体待办> (priority: high|med|low)
   ## 天气
   - [x] <已确认项>
   ## 预订确认
   - [ ] ...
   ## 交通接驳
   - [ ] ...
   ## 应急预案
   - [ ] ...
   ```

3. 最后一次调用 `generate_summary(plan_data=<当前 plan 的 dict 快照>, checklist_markdown=<上一步整理好的清单字符串>)`。
   - 必须同时传入两个参数；缺 `checklist_markdown` 会报 `INVALID_ARGUMENTS`。
   - 调用成功后系统会自动生成 `travel_plan.md` 与 `checklist.md` 两份可下载文档，并通知用户。
   - 整个 Phase 7 只能调用一次 `generate_summary`；如需重新生成，先通过 `request_backtrack(to_phase=5, reason=...)` 回退并调整。
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && pytest tests/test_phase7_prompt.py -v
```

Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add backend/phase/prompts.py backend/tests/test_phase7_prompt.py
git commit -m "docs(prompts): teach Phase 7 to pass checklist_markdown to generate_summary"
```

---

## Task 6：main.py 严格顺序落盘 + SSE 事件 + 下载端点

> **关键时序约束**（避免"事件先于持久化"的 bug）：
> 1. `save_deliverable("travel_plan.md", ...)`
> 2. `save_deliverable("checklist.md", ...)`
> 3. `plan.deliverables = {...}`
> 4. `await state_mgr.save(plan)`
> 5. `yield` SSE `deliverable_ready`
>
> 任意一步抛错 → 不 yield 事件，错误冒泡到上层（由现有 SSE error 流程处理）；前端不会看到"假成功"。

**Files:**
- Modify: `backend/main.py`
- Test: `backend/tests/test_deliverable_api.py`（新建）

- [ ] **Step 1: 写失败测试（下载端点 + 落盘路径）**

文件：`backend/tests/test_deliverable_api.py`

```python
# backend/tests/test_deliverable_api.py
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_download_endpoint_returns_file(tmp_path, monkeypatch):
    # Arrange: point data dir to tmp and seed a fake session + deliverable
    monkeypatch.setenv("TRAVEL_AGENT_DATA_DIR", str(tmp_path))
    from importlib import reload

    import main as main_module

    reload(main_module)
    sid = "sess_ffffffffffff"
    (tmp_path / "sessions" / sid / "deliverables").mkdir(parents=True)
    (tmp_path / "sessions" / sid / "deliverables" / "travel_plan.md").write_text(
        "# hi\n", encoding="utf-8"
    )

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/sessions/{sid}/deliverables/travel_plan.md")
    assert resp.status_code == 200
    assert resp.text == "# hi\n"
    assert "text/markdown" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_download_endpoint_rejects_non_whitelisted_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVEL_AGENT_DATA_DIR", str(tmp_path))
    from importlib import reload

    import main as main_module

    reload(main_module)
    sid = "sess_ffffffffffff"
    (tmp_path / "sessions" / sid / "deliverables").mkdir(parents=True)

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/sessions/{sid}/deliverables/other.txt")
    assert resp.status_code == 400

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/sessions/{sid}/deliverables/..%2Fplan.json")
    assert resp.status_code in (400, 404)


@pytest.mark.asyncio
async def test_download_endpoint_404_when_not_generated(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAVEL_AGENT_DATA_DIR", str(tmp_path))
    from importlib import reload

    import main as main_module

    reload(main_module)
    sid = "sess_ffffffffffff"
    (tmp_path / "sessions" / sid).mkdir(parents=True)

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/sessions/{sid}/deliverables/checklist.md")
    assert resp.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd backend && pytest tests/test_deliverable_api.py -v
```

Expected: 全部失败（404 / route not found）

- [ ] **Step 3: 在 main.py 新增下载端点**

在 `backend/main.py` 合适的 API 段落（靠近其他 `GET /api/sessions/...` 端点）新增：

```python
from fastapi import HTTPException
from fastapi.responses import FileResponse

from state.manager import ALLOWED_DELIVERABLE_NAMES

@app.get("/api/sessions/{session_id}/deliverables/{filename}")
async def download_deliverable(session_id: str, filename: str):
    # 1) filename 白名单（防 traversal）
    if filename not in ALLOWED_DELIVERABLE_NAMES:
        raise HTTPException(status_code=400, detail="filename not allowed")

    # 2) session_id 格式校验（由 _session_dir 内部 _validate_session_id 保证）
    try:
        session_dir = state_mgr._session_dir(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid session_id")

    # 3) session 存在 + 未软删除（与 /api/plan/{id} 同级保护）
    meta = await session_store.get(session_id)  # 若 session_store 不同名请按现有命名对齐
    if meta is None or getattr(meta, "deleted", False):
        raise HTTPException(status_code=404, detail="session not found")

    # 4) 路径严格从 _session_dir() 派生，禁止任何拼接
    path = session_dir / "deliverables" / filename
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="deliverable not generated yet; finish Phase 7 first",
        )
    return FileResponse(
        path,
        media_type="text/markdown",
        filename=filename,
    )
```

> **Note**：`session_store` 的实际变量名请对齐 `main.py` 里已有的注入；若软删标记字段名不是 `deleted`，按现状改写。本次**不做 owner 校验**（与其他 session 类端点保持一致），owner 校验作为独立的安全重构任务。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd backend && pytest tests/test_deliverable_api.py -v
```

Expected: 3 passed

- [ ] **Step 5: 在 main.py 工具后处理里触发落盘 + SSE 事件（严格顺序）**

定位 `main.py` 处理 `tool_result` 的循环（靠近 `PLAN_WRITER_TOOL_NAMES` 检查、SSE `tool_result` 发送之后）。在 tool_result 处理中追加以下分支：

```python
# 伪定位参考（请在实际代码中找到 tool 名称分发或结果处理段）
if tool_call.name == "generate_summary" and tool_result.status == "success":
    from datetime import datetime as _dt

    data = tool_result.data or {}
    travel_md = data.get("travel_plan_markdown")
    checklist_md = data.get("checklist_markdown")

    # generate_summary 成功路径保证两份内容都返回；任一缺失视为契约破损
    if not travel_md or not checklist_md:
        raise RuntimeError(
            "generate_summary did not return both markdown payloads"
        )

    # === 严格顺序：先落盘 → 写 state → 保存 plan → 最后才 yield 事件 ===
    await state_mgr.save_deliverable(
        plan.session_id, "travel_plan.md", travel_md
    )
    await state_mgr.save_deliverable(
        plan.session_id, "checklist.md", checklist_md
    )
    ready_at = _dt.now().astimezone().isoformat()
    plan.deliverables = {
        "travel_plan_md": "travel_plan.md",
        "checklist_md": "checklist.md",
        "ready_at": ready_at,
    }
    await state_mgr.save(plan)  # 在 SSE 事件之前确保字段已持久化

    yield {
        "type": "deliverable_ready",
        "files": [
            {
                "name": "travel_plan.md",
                "url": f"/api/sessions/{plan.session_id}/deliverables/travel_plan.md",
            },
            {
                "name": "checklist.md",
                "url": f"/api/sessions/{plan.session_id}/deliverables/checklist.md",
            },
        ],
        "ready_at": ready_at,
    }
```

> **Important:**
> - `yield` 的确切语法匹配所在 SSE 生成器格式（`f"data: {json.dumps(...)}\n\n"` 等），参考 `main.py` 内其他 yield。
> - 严禁把 yield 提到 `state_mgr.save(plan)` 之前 —— 前端收到事件的瞬间必须保证 `plan.deliverables` 已落盘。
> - 任意步骤抛错时不 yield `deliverable_ready`；由上层异常处理发 SSE `error` 事件。

- [ ] **Step 6: 补端到端落盘测试**

在 `backend/tests/test_deliverable_api.py` 追加：

```python
@pytest.mark.asyncio
async def test_generate_summary_persists_both_files_to_disk(tmp_path):
    from state.manager import StateManager
    from tools.generate_summary import make_generate_summary_tool

    mgr = StateManager(data_dir=str(tmp_path))
    plan = await mgr.create_session()

    tool_fn = make_generate_summary_tool()
    result = await tool_fn(
        plan_data={
            "session_id": plan.session_id,
            "destination": "东京",
            "daily_plans": [],
        },
        checklist_markdown="## 证件签证\n- [ ] 有效护照\n",
    )
    await mgr.save_deliverable(
        plan.session_id, "travel_plan.md", result["travel_plan_markdown"]
    )
    await mgr.save_deliverable(
        plan.session_id, "checklist.md", result["checklist_markdown"]
    )

    base = Path(tmp_path) / "sessions" / plan.session_id / "deliverables"
    assert (base / "travel_plan.md").exists()
    assert (base / "checklist.md").exists()
    assert "证件签证" in (base / "checklist.md").read_text()


@pytest.mark.asyncio
async def test_backtrack_then_regenerate_overwrites_old_files(tmp_path):
    """回退后再次 Phase 7 生成：文件被新内容覆盖（而不是残留旧版本）。"""
    from state.manager import StateManager

    mgr = StateManager(data_dir=str(tmp_path))
    plan = await mgr.create_session()
    await mgr.save_deliverable(plan.session_id, "travel_plan.md", "# v1\n")
    await mgr.save_deliverable(plan.session_id, "checklist.md", "# c1\n")
    await mgr.clear_deliverables(plan.session_id)  # 模拟 backtrack
    await mgr.save_deliverable(plan.session_id, "travel_plan.md", "# v2\n")
    await mgr.save_deliverable(plan.session_id, "checklist.md", "# c2\n")

    base = Path(tmp_path) / "sessions" / plan.session_id / "deliverables"
    assert (base / "travel_plan.md").read_text() == "# v2\n"
    assert (base / "checklist.md").read_text() == "# c2\n"
```

```bash
cd backend && pytest tests/test_deliverable_api.py -v
```

Expected: 4 passed

- [ ] **Step 7: 提交**

```bash
git add backend/main.py backend/tests/test_deliverable_api.py
git commit -m "feat(api): persist Phase 7 deliverables + emit deliverable_ready SSE"
```

---

## Task 7：前端类型扩展

**Files:**
- Modify: `frontend/src/types/plan.ts`

- [ ] **Step 1: 扩展 SSE 事件联合类型**

在 `GenericSSEEvent` 联合里加入新成员：

```typescript
| {
    type: 'deliverable_ready'
    files: Array<{ name: string; url: string }>
    ready_at: string
  }
```

- [ ] **Step 2: 在 `TravelPlanState` 接口末尾追加**

```typescript
  deliverables?: {
    travel_plan_md?: string
    checklist_md?: string
    ready_at?: string
  }
```

- [ ] **Step 3: 编译验证**

```bash
cd frontend && npm run build
```

Expected: 构建通过，无 TS 报错。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/types/plan.ts
git commit -m "feat(types): add deliverable_ready event and deliverables state"
```

---

## Task 8：DeliverablesCard 组件

**Files:**
- Create: `frontend/src/components/DeliverablesCard.tsx`

- [ ] **Step 1: 实现组件**

文件：`frontend/src/components/DeliverablesCard.tsx`

```tsx
import type { FC } from 'react'

export interface DeliverableFile {
  name: string
  url: string
}

export interface DeliverablesCardProps {
  files: DeliverableFile[]
  readyAt?: string
}

const LABELS: Record<string, { title: string; hint: string }> = {
  'travel_plan.md': {
    title: '旅行计划',
    hint: '完整行程、住宿、预算和偏好，旅行期间随时查阅。',
  },
  'checklist.md': {
    title: '出发前清单',
    hint: '证件签证、天气、预订、交通接驳、应急预案的待办与完成项。',
  },
}

export const DeliverablesCard: FC<DeliverablesCardProps> = ({ files, readyAt }) => {
  if (!files.length) return null
  return (
    <section className="deliverables-card">
      <header className="deliverables-card__header">
        <h3>出发前可下载文档</h3>
        {readyAt && (
          <time dateTime={readyAt} className="deliverables-card__ts">
            生成于 {new Date(readyAt).toLocaleString()}
          </time>
        )}
      </header>
      <ul className="deliverables-card__list">
        {files.map((f) => {
          const meta = LABELS[f.name] ?? { title: f.name, hint: '' }
          return (
            <li key={f.name} className="deliverables-card__item">
              <div>
                <p className="deliverables-card__title">{meta.title}</p>
                <p className="deliverables-card__hint">{meta.hint}</p>
              </div>
              <a
                className="deliverables-card__download"
                href={f.url}
                download={f.name}
              >
                下载 {f.name}
              </a>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

export default DeliverablesCard
```

- [ ] **Step 2: 补样式（按 Solstice 设计系统）**

在 `frontend/src/styles/` 或相应全局样式文件中追加（类名见上，保持与现有卡片风格一致）：

```css
.deliverables-card {
  padding: 16px;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 191, 64, 0.2);
}
.deliverables-card__header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 12px;
}
.deliverables-card__ts {
  font-size: 12px;
  opacity: 0.7;
}
.deliverables-card__list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  list-style: none;
  padding: 0;
  margin: 0;
}
.deliverables-card__item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 12px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.2);
}
.deliverables-card__title {
  margin: 0;
  font-weight: 600;
}
.deliverables-card__hint {
  margin: 4px 0 0;
  font-size: 13px;
  opacity: 0.75;
}
.deliverables-card__download {
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(255, 191, 64, 0.15);
  color: #ffbf40;
  text-decoration: none;
}
```

- [ ] **Step 3: 提交**

```bash
git add frontend/src/components/DeliverablesCard.tsx frontend/src/styles/
git commit -m "feat(frontend): add DeliverablesCard for Phase 7 downloads"
```

---

## Task 9：ChatPanel 事件接入

**Files:**
- Modify: `frontend/src/components/ChatPanel.tsx`

- [ ] **Step 1: 订阅并存储事件**

在 `ChatPanel` 的 SSE 事件处理分支（现有 `event.type === 'state_update'` 附近，约 L422）新增：

```typescript
} else if (event.type === 'deliverable_ready') {
  setDeliverables({ files: event.files, readyAt: event.ready_at })
}
```

在组件 state 区顶部加上：

```typescript
const [deliverables, setDeliverables] = useState<{
  files: Array<{ name: string; url: string }>
  readyAt?: string
} | null>(null)
```

- [ ] **Step 2: 在聊天流末尾渲染卡片**

在 ChatPanel 渲染区合适位置（助手最后一条消息之后，输入框之前）：

```tsx
{deliverables && (
  <DeliverablesCard
    files={deliverables.files}
    readyAt={deliverables.readyAt}
  />
)}
```

顶部 import：

```tsx
import DeliverablesCard from './DeliverablesCard'
```

- [ ] **Step 3: 手动验证**

```bash
npm run dev:all
```

- 打开前端，跑一次完整会话到 Phase 7。
- 触发 `generate_summary` 后应看到 DeliverablesCard 出现，两个下载链接可点。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/components/ChatPanel.tsx
git commit -m "feat(frontend): render DeliverablesCard on deliverable_ready event"
```

---

## Task 10：App.tsx 切回历史会话时回显

**Files:**
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: 改 openSession 消费 plan.deliverables**

在 `openSession`（约 L97-103）取到 plan 后：

```typescript
if (plan.deliverables && (plan.deliverables.travel_plan_md || plan.deliverables.checklist_md)) {
  const files: Array<{ name: string; url: string }> = []
  if (plan.deliverables.travel_plan_md) {
    files.push({
      name: 'travel_plan.md',
      url: `/api/sessions/${plan.session_id}/deliverables/travel_plan.md`,
    })
  }
  if (plan.deliverables.checklist_md) {
    files.push({
      name: 'checklist.md',
      url: `/api/sessions/${plan.session_id}/deliverables/checklist.md`,
    })
  }
  setRestoredDeliverables({ files, readyAt: plan.deliverables.ready_at })
} else {
  setRestoredDeliverables(null)
}
```

在 App 顶部 state：

```typescript
const [restoredDeliverables, setRestoredDeliverables] = useState<{
  files: Array<{ name: string; url: string }>
  readyAt?: string
} | null>(null)
```

- [ ] **Step 2: 在 RightPanel Plan Tab 顶部插入卡片**

```tsx
{restoredDeliverables && (
  <DeliverablesCard
    files={restoredDeliverables.files}
    readyAt={restoredDeliverables.readyAt}
  />
)}
```

- [ ] **Step 3: 手动验证**

- 完成一次会话，刷新浏览器并重新打开同一会话 → 右栏顶部看到两份文档下载入口。
- 切到一个未完成的会话 → 无卡片。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/App.tsx
git commit -m "feat(frontend): restore deliverables card when reopening session"
```

---

## Task 11：PROJECT_OVERVIEW.md 同步更新

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 更新 Phase 7 段落**

把 "§4 Phase 7" 小节的"工具"一行改为（顺带补上一直缺失的 `search_travel_services`）：

```
- 工具：`check_weather`、`check_availability`、`search_travel_services`、`web_search`、`generate_summary`（Finalizer，拒绝不完整行程或清单）、`request_backtrack`
```

并在 "产出" 一行后追加：

```
- 交付：Phase 7 末尾一次性产出 `travel_plan.md` + `checklist.md`，落盘到 `backend/data/sessions/sess_*/deliverables/`；回退到 Phase 5 或更早阶段会自动清理这两份文件
```

- [ ] **Step 2: 更新 §9 SSE 事件表**

新增一行：

```
| `deliverable_ready` | Phase 7 末尾通知前端两份文档已生成，含下载 URL |
```

- [ ] **Step 3: 更新 §11 API 端点表**

新增：

```
GET    /api/sessions/{id}/deliverables/{filename}   下载交付文档（白名单）
```

- [ ] **Step 4: 更新 §10 目录结构**

在 `backend/data/sessions/sess_*/` 节点下新增：

```
├── deliverables/       # Phase 7 交付物：travel_plan.md / checklist.md
```

- [ ] **Step 5: 提交**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: document Phase 7 double deliverables in PROJECT_OVERVIEW"
```

---

## 验证清单（全量）

执行完全部 Tasks 后：

1. **后端单测**：
   ```bash
   cd backend && pytest tests/test_markdown_renderer.py tests/test_state_manager.py tests/test_generate_summary.py tests/test_phase7_prompt.py tests/test_deliverable_api.py -v
   ```
   Expected: 所有用例通过。

2. **后端回归**：
   ```bash
   cd backend && pytest
   ```
   Expected: 不引入新失败。

3. **前端构建**：
   ```bash
   cd frontend && npm run build
   ```
   Expected: 无 TS 错误。

4. **端到端人工验证**：
   - `npm run dev:all` 启动 → 走完一次 Phase 1→3→5→7。
   - Phase 7 扫描后 LLM 调用 `generate_summary`，DeliverablesCard 出现，两个链接均可下载。
   - 检查 `backend/data/sessions/sess_*/deliverables/`，两个 md 内容与屏幕一致。
   - 关闭会话再打开同一会话 → 卡片仍然显示。
   - 打开一个未到 Phase 7 的会话 → 无卡片。
   - 调用 `GET /api/sessions/<未完成 sid>/deliverables/checklist.md` → 404。
   - 调用 `GET /api/sessions/<sid>/deliverables/plan.json` → 400。

5. **回退语义人工验证**：
   - Phase 7 完成后触发 `request_backtrack(to_phase=5, reason=...)`：
     - 观察磁盘 `backend/data/sessions/sess_*/deliverables/` → 两份 md **已被物理删除**。
     - `GET /api/sessions/<sid>/deliverables/travel_plan.md` → 404。
     - 前端已打开的卡片应在收到新的 `state_update`（`plan.deliverables` 为 null）后自动消失。
   - 回到 Phase 7 重新走完整流程再次调用 `generate_summary` → 文件以新内容写入，`plan.deliverables.ready_at` 为新时间戳。

6. **契约拒绝路径人工验证**：
   - 人为让 LLM 只填 3/5 天的 daily_plans 就调用 `generate_summary` → 应返回 `INCOMPLETE_ITINERARY`，不产生任何 md 文件。
   - 传入只含"购物"二级标题的 checklist_markdown → 应返回 `INVALID_ARGUMENTS`，suggestion 含"证件签证"。

---

## 非目标（本次不做）

- 不扩展 `Activity` / `Accommodation` / `Transport` 字段收集预订号、票号、紧急联系方式。
- 不修改 Phase 7 的扫描维度与工具清单（仅追加 `checklist_markdown` 使用说明）。
- 不做 PDF 导出。
- 不保留历史版本的 deliverables，backtrack 重做会直接覆盖。
- 不写 Playwright E2E（作为 follow-up 任务）。
