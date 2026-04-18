# Phase 7 双文档交付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Phase 7 结束时一次性交付并冻结两份可下载 Markdown 文件：`travel_plan.md` 与 `checklist.md`；只有回退后重新跑 Phase 7 才允许更新。

**Architecture:** 保持 `LLM 双文档直出` 路线：Phase 7 prompt 约束模型提交 `travel_plan_markdown` 与 `checklist_markdown` 给 `generate_summary`；工具做冻结和基础结构校验，`main.py` 负责落盘、更新 `plan.deliverables`、走现有 `state_update` SSE；前端只根据 `plan.deliverables` 展示下载链接，不新增预览和新事件类型。

**Tech Stack:** Python 3.12 / FastAPI / dataclasses / pytest / React 19 / TypeScript / Vite

**Spec:** `docs/superpowers/specs/2026-04-18-phase7-dual-deliverables-design.md`

**Existing tests:** `backend/tests/test_state_models.py`, `backend/tests/test_state_manager.py`, `backend/tests/test_generate_summary.py`, `backend/tests/test_prompt_architecture.py`, `backend/tests/test_api.py`, `backend/tests/test_backtrack_service.py`, `backend/tests/test_phase_integration.py`

---

## File Map

### Backend

- **Modify:** `backend/state/models.py` — 新增 `deliverables` 字段；纳入 `to_dict` / `from_dict` / `clear_downstream`
- **Modify:** `backend/state/manager.py` — 新增 deliverable 文件白名单、读写、清理方法
- **Modify:** `backend/tools/generate_summary.py` — 改为 `make_generate_summary_tool(plan)`，接收双 Markdown 参数，执行冻结和结构校验
- **Modify:** `backend/phase/prompts.py` — Phase 7 prompt 改为“双文档提交”契约
- **Modify:** `backend/main.py` — 注册 plan-aware `generate_summary`；成功后落盘交付物；新增下载端点
- **Modify:** `backend/phase/backtrack.py` — 依赖 `clear_downstream()` 清除 `deliverables`

### Frontend

- **Modify:** `frontend/src/types/plan.ts` — 为 `TravelPlanState` 增加 `deliverables` 类型
- **Create:** `frontend/src/components/DeliverablesCard.tsx` — 下载卡片
- **Modify:** `frontend/src/App.tsx` — 在右侧 plan 面板展示下载卡片
- **Modify:** `frontend/src/styles/index.css` — 下载卡片样式

### Tests

- **Modify:** `backend/tests/test_state_models.py`
- **Modify:** `backend/tests/test_state_manager.py`
- **Modify:** `backend/tests/test_generate_summary.py`
- **Modify:** `backend/tests/test_prompt_architecture.py`
- **Modify:** `backend/tests/test_api.py`
- **Modify:** `backend/tests/test_backtrack_service.py`
- **Modify:** `backend/tests/test_phase_integration.py`
- **Modify:** `backend/tests/test_tool_human_label.py`

### Docs

- **Modify:** `PROJECT_OVERVIEW.md` — 更新 Phase 7、下载 API、交付物冻结语义

---

## Task 1: Add Deliverables State + File Storage Primitives

**Files:**
- Modify: `backend/state/models.py`
- Modify: `backend/state/manager.py`
- Test: `backend/tests/test_state_models.py`
- Test: `backend/tests/test_state_manager.py`

- [ ] **Step 1: Write failing state-model and state-manager tests**

在 `backend/tests/test_state_models.py` 追加：

```python
def test_plan_serialization_roundtrips_deliverables():
    plan = TravelPlanState(
        session_id="sess_001",
        deliverables={
            "travel_plan_md": "travel_plan.md",
            "checklist_md": "checklist.md",
            "generated_at": "2026-04-18T22:30:00+08:00",
        },
    )

    data = plan.to_dict()
    assert data["deliverables"]["travel_plan_md"] == "travel_plan.md"

    restored = TravelPlanState.from_dict(data)
    assert restored.deliverables == plan.deliverables


def test_clear_downstream_from_phase_5_clears_deliverables():
    plan = TravelPlanState(
        session_id="sess_001",
        phase=7,
        deliverables={
            "travel_plan_md": "travel_plan.md",
            "checklist_md": "checklist.md",
            "generated_at": "2026-04-18T22:30:00+08:00",
        },
        daily_plans=[DayPlan(day=1, date="2026-04-10", activities=[])],
    )

    plan.clear_downstream(from_phase=5)
    assert plan.daily_plans == []
    assert plan.deliverables is None
```

