# Agent Loop Decomposition V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose `backend/agent/loop.py` into focused execution and Phase 5 modules while preserving the public `agent.loop.AgentLoop` compatibility surface.

**Architecture:** Keep `AgentLoop` as the public facade and outer coordinator. Move pure helper domains into `backend/agent/execution/`, move Phase 5 orchestrator-workers into `backend/agent/phase5/`, then reduce `AgentLoop.run()` by extracting tool-batch execution and phase-transition handling in controlled stages. Compatibility wrappers stay in `AgentLoop` until tests prove call sites no longer depend on private methods.

**Tech Stack:** Python 3.12, async generators, dataclasses, pytest, existing `LLMChunk` / `ToolCall` / `ToolResult` models, existing `ToolEngine`, `HookManager`, `PhaseRouter`, and `ContextManager`.

---

## Current Problem

`backend/agent/loop.py` is currently about 1181 lines and mixes these responsibilities:

- Outer agent iteration loop and LLM call orchestration.
- Phase 5 parallel orchestrator bridge.
- Phase 3 and Phase 5 repair-hint generation.
- Phase transition message rebuilding.
- Tool invocation guards, output validation, redundant search suppression, and read/write parallelization checks.
- Tool batch execution and chunk emission ordering.
- Repeated phase-transition branches inside `run()`.

The previous plan correctly targeted helper extraction, but it underweighted the size and complexity still left inside `run()`. This V2 plan adds two extra stages adopted from the external review:

- Extract the tool batch sub-loop into `_execute_tool_batch()` first, then move it to `agent/execution/tool_batches.py`.
- Extract repeated phase-transition handling into `_handle_phase_transition()` with an explicit outcome type.

## Non-Negotiable Compatibility Constraints

- Keep `from agent.loop import AgentLoop` working.
- Keep monkeypatch target `agent.loop.AgentLoop.run` working.
- Keep `AgentLoop.should_use_parallel_phase5(plan, config)` working.
- Keep these private compatibility methods during this refactor because tests call or patch at least some of them:
  - `_rebuild_messages_for_phase_change`
  - `_rebuild_messages_for_phase3_step_change`
  - `_extract_original_user_message`
  - `_copy_message`
  - `_current_tool_names`
  - `_build_backtrack_notice`
  - `_pre_execution_skip_result`
  - `_validate_tool_output`
  - `_is_parallel_read_call`
  - `_is_backtrack_result`
  - `_build_skipped_tool_result`
  - `_should_skip_redundant_update`
- Keep `agent.orchestrator`, `agent.day_worker`, and `agent.worker_prompt` imports working with compatibility re-export modules after Phase 5 files move.
- Keep `backend/tests/test_loop_phase5_routing.py` monkeypatch path `agent.orchestrator.Phase5Orchestrator` working. The Phase 5 parallel bridge must import `Phase5Orchestrator` through `agent.orchestrator`, not directly from `agent.phase5.orchestrator`, until tests are intentionally migrated.
- Do not change SSE chunk order. Tool-call protocol order is especially sensitive: `assistant(tool_calls)` must be followed by contiguous `tool` messages before injected system messages.
- Do not change `IterationProgress` semantics. API error handling reads `agent.progress`.
- Preserve all existing hooks: `before_llm_call`, `after_tool_call`, and `after_tool_result`.
- Do not use destructive rollback commands such as `git checkout --` as the default recovery path. If a task fails, inspect the diff, patch forward, or ask before discarding work.

## Target File Structure

```text
backend/agent/
  loop.py                         # Public AgentLoop facade and outer run loop

  execution/
    __init__.py
    message_rebuild.py             # Phase and Phase 3 step message rebuild helpers
    repair_hints.py                # Phase 3 / Phase 5 repair hint builders
    tool_invocation.py             # Tool guard, validation, read/write classification, search history
    tool_batches.py                # Tool-call batch executor after local extraction is stable

  phase5/
    __init__.py
    parallel.py                    # should_use_parallel_phase5 + run_parallel_phase5_orchestrator
    orchestrator.py                # moved from agent/orchestrator.py
    day_worker.py                  # moved from agent/day_worker.py
    worker_prompt.py               # moved from agent/worker_prompt.py

  orchestrator.py                  # compatibility re-export
  day_worker.py                    # compatibility re-export
  worker_prompt.py                 # compatibility re-export
```

## Size Guard Strategy

Use progressive thresholds so the test suite locks in real improvement without forcing unsafe extraction:

| Stage | Expected hard guard |
|-------|---------------------|
| Initial structure test | `loop.py < 1200` plus missing module checks fail red |
| After helper extraction | `loop.py < 900` |
| After local tool-batch and phase-transition extraction | `AgentLoop.run()` body visibly smaller; file may still be above 650 because the local methods remain |
| After moving tool batch to `execution/tool_batches.py` | `loop.py < 650` |
| Stretch target only if wrappers stay small | `loop.py < 550` |

Do not force `<450` in this round. That threshold is only reasonable after compatibility wrappers are removed in a separate cleanup.

## Task 1: Add Agent Loop Structure Tests

**Files:**
- Create: `backend/tests/test_agent_loop_structure.py`

- [ ] **Step 1: Add failing structure tests**

Create `backend/tests/test_agent_loop_structure.py`:

```python
from __future__ import annotations

import ast
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def line_count(relative_path: str) -> int:
    return len((BACKEND_DIR / relative_path).read_text(encoding="utf-8").splitlines())


def module_defines(relative_path: str, name: str) -> bool:
    tree = ast.parse((BACKEND_DIR / relative_path).read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.name == name
        for node in tree.body
    )


def test_agent_execution_and_phase5_packages_exist():
    agent_dir = BACKEND_DIR / "agent"
    execution_dir = agent_dir / "execution"
    phase5_dir = agent_dir / "phase5"

    expected_execution_modules = {
        "__init__.py",
        "message_rebuild.py",
        "repair_hints.py",
        "tool_invocation.py",
        "tool_batches.py",
    }
    expected_phase5_modules = {
        "__init__.py",
        "parallel.py",
        "orchestrator.py",
        "day_worker.py",
        "worker_prompt.py",
    }

    assert expected_execution_modules.issubset(
        {path.name for path in execution_dir.glob("*.py")}
    )
    assert expected_phase5_modules.issubset(
        {path.name for path in phase5_dir.glob("*.py")}
    )


def test_agent_execution_modules_expose_expected_names():
    assert module_defines(
        "agent/execution/repair_hints.py",
        "build_phase3_state_repair_message",
    )
    assert module_defines(
        "agent/execution/repair_hints.py",
        "build_phase5_state_repair_message",
    )
    assert module_defines(
        "agent/execution/message_rebuild.py",
        "rebuild_messages_for_phase_change",
    )
    assert module_defines(
        "agent/execution/tool_invocation.py",
        "SearchHistoryTracker",
    )
    assert module_defines(
        "agent/execution/tool_batches.py",
        "execute_tool_batch",
    )


def test_agent_loop_public_surface_and_size_guard():
    loop_text = (BACKEND_DIR / "agent/loop.py").read_text(encoding="utf-8")

    assert "class AgentLoop" in loop_text
    assert "async def run(" in loop_text
    assert line_count("agent/loop.py") < 1200


def test_agent_loop_compatibility_methods_remain():
    from agent.loop import AgentLoop

    for method_name in (
        "should_use_parallel_phase5",
        "_rebuild_messages_for_phase_change",
        "_rebuild_messages_for_phase3_step_change",
        "_pre_execution_skip_result",
        "_validate_tool_output",
        "_is_parallel_read_call",
        "_should_skip_redundant_update",
    ):
        assert hasattr(AgentLoop, method_name)


def test_phase5_compatibility_imports_remain():
    from agent.day_worker import run_day_worker
    from agent.orchestrator import Phase5Orchestrator
    from agent.worker_prompt import DayTask

    assert Phase5Orchestrator is not None
    assert run_day_worker is not None
    assert DayTask is not None
```

- [ ] **Step 2: Run the new structure test and verify red**

Run:

```bash
pytest backend/tests/test_agent_loop_structure.py -q
```

Expected: FAIL because `backend/agent/execution/` and `backend/agent/phase5/` do not exist yet.

- [ ] **Step 3: Review checkpoint**

Confirm the failure is only about missing target modules or names. Do not change implementation code in this task.

## Task 2: Move Phase 5 Into A Dedicated Subpackage

**Files:**
- Create: `backend/agent/phase5/__init__.py`
- Create: `backend/agent/phase5/parallel.py`
- Move: `backend/agent/orchestrator.py` -> `backend/agent/phase5/orchestrator.py`
- Move: `backend/agent/day_worker.py` -> `backend/agent/phase5/day_worker.py`
- Move: `backend/agent/worker_prompt.py` -> `backend/agent/phase5/worker_prompt.py`
- Modify: `backend/agent/orchestrator.py`
- Modify: `backend/agent/day_worker.py`
- Modify: `backend/agent/worker_prompt.py`
- Modify: `backend/agent/loop.py`

- [ ] **Step 1: Create `backend/agent/phase5/parallel.py`**

Create `parallel.py` with these public functions:

```python
from __future__ import annotations

import time
from typing import Any, AsyncIterator

from agent.internal_tasks import InternalTask
from config import Phase5ParallelConfig
from llm.types import ChunkType, LLMChunk


def should_use_parallel_phase5(
    plan: Any | None,
    config: Phase5ParallelConfig | None,
) -> bool:
    if plan is None or config is None:
        return False
    if not config.enabled:
        return False
    if plan.phase != 5:
        return False
    if plan.daily_plans:
        return False
    if not plan.selected_skeleton_id:
        return False
    if not plan.skeleton_plans:
        return False
    return True


async def run_parallel_phase5_orchestrator(
    *,
    plan: Any,
    llm: Any,
    tool_engine: Any,
    config: Phase5ParallelConfig | None,
) -> AsyncIterator[LLMChunk]:
    from agent.orchestrator import Phase5Orchestrator

    task_id = f"phase5_orchestration:{plan.session_id if plan else 'unknown'}"
    started_at = time.time()
    yield LLMChunk(
        type=ChunkType.INTERNAL_TASK,
        internal_task=InternalTask(
            id=task_id,
            kind="phase5_orchestration",
            label="Phase 5 并行编排",
            status="pending",
            message="正在拆分每日任务并并行生成行程…",
            blocking=True,
            scope="turn",
            started_at=started_at,
        ),
    )

    orchestrator = Phase5Orchestrator(
        plan=plan,
        llm=llm,
        tool_engine=tool_engine,
        config=config,
    )
    try:
        async for chunk in orchestrator.run():
            yield chunk
    except Exception as exc:
        yield LLMChunk(
            type=ChunkType.INTERNAL_TASK,
            internal_task=InternalTask(
                id=task_id,
                kind="phase5_orchestration",
                label="Phase 5 并行编排",
                status="error",
                message="并行逐日行程生成失败。",
                blocking=True,
                scope="turn",
                error=str(exc),
                started_at=started_at,
                ended_at=time.time(),
            ),
        )
        raise

    completed = bool(getattr(plan, "daily_plans", None))
    yield LLMChunk(
        type=ChunkType.INTERNAL_TASK,
        internal_task=InternalTask(
            id=task_id,
            kind="phase5_orchestration",
            label="Phase 5 并行编排",
            status="success" if completed else "warning",
            message=(
                "并行逐日行程生成完成"
                if completed
                else "并行生成未完全成功，已降级或等待后续串行处理。"
            ),
            blocking=True,
            scope="turn",
            result={"fallback": not completed},
            started_at=started_at,
            ended_at=time.time(),
        ),
    )
```