在 `backend/tests/test_state_manager.py` 追加：

```python
@pytest.mark.asyncio
async def test_save_and_read_deliverable(manager):
    plan = await manager.create_session()
    path = await manager.save_deliverable(
        plan.session_id,
        "travel_plan.md",
        "# 东京 5 日旅行计划\n",
    )

    assert Path(path).exists()
    assert await manager.read_deliverable(plan.session_id, "travel_plan.md") == (
        "# 东京 5 日旅行计划\n"
    )


@pytest.mark.asyncio
async def test_save_deliverable_rejects_non_whitelisted_name(manager):
    plan = await manager.create_session()

    with pytest.raises(ValueError):
        await manager.save_deliverable(plan.session_id, "../etc/passwd", "x")

    with pytest.raises(ValueError):
        await manager.read_deliverable(plan.session_id, "notes.txt")


@pytest.mark.asyncio
async def test_clear_deliverables_is_idempotent(manager):
    plan = await manager.create_session()
    await manager.save_deliverable(plan.session_id, "travel_plan.md", "# plan\n")
    await manager.save_deliverable(plan.session_id, "checklist.md", "# list\n")

    await manager.clear_deliverables(plan.session_id)
    await manager.clear_deliverables(plan.session_id)

    deliverables_dir = Path(manager._session_dir(plan.session_id)) / "deliverables"
    assert not (deliverables_dir / "travel_plan.md").exists()
    assert not (deliverables_dir / "checklist.md").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd backend && pytest tests/test_state_models.py tests/test_state_manager.py -k "deliverable" -v
```

Expected:

- `TypeError` or missing `deliverables` key in `TravelPlanState`
- `AttributeError: 'StateManager' object has no attribute 'save_deliverable'`

- [ ] **Step 3: Add `deliverables` to `TravelPlanState` and downstream reset**

在 `backend/state/models.py` 做如下修改：

```python
_PHASE_DOWNSTREAM: dict[int, list[str]] = {
    1: [
        "destination",
        "dates",
        "phase3_step",
        "trip_brief",
        "candidate_pool",
        "shortlist",
        "skeleton_plans",
        "selected_skeleton_id",
        "transport_options",
        "selected_transport",
        "accommodation_options",
        "accommodation",
        "risks",
        "alternatives",
        "daily_plans",
        "deliverables",
    ],
    3: [
        "dates",
        "phase3_step",
        "trip_brief",
        "candidate_pool",
        "shortlist",
        "skeleton_plans",
        "selected_skeleton_id",
        "transport_options",
        "selected_transport",
        "accommodation_options",
        "accommodation",
        "risks",
        "alternatives",
        "daily_plans",
        "deliverables",
    ],
    5: ["daily_plans", "deliverables"],
}

_FIELD_DEFAULTS: dict[str, Any] = {
    "destination": None,
    "dates": None,
    "phase3_step": "brief",
    "trip_brief": {},
    "candidate_pool": [],
    "shortlist": [],
    "skeleton_plans": [],
    "selected_skeleton_id": None,
    "transport_options": [],
    "selected_transport": None,
    "accommodation_options": [],
    "accommodation": None,
    "risks": [],
    "alternatives": [],
    "daily_plans": [],
    "deliverables": None,
}
```

在 `TravelPlanState` 字段区插入：

```python
    deliverables: dict[str, str] | None = None
```

在 `to_dict()` 中插入：

```python
            "deliverables": self.deliverables,
```

在 `from_dict()` 的 `return cls(` 构造参数中插入：

```python
            deliverables=d.get("deliverables"),
```

- [ ] **Step 4: Add deliverable file helpers to `StateManager`**

在 `backend/state/manager.py` 顶部增加白名单：

```python
ALLOWED_DELIVERABLE_NAMES = {"travel_plan.md", "checklist.md"}
```

在 `StateManager` 类中追加：