The `from agent.orchestrator import Phase5Orchestrator` import is intentional. It preserves the existing monkeypatch path used by tests.

- [ ] **Step 2: Move Phase 5 files and keep compatibility re-exports**

Use normal file moves, then replace the old root files with these shims:

`backend/agent/orchestrator.py`

```python
from agent.phase5.orchestrator import *  # noqa: F401,F403
```

`backend/agent/day_worker.py`

```python
from agent.phase5.day_worker import *  # noqa: F401,F403
```

`backend/agent/worker_prompt.py`

```python
from agent.phase5.worker_prompt import *  # noqa: F401,F403
```

- [ ] **Step 3: Update imports inside moved Phase 5 files**

In `backend/agent/phase5/orchestrator.py`, use:

```python
from agent.phase5.day_worker import DayWorkerResult, run_day_worker
from agent.phase5.worker_prompt import (
    DayTask,
    build_shared_prefix,
    split_skeleton_to_day_tasks,
)
```

In `backend/agent/phase5/day_worker.py`, use:

```python
from agent.phase5.worker_prompt import DayTask, build_day_suffix, build_shared_prefix
```

- [ ] **Step 4: Delegate Phase 5 methods in `AgentLoop`**

In `backend/agent/loop.py`, import:

```python
from agent.phase5.parallel import (
    run_parallel_phase5_orchestrator,
    should_use_parallel_phase5,
)
```

Keep the existing `AgentLoop` methods as compatibility wrappers:

```python
    @staticmethod
    def should_use_parallel_phase5(
        plan: Any | None,
        config: Phase5ParallelConfig | None,
    ) -> bool:
        return should_use_parallel_phase5(plan, config)

    async def _run_parallel_phase5_orchestrator(self) -> AsyncIterator[LLMChunk]:
        async for chunk in run_parallel_phase5_orchestrator(
            plan=self.plan,
            llm=self.llm,
            tool_engine=self.tool_engine,
            config=self.phase5_parallel_config,
        ):
            yield chunk
```

- [ ] **Step 5: Test and review**

Run:

```bash
python -m py_compile backend/agent/loop.py backend/agent/phase5/*.py backend/agent/orchestrator.py backend/agent/day_worker.py backend/agent/worker_prompt.py
pytest backend/tests/test_agent_loop_structure.py backend/tests/test_loop_phase5_routing.py backend/tests/test_orchestrator.py backend/tests/test_day_worker.py backend/tests/test_day_worker_progress_callback.py backend/tests/test_worker_prompt.py backend/tests/test_parallel_phase5_integration.py -q
```

Expected: PASS. Review that imports and monkeypatches still resolve through `agent.orchestrator`, `agent.day_worker`, and `agent.worker_prompt`.

## Task 3: Extract Repair Hint Builders

**Files:**
- Create: `backend/agent/execution/__init__.py`
- Create: `backend/agent/execution/repair_hints.py`
- Modify: `backend/agent/loop.py`

- [ ] **Step 1: Create repair-hint module**

Create `backend/agent/execution/repair_hints.py` with:

```python
from __future__ import annotations

import re
from typing import Any


def build_phase3_state_repair_message(
    *,
    plan: Any | None,
    current_phase: int,
    assistant_text: str,
    repair_hints_used: set[str],
) -> str | None:
    if current_phase != 3 or plan is None:
        return None
    if not plan.destination:
        return None
    text = assistant_text.strip()
    if len(text) < 12:
        return None

    step = getattr(plan, "phase3_step", "")
    repair_key = f"p3_{step}"
    if repair_key in repair_hints_used:
        stronger_key = f"p3_{step}_retry"
        if stronger_key in repair_hints_used:
            return None
        repair_key = stronger_key

    skeleton_signals = ("骨架", "轻松版", "平衡版", "高密度版", "深度版", "跳岛")
    has_skeleton_signals = any(token in text for token in skeleton_signals) or bool(
        re.search(r"方案\s*[A-C1-3]", text)
    )

    if (
        step == "brief"
        and not plan.trip_brief
        and any(token in text for token in ("画像", "偏好", "约束", "预算", "日期", "旅行"))
    ):
        repair_hints_used.add(repair_key)
        return (
            "[状态同步提醒]\n"
            "你刚刚已经完成了旅行画像说明，但 `trip_brief` 仍为空。"
            "请先调用 `set_trip_brief(fields={goal, pace, departure_city})`"
            " 写入画像核心字段；must_do 用 `add_preferences` 写入，"
            "avoid 用 `add_constraints` 写入，预算用 `update_trip_basics` 写入。"
            "写完后再继续，不要重复整段面向用户解释。"
        )

    if step == "candidate":
        if not plan.shortlist and any(
            token in text for token in ("候选", "推荐", "不建议", "why", "why_not")
        ):
            repair_hints_used.add(repair_key)
            if not plan.candidate_pool:
                return (
                    "[状态同步提醒]\n"
                    "你刚刚已经给出了候选筛选结果，但 `candidate_pool` 仍为空。"
                    "请先调用 `set_candidate_pool(pool=[...])` 写入候选全集，"
                    "再调用 `set_shortlist(items=[...])` 写入第一轮筛选结果。"
                    "写入 shortlist 后系统会自动推进子阶段。"
                )
            return (
                "[状态同步提醒]\n"
                "你刚刚已经给出了候选筛选结果，但 `shortlist` 仍为空。"
                "请先调用 `set_shortlist(items=[...])` 写入第一轮筛选结果。"
                "写入 shortlist 后系统会自动推进子阶段。"
            )

        if not plan.skeleton_plans and has_skeleton_signals:
            repair_hints_used.add(repair_key)
            return (
                "[状态同步提醒]\n"
                "你刚刚已经给出了骨架方案，但 `skeleton_plans` 仍为空。"
                "请先调用 `set_skeleton_plans(plans=[...])`"
                " 写入结构化骨架方案列表（每个方案必须包含 `id` 和 `name`）。"
                '如果用户已经明确选中某套方案，再调用 `select_skeleton(id="...")`。'
                "写入后系统会自动推进子阶段。"
            )

    if step == "skeleton" and not plan.skeleton_plans and has_skeleton_signals:
        repair_hints_used.add(repair_key)
        return (
            "[状态同步提醒]\n"
            "你刚刚已经给出了 2-3 套骨架方案，但 `skeleton_plans` 仍为空。"
            "请先调用 `set_skeleton_plans(plans=[...])`"
            " 写入结构化骨架方案列表。"
            '如果用户已经明确选中某套方案，再调用 `select_skeleton(id="...")`，'
            "系统会自动推进到 lock 子阶段。"
        )

    if step == "lock":
        missing_fields: list[str] = []
        if not plan.transport_options and any(
            token in text for token in ("航班", "火车", "高铁", "交通")
        ):
            missing_fields.append("`set_transport_options(options=[...])`")
        if (
            not plan.accommodation_options
            and not plan.accommodation
            and any(token in text for token in ("住宿", "酒店", "民宿", "旅馆"))
        ):
            missing_fields.append(
                "`set_accommodation_options(options=[...])` 或 `set_accommodation(area=...)`"
            )
        if not plan.risks and any(token in text for token in ("风险", "注意", "天气")):
            missing_fields.append("`set_risks(list=[...])`")
        if not plan.alternatives and any(
            token in text for token in ("备选", "替代", "雨天")
        ):
            missing_fields.append("`set_alternatives(list=[...])`")
        if missing_fields:
            repair_hints_used.add(repair_key)
            fields_str = "、".join(missing_fields)
            return (
                "[状态同步提醒]\n"
                f"你刚刚已经给出了锁定阶段建议，但以下字段仍未写入：{fields_str}。"
                "请先把结构化结果写入对应字段；只有用户明确选中了交通或住宿时，才写 `selected_transport` 或 `accommodation`。"
            )

    return None


def build_phase5_state_repair_message(
    *,
    plan: Any | None,
    current_phase: int,
    assistant_text: str,
    repair_hints_used: set[str],
) -> str | None:
    if current_phase != 5 or plan is None:
        return None
    if not plan.dates:
        return None
    repair_key = "p5_daily"
    if repair_key in repair_hints_used:
        return None
    text = assistant_text.strip()
    if len(text) < 20:
        return None

    total_days = plan.dates.total_days
    planned_days = set()
    for daily_plan in plan.daily_plans:
        if hasattr(daily_plan, "day"):
            planned_days.add(daily_plan.day)
        elif isinstance(daily_plan, dict):
            planned_days.add(daily_plan.get("day"))
    planned_count = len(planned_days)

    if planned_count >= total_days:
        return None

    day_pattern_count = len(
        re.findall(
            r"第\s*[1-9一二三四五六七八九十]\s*天|Day\s*\d|DAY\s*\d",
            text,
        )
    )
    has_time_slots = bool(re.search(r"\d{1,2}:\d{2}", text))
    has_activity_markers = any(
        keyword in text
        for keyword in ("活动", "景点", "行程", "安排", "上午", "下午", "晚上", "餐厅")
    )
    has_json_markers = (
        sum(
            1
            for keyword in ('"day"', '"date"', '"activities"', '"start_time"')
            if keyword in text
        )
        >= 2
    )
    has_date_patterns = bool(re.search(r"\d{4}-\d{2}-\d{2}", text))

    if (
        (day_pattern_count >= 1 and (has_time_slots or has_activity_markers))
        or has_json_markers
        or (has_date_patterns and has_activity_markers)
    ):
        repair_hints_used.add(repair_key)
        return (
            "[状态同步提醒]\n"
            f"你刚刚已经给出了逐日行程安排，但 `daily_plans` 仍只有 {planned_count}/{total_days} 天。"
            '请立即调用 `save_day_plan(mode="create", day=缺失天数, date=对应日期, activities=活动列表)` 逐天保存缺失天数，'
            "或在需要一次性完整覆盖时调用 `replace_all_day_plans(days=完整天数列表)`。"
            "`optimize_day_route` 只做路线辅助，不能替代状态写入。"
        )
    return None
```