```python
    async def save_deliverable(
        self,
        session_id: str,
        filename: str,
        content: str,
    ) -> str:
        if filename not in ALLOWED_DELIVERABLE_NAMES:
            raise ValueError(f"deliverable filename not allowed: {filename!r}")

        deliverables_dir = self._session_dir(session_id) / "deliverables"
        deliverables_dir.mkdir(parents=True, exist_ok=True)
        path = deliverables_dir / filename
        path.write_text(content, encoding="utf-8")
        return str(path)

    async def read_deliverable(self, session_id: str, filename: str) -> str:
        if filename not in ALLOWED_DELIVERABLE_NAMES:
            raise ValueError(f"deliverable filename not allowed: {filename!r}")

        path = self._session_dir(session_id) / "deliverables" / filename
        if not path.exists():
            raise FileNotFoundError(f"deliverable not found: {filename}")
        return path.read_text(encoding="utf-8")

    async def clear_deliverables(self, session_id: str) -> None:
        deliverables_dir = self._session_dir(session_id) / "deliverables"
        if not deliverables_dir.exists():
            return

        for name in ALLOWED_DELIVERABLE_NAMES:
            target = deliverables_dir / name
            if target.exists():
                target.unlink()
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
cd backend && pytest tests/test_state_models.py tests/test_state_manager.py -k "deliverable" -v
```

Expected:

- all selected tests pass

- [ ] **Step 6: Commit**

```bash
git add backend/state/models.py backend/state/manager.py backend/tests/test_state_models.py backend/tests/test_state_manager.py
git commit -m "feat(state): add frozen deliverables state and file storage"
```

---

## Task 2: Make `generate_summary` Submit Two Markdown Deliverables

**Files:**
- Modify: `backend/tools/generate_summary.py`
- Modify: `backend/phase/prompts.py`
- Modify: `backend/tests/test_generate_summary.py`
- Modify: `backend/tests/test_prompt_architecture.py`
- Modify: `backend/tests/test_tool_human_label.py`

- [ ] **Step 1: Write failing tool and prompt tests**

把 `backend/tests/test_generate_summary.py` 改成 plan-aware fixture，并追加冻结/结构校验用例：

```python
import pytest

from state.models import TravelPlanState
from tools.base import ToolError
from tools.generate_summary import make_generate_summary_tool


@pytest.fixture
def plan():
    return TravelPlanState(session_id="sess_123456789abc", phase=7)


@pytest.fixture
def tool_fn(plan):
    return make_generate_summary_tool(plan)


@pytest.mark.asyncio
async def test_generate_summary_returns_dual_markdown(tool_fn):
    result = await tool_fn(
        plan_data={"destination": "东京"},
        travel_plan_markdown="# 东京 5 日旅行计划\n\n## 第 1 天\n- 浅草寺\n",
        checklist_markdown="# 东京出发前清单\n\n- [ ] 护照\n",
    )

    assert "travel_plan_markdown" in result
    assert "checklist_markdown" in result
    assert result["summary"].startswith("已生成并冻结")


@pytest.mark.asyncio
async def test_generate_summary_rejects_frozen_deliverables(plan):
    plan.deliverables = {
        "travel_plan_md": "travel_plan.md",
        "checklist_md": "checklist.md",
        "generated_at": "2026-04-18T22:30:00+08:00",
    }
    tool_fn = make_generate_summary_tool(plan)

    with pytest.raises(ToolError, match="已冻结"):
        await tool_fn(
            plan_data={"destination": "东京"},
            travel_plan_markdown="# 东京\n\n## 第 1 天\n- 浅草寺\n",
            checklist_markdown="# 清单\n\n- [ ] 护照\n",
        )


@pytest.mark.asyncio
async def test_generate_summary_rejects_invalid_markdown_structure(tool_fn):
    with pytest.raises(ToolError, match="travel_plan_markdown"):
        await tool_fn(
            plan_data={"destination": "东京"},
            travel_plan_markdown="东京自由行",
            checklist_markdown="# 清单\n\n- [ ] 护照\n",
        )
```

在 `backend/tests/test_prompt_architecture.py` 的 Phase 7 断言区追加：

```python
    def test_phase7_mentions_travel_plan_markdown(self):
        assert "travel_plan_markdown" in PHASE7_PROMPT

    def test_phase7_mentions_checklist_markdown(self):
        assert "checklist_markdown" in PHASE7_PROMPT

    def test_phase7_mentions_frozen_deliverables(self):
        assert "冻结" in PHASE7_PROMPT or "先回退" in PHASE7_PROMPT
```

在 `backend/tests/test_tool_human_label.py` 把工厂调用改成：

```python
    engine.register(make_generate_summary_tool(plan))
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd backend && pytest tests/test_generate_summary.py tests/test_prompt_architecture.py tests/test_tool_human_label.py -v
```