- [ ] **Step 2: Replace `AgentLoop` methods with wrappers**

In `backend/agent/loop.py`, import the two functions and replace the old method bodies with wrappers that pass `plan=self.plan`.

- [ ] **Step 3: Test and review**

Run:

```bash
python -m py_compile backend/agent/loop.py backend/agent/execution/repair_hints.py
pytest backend/tests/test_agent_loop.py backend/tests/test_e2e_golden_path.py -q
```

Expected: PASS. Review that `re` is no longer imported in `loop.py` unless another remaining block uses it.

## Task 4: Extract Message Rebuild Helpers

**Files:**
- Create: `backend/agent/execution/message_rebuild.py`
- Modify: `backend/agent/loop.py`

- [ ] **Step 1: Create `message_rebuild.py`**

Create functions with these signatures:

```python
from __future__ import annotations

from typing import Any

from agent.types import Message, Role, ToolResult


def copy_message(message: Message) -> Message:
    return Message(
        role=message.role,
        content=message.content,
        tool_calls=message.tool_calls,
        tool_result=message.tool_result,
        name=message.name,
    )


def extract_original_user_message(messages: list[Message]) -> Message:
    for message in reversed(messages):
        if message.role == Role.USER:
            return copy_message(message)
    return Message(role=Role.USER, content="")


def current_tool_names(
    *,
    tool_engine: Any,
    plan: Any | None,
    phase: int | None = None,
) -> list[str]:
    target_phase = phase if phase is not None else (plan.phase if plan is not None else None)
    if target_phase is None:
        return []
    return [
        tool["name"]
        for tool in tool_engine.get_tools_for_phase(target_phase, plan)
    ]


def build_backtrack_notice(
    *,
    plan: Any | None,
    from_phase: int,
    to_phase: int,
    result: ToolResult,
) -> str:
    reason = "用户请求回退"
    if isinstance(result.data, dict) and result.data.get("reason"):
        reason = str(result.data["reason"])
    elif getattr(plan, "backtrack_history", None):
        reason = plan.backtrack_history[-1].reason
    return f"[阶段回退]\n用户从 phase {from_phase} 回退到 phase {to_phase}，原因：{reason}"


async def rebuild_messages_for_phase_change(
    *,
    phase_router: Any | None,
    context_manager: Any | None,
    plan: Any | None,
    memory_mgr: Any | None,
    memory_enabled: bool,
    user_id: str,
    tool_engine: Any,
    from_phase: int,
    to_phase: int,
    original_user_message: Message,
    result: ToolResult,
) -> list[Message]:
    if (
        phase_router is None
        or context_manager is None
        or plan is None
        or memory_mgr is None
    ):
        raise RuntimeError("Phase-aware rebuild requires router/context/plan/memory")

    phase_prompt = phase_router.get_prompt_for_plan(plan)
    memory_context, _recalled_ids, *_ = (
        await memory_mgr.generate_context(user_id, plan)
        if memory_enabled
        else ("暂无相关用户记忆", [], 0, 0, 0)
    )
    rebuilt = [
        context_manager.build_system_message(
            plan,
            phase_prompt,
            memory_context,
            available_tools=current_tool_names(
                tool_engine=tool_engine,
                plan=plan,
                phase=to_phase,
            ),
        )
    ]

    if to_phase < from_phase:
        rebuilt.append(
            Message(
                role=Role.SYSTEM,
                content=build_backtrack_notice(
                    plan=plan,
                    from_phase=from_phase,
                    to_phase=to_phase,
                    result=result,
                ),
            )
        )
    else:
        rebuilt.append(
            Message(
                role=Role.ASSISTANT,
                content=context_manager.build_phase_handoff_note(
                    plan=plan,
                    from_phase=from_phase,
                    to_phase=to_phase,
                ),
            )
        )

    rebuilt.append(copy_message(original_user_message))
    return rebuilt


async def rebuild_messages_for_phase3_step_change(
    *,
    phase_router: Any | None,
    context_manager: Any | None,
    plan: Any | None,
    memory_mgr: Any | None,
    memory_enabled: bool,
    user_id: str,
    tool_engine: Any,
    original_user_message: Message,
) -> list[Message]:
    if (
        phase_router is None
        or context_manager is None
        or plan is None
        or memory_mgr is None
    ):
        raise RuntimeError("Phase3 step rebuild requires router/context/plan/memory")

    phase_prompt = phase_router.get_prompt_for_plan(plan)
    memory_context, _recalled_ids, *_ = (
        await memory_mgr.generate_context(user_id, plan)
        if memory_enabled
        else ("暂无相关用户记忆", [], 0, 0, 0)
    )
    return [
        context_manager.build_system_message(
            plan,
            phase_prompt,
            memory_context,
            available_tools=current_tool_names(
                tool_engine=tool_engine,
                plan=plan,
                phase=plan.phase,
            ),
        ),
        copy_message(original_user_message),
    ]
```

- [ ] **Step 2: Keep `AgentLoop` wrappers**

Replace `AgentLoop` helper bodies with wrappers. Example:

```python
    async def _rebuild_messages_for_phase_change(
        self,
        messages: list[Message],
        from_phase: int,
        to_phase: int,
        original_user_message: Message,
        result: ToolResult,
    ) -> list[Message]:
        return await rebuild_messages_for_phase_change(
            phase_router=self.phase_router,
            context_manager=self.context_manager,
            plan=self.plan,
            memory_mgr=self.memory_mgr,
            memory_enabled=self.memory_enabled,
            user_id=self.user_id,
            tool_engine=self.tool_engine,
            from_phase=from_phase,
            to_phase=to_phase,
            original_user_message=original_user_message,
            result=result,
        )
```

Keep wrappers for `_extract_original_user_message`, `_copy_message`, `_current_tool_names`, and `_build_backtrack_notice`.

- [ ] **Step 3: Test and review**

Run:

```bash
python -m py_compile backend/agent/loop.py backend/agent/execution/message_rebuild.py
pytest backend/tests/test_agent_loop.py backend/tests/test_phase_transition_event.py -q
```

Expected: PASS. Review direct calls and patches of `_rebuild_messages_for_phase_change`.

## Task 5: Extract Tool Invocation Helpers

**Files:**
- Create: `backend/agent/execution/tool_invocation.py`
- Modify: `backend/agent/loop.py`

- [ ] **Step 1: Create `tool_invocation.py`**

Create the module:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.types import ToolCall, ToolResult


SEARCH_TOOLS = {
    "web_search",
    "xiaohongshu_search",
    "xiaohongshu_search_notes",
    "quick_travel_search",
}


@dataclass
class SearchHistoryTracker:
    max_size: int = 20
    recent_queries: list[str] = field(default_factory=list)

    def should_skip_redundant_update(self, tool_call: ToolCall) -> bool:
        if tool_call.name not in SEARCH_TOOLS:
            return False

        argument_name = (
            "keyword" if tool_call.name == "xiaohongshu_search_notes" else "query"
        )
        query = (tool_call.arguments or {}).get(argument_name, "")
        if not isinstance(query, str) or not query.strip():
            return False

        normalized = query.strip().lower()
        count = sum(1 for seen_query in self.recent_queries if seen_query == normalized)
        self.recent_queries.append(normalized)
        if len(self.recent_queries) > self.max_size:
            del self.recent_queries[:-self.max_size]
        return count >= 2


def is_backtrack_result(result: ToolResult) -> bool:
    return (
        result.status == "success"
        and isinstance(result.data, dict)
        and bool(result.data.get("backtracked"))
    )


def build_skipped_tool_result(
    tool_call_id: str,
    *,
    error: str,
    error_code: str,
    suggestion: str,
) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_call_id,
        status="skipped",
        error=error,
        error_code=error_code,
        suggestion=suggestion,
    )


def pre_execution_skip_result(
    *,
    tool_call: ToolCall,
    guardrail: Any | None,
    search_history: SearchHistoryTracker,
) -> ToolResult | None:
    if search_history.should_skip_redundant_update(tool_call):
        query = (tool_call.arguments or {}).get("query", "")
        return build_skipped_tool_result(
            tool_call.id,
            error=f'相同查询 "{query}" 已搜索过多次且未得到新结果。',
            error_code="REDUNDANT_SEARCH",
            suggestion=(
                "请不要重复搜索相同内容。"
                "如果搜索没有找到需要的信息，请换一个查询方向，"
                "或直接根据已有信息推进规划（调用状态写入工具写入产物）。"
            ),
        )

    if guardrail is None:
        return None

    guardrail_result = guardrail.validate_input(tool_call)
    if guardrail_result.allowed:
        return None
    return build_skipped_tool_result(
        tool_call.id,
        error=guardrail_result.reason,
        error_code="GUARDRAIL_REJECTED",
        suggestion=guardrail_result.reason,
    )


def validate_tool_output(
    *,
    guardrail: Any | None,
    tool_call: ToolCall,
    result: ToolResult,
) -> ToolResult:
    if guardrail is None or result.status != "success":
        return result

    output_check = guardrail.validate_output(tool_call.name, result.data)
    if output_check.level != "warn" or not output_check.reason:
        return result
    return ToolResult(
        tool_call_id=result.tool_call_id,
        status=result.status,
        data=result.data,
        metadata=result.metadata,
        suggestion=output_check.reason,
    )


def is_parallel_read_call(
    *,
    parallel_tool_execution: bool,
    tool_engine: Any,
    tool_call: ToolCall,
) -> bool:
    if not parallel_tool_execution:
        return False
    tool_def = tool_engine.get_tool(tool_call.name)
    return tool_def is None or tool_def.side_effect != "write"
```

- [ ] **Step 2: Keep wrappers and preserve `_recent_search_queries` compatibility**

In `AgentLoop.__init__`, add:

```python
self._search_history = SearchHistoryTracker()
self._recent_search_queries = self._search_history.recent_queries
```

Replace the old helper bodies with wrappers that call `tool_invocation.py`.

- [ ] **Step 3: Test and review**

Run:

```bash
python -m py_compile backend/agent/loop.py backend/agent/execution/tool_invocation.py
pytest backend/tests/test_agent_loop.py backend/tests/test_parallel_tool_call_sequence.py backend/tests/test_guardrail.py -q
```

Expected: PASS. Review that `is_parallel_read_call` receives both `parallel_tool_execution` and `tool_engine`.

## Task 6: Locally Extract `_execute_tool_batch()` Inside `AgentLoop`

**Files:**
- Modify: `backend/agent/loop.py`

This task adopts the external plan's safest `run()` reduction step. The method stays inside `AgentLoop` first so behavior can be verified before crossing a module boundary.

- [ ] **Step 1: Add outcome dataclass near the top of `loop.py`**

```python
from dataclasses import dataclass