Expected:

- `TypeError: make_generate_summary_tool() takes 0 positional arguments but 1 was given`
- Phase 7 prompt assertions for `travel_plan_markdown` / `checklist_markdown` fail

- [ ] **Step 3: Rework `generate_summary` into a plan-aware dual-markdown tool**

把 `backend/tools/generate_summary.py` 改成：

```python
from __future__ import annotations

import re

from state.models import TravelPlanState
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "plan_data": {
            "type": "object",
            "description": "当前旅行计划状态快照",
        },
        "travel_plan_markdown": {
            "type": "string",
            "description": "完整旅行计划 Markdown 文本",
        },
        "checklist_markdown": {
            "type": "string",
            "description": "出发前清单 Markdown 文本",
        },
    },
    "required": ["plan_data", "travel_plan_markdown", "checklist_markdown"],
}


def _has_h1(text: str) -> bool:
    return bool(re.search(r"(?m)^#\\s+\\S", text))


def _has_day_section(text: str) -> bool:
    return "## 第" in text or "### 第" in text


def _has_list_items(text: str) -> bool:
    return bool(re.search(r"(?m)^- (\\[ \\] )?\\S", text))


def make_generate_summary_tool(plan: TravelPlanState):
    @tool(
        name="generate_summary",
        description=\"\"\"提交最终交付物。
Use when: Phase 7 结束时提交 travel_plan_markdown 与 checklist_markdown。
Don't use when: 交付物已冻结，或文档还没整理完整。\"\"\",
        phases=[7],
        parameters=_PARAMETERS,
        side_effect="write",
        human_label="生成并冻结交付文档",
    )
    async def generate_trip_summary(
        plan_data: dict,
        travel_plan_markdown: str,
        checklist_markdown: str,
    ) -> dict:
        if plan.deliverables:
            raise ToolError(
                "当前交付物已冻结；如需更新请先回退到更早阶段再重新完成 Phase 7",
                error_code="DELIVERABLES_FROZEN",
                suggestion="先执行回退，再重新完成 Phase 7",
            )

        if not isinstance(plan_data, dict):
            plan_data = {}

        travel_md = (travel_plan_markdown or "").strip()
        checklist_md = (checklist_markdown or "").strip()

        if not travel_md or not _has_h1(travel_md) or not _has_day_section(travel_md):
            raise ToolError(
                "travel_plan_markdown 缺少一级标题或逐日 section",
                error_code="INVALID_ARGUMENTS",
                suggestion="补充 # 标题，并至少包含一个“第 X 天”小节",
            )

        if not checklist_md or not _has_h1(checklist_md) or not _has_list_items(checklist_md):
            raise ToolError(
                "checklist_markdown 缺少一级标题或清单项",
                error_code="INVALID_ARGUMENTS",
                suggestion="补充 # 标题，并至少包含一个列表项或 checklist 项",
            )

        destination = plan_data.get("destination") or plan.destination or "当前行程"
        return {
            "summary": f"已生成并冻结 {destination} 的 travel_plan.md 与 checklist.md",
            "travel_plan_markdown": travel_md + "\\n",
            "checklist_markdown": checklist_md + "\\n",
        }

    return generate_trip_summary
```

- [ ] **Step 4: Update `PHASE7_PROMPT` to require dual markdown submission**

在 `backend/phase/prompts.py` 的 `PHASE7_PROMPT` 中替换“步骤 3”和工具契约相关段落为：

```python
### 步骤 3 — 提交正式交付物
最终必须调用 generate_summary，一次性提交：
- `travel_plan_markdown`：完整旅行计划 Markdown
- `checklist_markdown`：出发前清单 Markdown

要求：
- `travel_plan_markdown` 必须基于当前已确认的 destination / dates / daily_plans / accommodation / selected_transport
- `checklist_markdown` 必须基于本轮实际查询到的天气、服务和注意事项
- 不要编造票号、订单号、未确认价格或未验证链接
- 如果系统已存在冻结交付物，不要再次调用 generate_summary；应明确告知用户需要先回退
```

并在完成 Gate 中把“生成结构化摘要”改成：

```python
- 已调用 generate_summary 提交 `travel_plan_markdown` 与 `checklist_markdown`
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
cd backend && pytest tests/test_generate_summary.py tests/test_prompt_architecture.py tests/test_tool_human_label.py -v
```

Expected:

- all tests pass

- [ ] **Step 6: Commit**

```bash
git add backend/tools/generate_summary.py backend/phase/prompts.py backend/tests/test_generate_summary.py backend/tests/test_prompt_architecture.py backend/tests/test_tool_human_label.py
git commit -m "feat(phase7): require dual markdown deliverables in generate_summary"
```

---

## Task 3: Persist Frozen Deliverables in `main.py` and Expose Download API

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/tests/test_api.py`
- Modify: `backend/tests/test_phase_integration.py`

- [ ] **Step 1: Write failing API and integration tests**

在 `backend/tests/test_api.py` 顶部 helper 区追加：

```python
def _get_state_manager(app):
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__closure__"):
            continue
        free_vars = getattr(endpoint.__code__, "co_freevars", ())
        for name, cell in zip(free_vars, endpoint.__closure__ or ()):
            if name == "state_mgr":
                return cell.cell_contents
    raise RuntimeError("Cannot locate state_mgr")
```

在同文件追加：

```python
@pytest.mark.asyncio
async def test_download_deliverable_success(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")
        session_id = resp.json()["session_id"]

    sessions = _get_sessions(app)
    state_mgr = _get_state_manager(app)
    plan = sessions[session_id]["plan"]
    plan.deliverables = {
        "travel_plan_md": "travel_plan.md",
        "checklist_md": "checklist.md",
        "generated_at": "2026-04-18T22:30:00+08:00",
    }
    await state_mgr.save_deliverable(session_id, "travel_plan.md", "# 东京计划\\n")
    await state_mgr.save(plan)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{session_id}/deliverables/travel_plan.md")

    assert resp.status_code == 200
    assert resp.text == "# 东京计划\\n"
    assert "attachment" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_deliverable_rejects_unknown_filename(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")
        session_id = resp.json()["session_id"]
        bad = await client.get(f"/api/sessions/{session_id}/deliverables/random.txt")

    assert bad.status_code == 404
```

把 `backend/tests/test_phase_integration.py::test_phase7_summary_generation` 中 `generate_summary` 的参数替换为：

```python
        tc_summary = ToolCall(
            id="tc_gs_1",
            name="generate_summary",
            arguments={
                "plan_data": {
                    "destination": "京都",
                    "total_days": 5,
                },
                "travel_plan_markdown": "# 京都 5 日旅行计划\\n\\n## 第 1 天\\n- 景点1\\n",
                "checklist_markdown": "# 京都出发前清单\\n\\n- [ ] 护照\\n",
            },
        )
```

并在断言区追加：

```python
    assert plan_data["deliverables"]["travel_plan_md"] == "travel_plan.md"
    assert plan_data["deliverables"]["checklist_md"] == "checklist.md"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd backend && pytest tests/test_api.py tests/test_phase_integration.py -k "deliverable or phase7_summary_generation" -v
```

Expected:

- 404 on missing download endpoint
- Phase 7 integration fails because `generate_summary` is not yet registered with `plan`

- [ ] **Step 3: Register plan-aware `generate_summary` and persist deliverables on success**

把 `backend/main.py` 里的注册改成：

```python
        tool_engine.register(make_generate_summary_tool(plan))
```

在 `backend/main.py` 内新增 helper：

```python
    async def _persist_phase7_deliverables(
        plan: TravelPlanState,
        result_data: dict,
    ) -> None:
        if plan.deliverables:
            raise RuntimeError("deliverables already frozen")

        travel_md = str(result_data["travel_plan_markdown"])
        checklist_md = str(result_data["checklist_markdown"])

        await state_mgr.save_deliverable(plan.session_id, "travel_plan.md", travel_md)
        await state_mgr.save_deliverable(plan.session_id, "checklist.md", checklist_md)

        plan.deliverables = {
            "travel_plan_md": "travel_plan.md",
            "checklist_md": "checklist.md",
            "generated_at": _now_iso(),
        }
```

在 `_run_agent_stream()` 的 tool-result success 分支中，用 `tool_name` 单独处理 `generate_summary`：

```python
                    tool_name = tool_call_names.get(chunk.tool_result.tool_call_id)
                    if (
                        chunk.tool_result
                        and chunk.tool_result.status == "success"
                        and (
                            tool_name in PLAN_WRITER_TOOL_NAMES
                            or tool_name == "generate_summary"
                        )
                    ):
                        result_data = (
                            chunk.tool_result.data
                            if isinstance(chunk.tool_result.data, dict)
                            else {}
                        )

                        if tool_name == "generate_summary":
                            await _persist_phase7_deliverables(plan, result_data)
                        else:
                            updated_fields = _plan_writer_updated_fields(result_data)
                            if result_data.get("backtracked"):
                                await state_mgr.clear_deliverables(plan.session_id)
                                await _rotate_trip_on_reset_backtrack(
                                    user_id=session["user_id"],
                                    plan=plan,
                                    to_phase=int(result_data.get("to_phase", plan.phase)),
                                    reason_text=str(result_data.get("reason", "")),
                                )
                            elif "selected_skeleton_id" in updated_fields:
                                _schedule_memory_event(
                                    user_id=session["user_id"],
                                    session_id=plan.session_id,
                                    event_type="accept",
                                    object_type="skeleton",
                                    object_payload=chunk.tool_result.data or {},
                                )
                            elif "selected_transport" in updated_fields:
                                _schedule_memory_event(
                                    user_id=session["user_id"],
                                    session_id=plan.session_id,
                                    event_type="accept",
                                    object_type="transport",
                                    object_payload=chunk.tool_result.data or {},
                                )
                            elif "accommodation" in updated_fields:
                                _schedule_memory_event(
                                    user_id=session["user_id"],
                                    session_id=plan.session_id,
                                    event_type="accept",
                                    object_type="hotel",
                                    object_payload=chunk.tool_result.data or {},
                                )
```

保留后面的：

```python
                        await state_mgr.save(plan)
                        await session_store.update(
                            plan.session_id,
                            phase=plan.phase,
                            title=_generate_title(plan),
                        )
                        yield json.dumps(
                            {"type": "state_update", "plan": plan.to_dict()},
                            ensure_ascii=False,
                        )
```

- [ ] **Step 4: Add the download endpoint**

在 `backend/main.py` 的 API 区新增：

```python
    @app.get("/api/sessions/{session_id}/deliverables/{filename}")
    async def download_deliverable(session_id: str, filename: str):
        await _ensure_storage_ready()
        meta = await session_store.load(session_id)
        if meta is None or meta["status"] == "deleted":
            raise HTTPException(status_code=404, detail="Session not found")

        try:
            content = await state_mgr.read_deliverable(session_id, filename)
        except ValueError:
            raise HTTPException(status_code=404, detail="Deliverable not found")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Deliverable not found")

        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
cd backend && pytest tests/test_api.py tests/test_phase_integration.py -k "deliverable or phase7_summary_generation" -v
```

Expected:

- download endpoint tests pass
- Phase 7 integration test passes and plan now carries `deliverables`

- [ ] **Step 6: Commit**

```bash
git add backend/main.py backend/tests/test_api.py backend/tests/test_phase_integration.py
git commit -m "feat(api): freeze phase7 deliverables and expose markdown downloads"
```

---

## Task 4: Clear Deliverables on Backtrack

**Files:**
- Modify: `backend/tests/test_backtrack_service.py`
- Modify: `backend/tests/test_api.py`
- Modify: `backend/main.py`

- [ ] **Step 1: Write failing backtrack cleanup tests**

在 `backend/tests/test_backtrack_service.py::_make_plan()` 中加入 `deliverables`：

```python
        deliverables={
            "travel_plan_md": "travel_plan.md",
            "checklist_md": "checklist.md",
            "generated_at": "2026-04-18T22:30:00+08:00",
        },
```

并在 `test_normal_backtrack_phase_5_to_3()` 断言区追加：

```python
        assert plan.deliverables is None
```

在 `backend/tests/test_api.py` 追加：

```python
@pytest.mark.asyncio
async def test_backtrack_endpoint_clears_deliverables_and_files(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")
        session_id = resp.json()["session_id"]

    sessions = _get_sessions(app)
    state_mgr = _get_state_manager(app)
    plan = sessions[session_id]["plan"]
    plan.phase = 7
    plan.deliverables = {
        "travel_plan_md": "travel_plan.md",
        "checklist_md": "checklist.md",
        "generated_at": "2026-04-18T22:30:00+08:00",
    }
    await state_mgr.save_deliverable(session_id, "travel_plan.md", "# plan\\n")
    await state_mgr.save_deliverable(session_id, "checklist.md", "# list\\n")
    await state_mgr.save(plan)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        backtrack = await client.post(
            f"/api/backtrack/{session_id}",
            json={"to_phase": 5, "reason": "重新生成交付物"},
        )
        missing = await client.get(
            f"/api/sessions/{session_id}/deliverables/travel_plan.md"
        )

    assert backtrack.status_code == 200
    assert backtrack.json()["plan"]["deliverables"] is None
    assert missing.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd backend && pytest tests/test_backtrack_service.py tests/test_api.py -k "deliverable and backtrack" -v
```

Expected:

- `plan.deliverables` still present after service backtrack
- files still downloadable after `/api/backtrack`

- [ ] **Step 3: Make backtrack clear in-memory `deliverables`**

这个行为主要由 Task 1 的 `_PHASE_DOWNSTREAM` 覆盖，但这里要确认 `BacktrackService` 不做额外 override；保持：

```python
        plan.clear_downstream(from_phase=to_phase)
        plan.phase = to_phase
```

如果实现过程中 `deliverables` 仍未清空，修正 `clear_downstream()` 或 `_FIELD_DEFAULTS`，不要在 `BacktrackService` 里做重复赋值。

- [ ] **Step 4: Clear deliverable files in `/api/backtrack` and fallback backtrack path**

在 `backend/main.py` 的 `/api/backtrack/{session_id}` 里，`phase_router.prepare_backtrack(plan, req.to_phase, req.reason or "用户主动回退", snapshot_path)` 后立刻加：

```python
        await state_mgr.clear_deliverables(session_id)
```

在 `_run_agent_stream()` 里 `result_data.get("backtracked")` 分支也保留：

```python
                                await state_mgr.clear_deliverables(plan.session_id)
```

在 fallback backtrack 路径里，`phase_router.prepare_backtrack(plan, backtrack_target, reason, snapshot_path)` 后插入：

```python
                    await state_mgr.clear_deliverables(plan.session_id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
cd backend && pytest tests/test_backtrack_service.py tests/test_api.py -k "deliverable and backtrack" -v
```

Expected:

- backtrack service clears `plan.deliverables`
- backtrack endpoint removes files and download now returns 404

- [ ] **Step 6: Commit**

```bash
git add backend/main.py backend/tests/test_backtrack_service.py backend/tests/test_api.py
git commit -m "fix(backtrack): clear frozen deliverables on phase reset"
```

---

## Task 5: Show Download Links in the Right Panel

**Files:**
- Modify: `frontend/src/types/plan.ts`
- Create: `frontend/src/components/DeliverablesCard.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles/index.css`

- [ ] **Step 1: Add the typed deliverables shape**

在 `frontend/src/types/plan.ts` 插入：

```ts
export interface Deliverables {
  travel_plan_md: string
  checklist_md: string
  generated_at: string
}
```

并在 `TravelPlanState` 中插入：

```ts
  deliverables?: Deliverables | null
```

- [ ] **Step 2: Create the download card component**

新建 `frontend/src/components/DeliverablesCard.tsx`：

```tsx
import type { Deliverables } from '../types/plan'

type DeliverablesCardProps = {
  sessionId: string
  deliverables: Deliverables
}

const LINKS = [
  {
    key: 'travel_plan_md',
    label: '旅行计划',
    description: '完整行程 Markdown',
  },
  {
    key: 'checklist_md',
    label: '出发清单',
    description: '出发前准备 Markdown',
  },
] as const

export default function DeliverablesCard({ sessionId, deliverables }: DeliverablesCardProps) {
  return (
    <div className="deliverables-card">
      <div className="deliverables-title">交付文档</div>
      <div className="deliverables-meta">
        生成于 {new Date(deliverables.generated_at).toLocaleString('zh-CN')}
      </div>
      <div className="deliverables-list">
        {LINKS.map((item) => {
          const filename = deliverables[item.key]
          const href = `/api/sessions/${sessionId}/deliverables/${encodeURIComponent(filename)}`
          return (
            <a
              key={item.key}
              className="deliverable-link"
              href={href}
              download={filename}
              target="_blank"
              rel="noreferrer"
            >
              <span>{item.label}</span>
              <small>{item.description}</small>
            </a>
          )
        })}
      </div>
    </div>
  )
}
```

- [ ] **Step 3: Render the card in `App.tsx`**

在 `frontend/src/App.tsx` 顶部增加：

```tsx
import DeliverablesCard from './components/DeliverablesCard'
```

在右侧 `plan` tab 的 `destination-banner` 之后插入：

```tsx
              {plan?.deliverables && (
                <div className="sidebar-section">
                  <DeliverablesCard
                    sessionId={sessionId}
                    deliverables={plan.deliverables}
                  />
                </div>
              )}
```

- [ ] **Step 4: Add minimal styling**

在 `frontend/src/styles/index.css` 追加：

```css
.deliverables-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.deliverables-title {
  font-size: 0.92rem;
  font-family: var(--font-display);
  color: var(--text-primary);
}

.deliverables-meta {
  font-size: 0.76rem;
  color: var(--text-muted);
}

.deliverables-list {
  display: grid;
  gap: 10px;
}

.deliverable-link {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-subtle);
  background: var(--bg-elevated);
  color: var(--text-primary);
  text-decoration: none;
  transition: all var(--transition-smooth);
}

.deliverable-link:hover {
  border-color: var(--border-accent);
  transform: translateY(-1px);
}

.deliverable-link small {
  color: var(--text-muted);
}
```

- [ ] **Step 5: Verify the frontend build**

Run:

```bash
cd frontend && npm run build
```

Expected:

- TypeScript and Vite build succeed

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types/plan.ts frontend/src/components/DeliverablesCard.tsx frontend/src/App.tsx frontend/src/styles/index.css
git commit -m "feat(frontend): show frozen phase7 deliverable download links"
```

---

## Task 6: Update Project Overview and Run Final Verification

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Update `PROJECT_OVERVIEW.md` to reflect the new Phase 7 contract**

在 `PROJECT_OVERVIEW.md` 中更新以下几处：

```md
### Phase 7 — 出发前查漏（skill-card）
- 角色：出发前查漏官；扫描全计划，生成带优先级的检查清单，并在结束时冻结两份 Markdown 交付物
- 工具：`check_weather`、`check_availability`、`web_search`、`search_travel_services`、`request_backtrack`
- 完成 Gate：所有高优先级项解决后，调用 `generate_summary` 提交 `travel_plan_markdown` 与 `checklist_markdown`
```

在目录或 API 描述处追加：

```md
- `backend/data/sessions/<session_id>/deliverables/`：正式交付物目录（`travel_plan.md`、`checklist.md`）
- `GET /api/sessions/{session_id}/deliverables/{filename}`：下载冻结的 Markdown 交付物
```

在状态模型描述处追加：

```md
- `TravelPlanState.deliverables`：当前正式交付物元数据；非空表示已冻结
```

- [ ] **Step 2: Run the focused backend regression suite**

Run:

```bash
cd backend && pytest \
  tests/test_state_models.py \
  tests/test_state_manager.py \
  tests/test_generate_summary.py \
  tests/test_prompt_architecture.py \
  tests/test_backtrack_service.py \
  tests/test_api.py \
  tests/test_phase_integration.py \
  tests/test_tool_human_label.py -v
```

Expected:

- all listed tests pass

- [ ] **Step 3: Re-run the frontend build**

Run:

```bash
cd frontend && npm run build
```

Expected:

- successful production build

- [ ] **Step 4: Manual smoke-check the user-visible path**

Run the app locally, then verify:

1. Phase 7 成功后右侧面板出现两个下载链接
2. 点击可下载 `travel_plan.md` 与 `checklist.md`
3. 刷新页面后链接仍存在
4. 回退到 Phase 5 后链接消失
5. 再次完成 Phase 7 后可重新生成

- [ ] **Step 5: Commit**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: document frozen phase7 markdown deliverables"
```

---

## Self-Review

### Spec coverage

- 双 Markdown 交付：Task 2 + Task 3
- 冻结语义：Task 2 + Task 3
- 回退解冻：Task 4
- 下载-only 前端：Task 5
- 文档同步：Task 6

### Placeholder scan

- 无 `TBD` / `TODO` / “之后再补”
- 所有新增行为都给了对应文件和测试落点

### Type consistency

- 后端统一使用 `deliverables = {travel_plan_md, checklist_md, generated_at}`
- 前端 `Deliverables` 类型与后端字段名保持一致
- 文件名白名单固定为 `travel_plan.md` / `checklist.md`

---

Plan complete and saved to `docs/superpowers/plans/2026-04-18-phase7-dual-deliverables.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