@dataclass
class ToolBatchOutcome:
    saw_state_update: bool
    needs_rebuild: bool
    rebuild_result: ToolResult | None
```

The `rebuild_result` type is `ToolResult | None`, not `list[Message] | None`.

- [ ] **Step 2: Extract the tool execution sub-loop into an async iterator method**

Add `AgentLoop._execute_tool_batch(self, *, tool_calls: list[ToolCall], messages: list[Message]) -> AsyncIterator[LLMChunk | ToolBatchOutcome]`.

The method owns exactly the existing `idx = 0` through `while idx < len(tool_calls)` block. It must preserve these behaviors:

- `self._pre_execution_skip_result()` is checked before execution and before parallel read batching.
- Parallel read batches still call `self.tool_engine.execute_batch()`.
- Each tool result appends a `Role.TOOL` message before yielding `TOOL_RESULT`.
- Keepalive chunks still precede `after_tool_call`.
- `after_tool_call` still runs before the `TOOL_RESULT` chunk.
- `_run_after_tool_result_hook()` chunks still stream after the `TOOL_RESULT` chunk.
- Backtrack still emits skipped results for non-executed remaining tool calls.
- The final item yielded by the method is a `ToolBatchOutcome` instance.

- [ ] **Step 3: Replace the inlined block in `run()`**

In `run()`, replace the inlined batch loop with:

```python
batch_outcome: ToolBatchOutcome | None = None
async for batch_item in self._execute_tool_batch(
    tool_calls=tool_calls,
    messages=messages,
):
    if isinstance(batch_item, LLMChunk):
        yield batch_item
    else:
        batch_outcome = batch_item

if batch_outcome is None:
    raise RuntimeError("Tool batch execution finished without an outcome")

saw_state_update = batch_outcome.saw_state_update
needs_rebuild = batch_outcome.needs_rebuild
rebuild_result = batch_outcome.rebuild_result
```

- [ ] **Step 4: Test and review**

Run:

```bash
python -m py_compile backend/agent/loop.py
pytest backend/tests/test_agent_loop.py backend/tests/test_parallel_tool_call_sequence.py backend/tests/test_tool_human_label.py backend/tests/test_phase_transition_event.py -q
```

Expected: PASS. Review `git diff` for chunk-order changes before continuing.

## Task 7: Locally Extract `_handle_phase_transition()`

**Files:**
- Modify: `backend/agent/loop.py`

- [ ] **Step 1: Add explicit outcome dataclass**

```python
@dataclass
class PhaseTransitionOutcome:
    messages: list[Message]
    current_phase: int
    tools: list[dict]
```

- [ ] **Step 2: Add an async iterator helper**

Add this method to `AgentLoop`:

```python
    async def _handle_phase_transition(
        self,
        *,
        messages: list[Message],
        from_phase: int,
        to_phase: int,
        from_step: Any,
        reason: str,
        original_user_message: Message,
        result: ToolResult,
    ) -> AsyncIterator[LLMChunk | PhaseTransitionOutcome]:
        yield LLMChunk(
            type=ChunkType.PHASE_TRANSITION,
            phase_info={
                "from_phase": from_phase,
                "to_phase": to_phase,
                "from_step": from_step,
                "to_step": getattr(self.plan, "phase3_step", None),
                "reason": reason,
            },
        )
        rebuilt_messages = await self._rebuild_messages_for_phase_change(
            messages=messages,
            from_phase=from_phase,
            to_phase=to_phase,
            original_user_message=original_user_message,
            result=result,
        )
        yield PhaseTransitionOutcome(
            messages=rebuilt_messages,
            current_phase=to_phase,
            tools=self.tool_engine.get_tools_for_phase(to_phase, self.plan),
        )
```

- [ ] **Step 3: Replace the three repeated branches**

Use `_handle_phase_transition()` for these reasons:

- `"backtrack"`
- `"plan_tool_direct"`
- `"check_and_apply_transition"`

Each branch must still set:

```python
prev_iteration_had_tools = True
phase_changed_in_prev_iteration = True
```

and must still `continue` after applying the outcome.

- [ ] **Step 4: Test and review**

Run:

```bash
python -m py_compile backend/agent/loop.py
pytest backend/tests/test_phase_transition_event.py backend/tests/test_agent_loop.py -q
```

Expected: PASS. Review that phase transition events still appear before message rebuilds.

## Task 8: Move Tool Batch Execution To `execution/tool_batches.py`

**Files:**
- Create: `backend/agent/execution/tool_batches.py`
- Modify: `backend/agent/loop.py`

- [ ] **Step 1: Create external executor**

Move `ToolBatchOutcome` and the stable `_execute_tool_batch()` body into `backend/agent/execution/tool_batches.py` as:

```python
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import LLMChunk
from run import IterationProgress


@dataclass
class ToolBatchOutcome:
    progress: IterationProgress
    saw_state_update: bool
    needs_rebuild: bool
    rebuild_result: ToolResult | None
    next_parallel_group_counter: int
```

Add `execute_tool_batch(...)` with this full callable signature:

```text
execute_tool_batch(
    *,
    tool_calls: list[ToolCall],
    messages: list[Message],
    tool_engine: Any,
    hooks: Any,
    guardrail: Any | None,
    parallel_tool_execution: bool,
    parallel_group_counter: int,
    search_history: Any,
    check_cancelled: Callable[[], None],
    run_after_tool_result_hook: Callable[..., AsyncIterator[LLMChunk]],
    current_progress: IterationProgress,
) -> AsyncIterator[LLMChunk | ToolBatchOutcome]
```

The external executor should use functions from `agent.execution.tool_invocation` directly. It must not call back into `AgentLoop` except through the two injected callables.

- [ ] **Step 2: Make `AgentLoop._execute_tool_batch()` a wrapper**

Keep the private method as a wrapper:

```python
    async def _execute_tool_batch(
        self,
        *,
        tool_calls: list[ToolCall],
        messages: list[Message],
    ) -> AsyncIterator[LLMChunk | ToolBatchOutcome]:
        async for batch_item in execute_tool_batch(
            tool_calls=tool_calls,
            messages=messages,
            tool_engine=self.tool_engine,
            hooks=self.hooks,
            guardrail=self.guardrail,
            parallel_tool_execution=self.parallel_tool_execution,
            parallel_group_counter=self._parallel_group_counter,
            search_history=self._search_history,
            check_cancelled=self._check_cancelled,
            run_after_tool_result_hook=self._run_after_tool_result_hook,
            current_progress=self._progress,
        ):
            if isinstance(batch_item, ToolBatchOutcome):
                self._parallel_group_counter = batch_item.next_parallel_group_counter
                self._progress = batch_item.progress
            yield batch_item
```

- [ ] **Step 3: Tighten the line-count test**

In `backend/tests/test_agent_loop_structure.py`, change:

```python
assert line_count("agent/loop.py") < 1200
```

to:

```python
assert line_count("agent/loop.py") < 650
```

If compatibility wrappers keep the file slightly above 650, use the measured count plus 50 as the temporary guard and add a one-line comment naming the wrappers as the reason.

- [ ] **Step 4: Test and review**

Run:

```bash
python -m py_compile backend/agent/loop.py backend/agent/execution/tool_batches.py
pytest backend/tests/test_agent_loop.py backend/tests/test_parallel_tool_call_sequence.py backend/tests/test_tool_human_label.py backend/tests/test_phase_transition_event.py -q
```

Expected: PASS. Review that `TOOL_RESULT` chunk order and `parallel_group` metadata are unchanged.

## Task 9: Final Documentation And Full Verification

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Modify: `backend/tests/test_agent_loop_structure.py`

- [ ] **Step 1: Update `PROJECT_OVERVIEW.md`**

Update the backend tree entry for `agent/` so it describes the new structure:

```text
agent/                  # AgentLoop facade + execution helpers + Phase 5 orchestrator-workers subsystem
```

Update the Phase 5 section so it references:

```text
agent/phase5/orchestrator.py
agent/phase5/day_worker.py
agent/phase5/worker_prompt.py
```

- [ ] **Step 2: Run compilation**

Run:

```bash
python -m py_compile backend/agent/*.py backend/agent/execution/*.py backend/agent/phase5/*.py backend/main.py backend/api/*.py backend/api/orchestration/*.py backend/api/orchestration/*/*.py backend/api/routes/*.py
```

Expected: exit code 0.

- [ ] **Step 3: Run focused tests**

Run:

```bash
pytest backend/tests/test_agent_loop_structure.py backend/tests/test_agent_loop.py backend/tests/test_loop_phase5_routing.py backend/tests/test_parallel_tool_call_sequence.py backend/tests/test_phase_transition_event.py backend/tests/test_orchestrator.py backend/tests/test_day_worker.py backend/tests/test_day_worker_progress_callback.py backend/tests/test_worker_prompt.py backend/tests/test_parallel_phase5_integration.py -q
```

Expected: PASS.

- [ ] **Step 4: Run full backend tests**

Run:

```bash
pytest backend/tests -q
```

Expected: all tests pass. Existing OTEL `localhost:4317` warnings are local collector noise when the pytest exit code is 0.

- [ ] **Step 5: Run diff hygiene checks**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 6: Final review checklist**

Confirm each item:

- `agent.loop.AgentLoop.run` still exists and can be monkeypatched.
- `AgentLoop.should_use_parallel_phase5` still exists.
- `agent.orchestrator.Phase5Orchestrator` monkeypatches still affect Phase 5 parallel routing tests.
- Tool result chunk order is unchanged.
- `IterationProgress` transitions are unchanged for text-only, read-only tools, and write tools.
- Phase transition messages still rebuild with the original user message.
- Phase 3 step-change rebuild still omits handoff/backtrack notes.
- Phase 3 and Phase 5 repair hints still mutate `repair_hints_used`.
- Redundant search dedupe still caps query history at 20 entries.
- `PROJECT_OVERVIEW.md` reflects the new `agent/` structure.

## Execution Notes

- Execute tasks serially. After every task, run the listed tests and inspect the diff before continuing.
- Prefer patch-forward fixes over rollback. Ask before discarding local changes.
- Do not make commits unless the user explicitly asks. If committing, update `PROJECT_OVERVIEW.md` in the same commit whenever architecture changes.
