# Split `update_plan_state` Into Single-Responsibility Tools — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the omnibus `update_plan_state(field, value)` tool with 19 single-responsibility, strong-typed plan-writing tools to permanently eliminate the "stringified list/dict" failure mode.

**Architecture:** A thin write layer (`state/plan_writers.py`) provides one pure function per write operation. Both the new tools and the transitional adapter call this shared layer. Four migration steps: scaffold → adapter → expose → remove.

**Tech Stack:** Python 3.12+, FastAPI, pytest, async/await, JSON Schema

**Spec:** `docs/superpowers/specs/2026-04-15-split-update-plan-state-design.md`

---

## File Structure

```
backend/
  state/
    plan_writers.py                      # NEW — thin write layer (19 writer functions)
  tools/
    plan_tools/
      __init__.py                        # NEW — make_all_plan_tools() aggregator
      phase3_tools.py                    # NEW — 11 Category A Phase 3 tools
      daily_plans.py                     # NEW — append_day_plan + replace_daily_plans
      trip_basics.py                     # NEW — update_trip_basics (Category B)
      append_tools.py                    # NEW — 4 Category C tools
      backtrack.py                       # NEW — request_backtrack (Category D)
    update_plan_state.py                 # MODIFIED (Step 2), DELETED (Step 4)
    engine.py                            # MODIFIED (Step 3, 4)
  phase/
    prompts.py                           # MODIFIED (Step 3)
  agent/
    loop.py                              # MODIFIED (Step 3)
    reflection.py                        # MODIFIED (Step 3)
    tool_choice.py                       # MODIFIED (Step 3)
  harness/
    guardrail.py                         # MODIFIED (Step 3)
  main.py                                # MODIFIED (Step 3, 4)
  tests/
    test_plan_writers.py                 # NEW
    test_plan_tools/
      __init__.py                        # NEW
      test_phase3_tools.py               # NEW
      test_daily_plans.py                # NEW
      test_trip_basics.py                # NEW
      test_append_tools.py               # NEW
      test_backtrack.py                  # NEW
      test_init.py                       # NEW — integration tests
    test_update_plan_state_strict.py     # NEW (Step 2)
    helpers/
      __init__.py                        # NEW
      register_plan_tools.py             # NEW — batch helper
```

> **Implementation note:** The source spec originally sketches “one file per tool”, but this plan groups closely related ToolDefs into 5 modules by category (`phase3_tools.py`, `daily_plans.py`, `trip_basics.py`, `append_tools.py`, `backtrack.py`). That is an intentional execution-oriented deviation: the runtime surface still exposes **19** single-responsibility tools, while the code stays closer to current backend module size patterns and avoids 19 tiny near-duplicate files.

---

### Task 1: Create `state/plan_writers.py` — thin write layer

**Files:**
- Create: `backend/state/plan_writers.py`
- Create: `backend/tests/test_plan_writers.py`

- [ ] **Step 1: Create `backend/state/plan_writers.py`**

```python
# backend/state/plan_writers.py
"""Thin write layer for TravelPlanState mutations.

Every plan-writing operation is a pure function: take plan + data, mutate plan.
Type validation lives in the tool wrappers (ToolError); this layer performs
defensive assertions that should never fire in production.

Both the new single-responsibility tools AND the transitional update_plan_state
adapter call these functions, ensuring identical write behavior.
"""
from __future__ import annotations

from typing import Any

from state.models import (
    Accommodation,
    Constraint,
    DayPlan,
    Preference,
    TravelPlanState,
)
from state.intake import (
    parse_budget_value,
    parse_dates_value,
    parse_travelers_value,
)


# ---------------------------------------------------------------------------
# Category A: high-risk structured writes
# ---------------------------------------------------------------------------

def write_skeleton_plans(plan: TravelPlanState, plans: list[dict]) -> None:
    """Replace skeleton_plans wholesale."""
    assert isinstance(plans, list), f"Expected list, got {type(plans).__name__}"
    plan.skeleton_plans = plans


def write_selected_skeleton_id(plan: TravelPlanState, skeleton_id: str) -> None:
    """Lock a skeleton plan by ID."""
    assert isinstance(skeleton_id, str), f"Expected str, got {type(skeleton_id).__name__}"
    plan.selected_skeleton_id = skeleton_id


def write_candidate_pool(plan: TravelPlanState, pool: list[dict]) -> None:
    assert isinstance(pool, list), f"Expected list, got {type(pool).__name__}"
    plan.candidate_pool = pool


def write_shortlist(plan: TravelPlanState, items: list[dict]) -> None:
    assert isinstance(items, list), f"Expected list, got {type(items).__name__}"
    plan.shortlist = items


def write_transport_options(plan: TravelPlanState, options: list[dict]) -> None:
    assert isinstance(options, list), f"Expected list, got {type(options).__name__}"
    plan.transport_options = options


def write_selected_transport(plan: TravelPlanState, choice: dict) -> None:
    assert isinstance(choice, dict), f"Expected dict, got {type(choice).__name__}"
    plan.selected_transport = choice


def write_accommodation_options(plan: TravelPlanState, options: list[dict]) -> None:
    assert isinstance(options, list), f"Expected list, got {type(options).__name__}"
    plan.accommodation_options = options


def write_accommodation(plan: TravelPlanState, area: str, hotel: str | None = None) -> None:
    assert isinstance(area, str), f"Expected str for area, got {type(area).__name__}"
    plan.accommodation = Accommodation(area=area, hotel=hotel)


def write_risks(plan: TravelPlanState, risks: list[dict]) -> None:
    assert isinstance(risks, list), f"Expected list, got {type(risks).__name__}"
    plan.risks = risks


def write_alternatives(plan: TravelPlanState, alternatives: list[dict]) -> None:
    assert isinstance(alternatives, list), f"Expected list, got {type(alternatives).__name__}"
    plan.alternatives = alternatives


def write_trip_brief(plan: TravelPlanState, fields: dict) -> None:
    """Merge fields into existing trip_brief (incremental update)."""
    assert isinstance(fields, dict), f"Expected dict, got {type(fields).__name__}"
    plan.trip_brief.update(fields)


# ---------------------------------------------------------------------------
# Category A: daily plans
# ---------------------------------------------------------------------------

def append_one_day_plan(plan: TravelPlanState, day_dict: dict) -> None:
    """Append a single day to daily_plans."""
    assert isinstance(day_dict, dict), f"Expected dict, got {type(day_dict).__name__}"
    plan.daily_plans.append(DayPlan.from_dict(day_dict))


def replace_all_daily_plans(plan: TravelPlanState, days: list[dict]) -> None:
    """Replace the entire daily_plans list."""
    assert isinstance(days, list), f"Expected list, got {type(days).__name__}"
    plan.daily_plans = [DayPlan.from_dict(d) for d in days]


# ---------------------------------------------------------------------------
# Category B: phrase-tolerant basic writes
# ---------------------------------------------------------------------------

def write_destination(plan: TravelPlanState, value: Any) -> None:
    if isinstance(value, dict):
        plan.destination = str(value.get("name", value))
    else:
        plan.destination = str(value)


def write_dates(plan: TravelPlanState, value: Any) -> None:
    plan.dates = parse_dates_value(value)


def write_travelers(plan: TravelPlanState, value: Any) -> None:
    plan.travelers = parse_travelers_value(value)


def write_budget(plan: TravelPlanState, value: Any) -> None:
    plan.budget = parse_budget_value(value)


def write_departure_city(plan: TravelPlanState, value: Any) -> None:
    if isinstance(value, dict):
        city = (
            value.get("name")
            or value.get("city")
            or value.get("departure_city")
            or value.get("from")
        )
        plan.trip_brief["departure_city"] = str(city or value)
        return
    plan.trip_brief["departure_city"] = str(value)


# ---------------------------------------------------------------------------
# Category C: append-semantics
# ---------------------------------------------------------------------------

def _stringify_preference_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " · ".join(
            part
            for part in (_stringify_preference_value(item) for item in value)
            if part
        )
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            text = _stringify_preference_value(item)
            if text:
                parts.append(f"{key}: {text}")
        return "；".join(parts)
    return str(value)


def append_preferences(plan: TravelPlanState, items: list) -> None:
    """Append one or more preferences."""
    for item in items:
        if isinstance(item, dict):
            if "key" in item:
                plan.preferences.append(Preference.from_dict(item))
            else:
                for key, val in item.items():
                    plan.preferences.append(
                        Preference(key=str(key), value=_stringify_preference_value(val))
                    )
        elif isinstance(item, str):
            plan.preferences.append(Preference(key=item, value=""))
        else:
            plan.preferences.append(Preference(key=str(item), value=""))


def append_constraints(plan: TravelPlanState, items: list[dict]) -> None:
    """Append one or more constraints."""
    for item in items:
        if isinstance(item, dict):
            constraint_type = str(item.get("type", "soft"))
            description = str(
                item.get("description") or item.get("summary") or item
            )
            plan.constraints.append(
                Constraint(type=constraint_type, description=description)
            )
        else:
            plan.constraints.append(Constraint(type="soft", description=str(item)))


def append_destination_candidate(plan: TravelPlanState, item: dict) -> None:
    """Append a single destination candidate."""
    assert isinstance(item, dict), f"Expected dict, got {type(item).__name__}"
    plan.destination_candidates.append(item)


def replace_destination_candidates(plan: TravelPlanState, items: list[dict]) -> None:
    """Replace the entire destination_candidates list."""
    assert isinstance(items, list), f"Expected list, got {type(items).__name__}"
    plan.destination_candidates = items


# ---------------------------------------------------------------------------
# Category D: standalone action
# ---------------------------------------------------------------------------

def execute_backtrack(
    plan: TravelPlanState,
    to_phase: int,
    reason: str,
) -> dict:
    """Execute a phase backtrack. Returns result dict for tool response."""
    from phase.backtrack import BacktrackService

    if to_phase == 2:
        to_phase = 1
    if to_phase >= plan.phase:
        raise ValueError(
            f"只能回退到更早的阶段，当前阶段: {plan.phase}，目标: {to_phase}"
        )
    from_phase = plan.phase
    service = BacktrackService()
    service.execute(plan, to_phase, reason, snapshot_path="")
    return {
        "backtracked": True,
        "from_phase": from_phase,
        "to_phase": to_phase,
        "reason": reason,
        "next_action": "请向用户确认回退结果，不要继续调用其他工具",
    }
```

- [ ] **Step 2: Run basic import check**

```bash
cd backend && python -c "from state.plan_writers import write_skeleton_plans, write_candidate_pool, append_preferences, execute_backtrack; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Create `backend/tests/test_plan_writers.py`**

```python
# backend/tests/test_plan_writers.py
"""Unit tests for state/plan_writers.py — pure data mutation functions."""
from __future__ import annotations

import pytest

from state.models import TravelPlanState


@pytest.fixture
def plan():
    return TravelPlanState(session_id="pw-test")


# --- Category A: structured writes ---

class TestWriteSkeletonPlans:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_skeleton_plans
        plan.skeleton_plans = [{"id": "old"}]
        write_skeleton_plans(plan, [{"id": "a"}, {"id": "b"}])
        assert len(plan.skeleton_plans) == 2
        assert plan.skeleton_plans[0]["id"] == "a"

    def test_asserts_on_non_list(self, plan):
        from state.plan_writers import write_skeleton_plans
        with pytest.raises(AssertionError):
            write_skeleton_plans(plan, "not a list")


class TestWriteSelectedSkeletonId:
    def test_sets_id(self, plan):
        from state.plan_writers import write_selected_skeleton_id
        write_selected_skeleton_id(plan, "plan_a")
        assert plan.selected_skeleton_id == "plan_a"

    def test_asserts_on_non_str(self, plan):
        from state.plan_writers import write_selected_skeleton_id
        with pytest.raises(AssertionError):
            write_selected_skeleton_id(plan, 123)


class TestWriteCandidatePool:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_candidate_pool
        write_candidate_pool(plan, [{"name": "A"}, {"name": "B"}])
        assert len(plan.candidate_pool) == 2

    def test_asserts_on_non_list(self, plan):
        from state.plan_writers import write_candidate_pool
        with pytest.raises(AssertionError):
            write_candidate_pool(plan, {"name": "A"})


class TestWriteShortlist:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_shortlist
        write_shortlist(plan, [{"name": "A"}])
        assert len(plan.shortlist) == 1


class TestWriteTransportOptions:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_transport_options
        write_transport_options(plan, [{"type": "flight"}, {"type": "train"}])
        assert len(plan.transport_options) == 2


class TestWriteSelectedTransport:
    def test_sets_dict(self, plan):
        from state.plan_writers import write_selected_transport
        write_selected_transport(plan, {"type": "flight", "price": 1200})
        assert plan.selected_transport["type"] == "flight"

    def test_asserts_on_non_dict(self, plan):
        from state.plan_writers import write_selected_transport
        with pytest.raises(AssertionError):
            write_selected_transport(plan, "flight")


class TestWriteAccommodationOptions:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_accommodation_options
        write_accommodation_options(plan, [{"name": "Hotel A"}])
        assert len(plan.accommodation_options) == 1


class TestWriteAccommodation:
    def test_sets_area_and_hotel(self, plan):
        from state.plan_writers import write_accommodation
        write_accommodation(plan, area="新宿", hotel="Hyatt")
        assert plan.accommodation.area == "新宿"
        assert plan.accommodation.hotel == "Hyatt"

    def test_sets_area_only(self, plan):
        from state.plan_writers import write_accommodation
        write_accommodation(plan, area="银座")
        assert plan.accommodation.area == "银座"
        assert plan.accommodation.hotel is None


class TestWriteRisks:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_risks
        write_risks(plan, [{"type": "weather", "desc": "台风"}])
        assert len(plan.risks) == 1


class TestWriteAlternatives:
    def test_replaces_wholesale(self, plan):
        from state.plan_writers import write_alternatives
        write_alternatives(plan, [{"name": "备选A"}])
        assert len(plan.alternatives) == 1


class TestWriteTripBrief:
    def test_merges_into_existing(self, plan):
        from state.plan_writers import write_trip_brief
        plan.trip_brief = {"goal": "old"}
        write_trip_brief(plan, {"pace": "relaxed"})
        assert plan.trip_brief == {"goal": "old", "pace": "relaxed"}

    def test_overwrites_key(self, plan):
        from state.plan_writers import write_trip_brief
        plan.trip_brief = {"goal": "old"}
        write_trip_brief(plan, {"goal": "new"})
        assert plan.trip_brief["goal"] == "new"


# --- Category A: daily plans ---

class TestAppendOneDayPlan:
    def test_appends_day(self, plan):
        from state.plan_writers import append_one_day_plan
        append_one_day_plan(plan, {
            "day": 1,
            "date": "2026-05-01",
            "activities": [],
        })
        assert len(plan.daily_plans) == 1
        assert plan.daily_plans[0].day == 1

    def test_asserts_on_non_dict(self, plan):
        from state.plan_writers import append_one_day_plan
        with pytest.raises(AssertionError):
            append_one_day_plan(plan, "day 1")


class TestReplaceAllDailyPlans:
    def test_replaces_all(self, plan):
        from state.plan_writers import replace_all_daily_plans, append_one_day_plan
        append_one_day_plan(plan, {"day": 1, "date": "2026-05-01", "activities": []})
        replace_all_daily_plans(plan, [
            {"day": 1, "date": "2026-05-01", "activities": []},
            {"day": 2, "date": "2026-05-02", "activities": []},
        ])
        assert len(plan.daily_plans) == 2


# --- Category B: phrase-tolerant ---

class TestWriteDestination:
    def test_string(self, plan):
        from state.plan_writers import write_destination
        write_destination(plan, "东京")
        assert plan.destination == "东京"

    def test_dict_with_name(self, plan):
        from state.plan_writers import write_destination
        write_destination(plan, {"name": "京都", "country": "日本"})
        assert plan.destination == "京都"


class TestWriteDates:
    def test_structured(self, plan):
        from state.plan_writers import write_dates
        write_dates(plan, {"start": "2026-05-01", "end": "2026-05-05"})
        assert plan.dates is not None
        assert plan.dates.total_days == 4


class TestWriteTravelers:
    def test_structured(self, plan):
        from state.plan_writers import write_travelers
        write_travelers(plan, {"adults": 2, "children": 1})
        assert plan.travelers.adults == 2
        assert plan.travelers.children == 1


class TestWriteBudget:
    def test_structured(self, plan):
        from state.plan_writers import write_budget
        write_budget(plan, {"total": 15000, "currency": "CNY"})
        assert plan.budget.total == 15000


class TestWriteDepartureCity:
    def test_writes_into_trip_brief(self, plan):
        from state.plan_writers import write_departure_city
        write_departure_city(plan, "上海")
        assert plan.trip_brief["departure_city"] == "上海"

    def test_extracts_city_from_object(self, plan):
        from state.plan_writers import write_departure_city
        write_departure_city(plan, {"city": "杭州"})
        assert plan.trip_brief["departure_city"] == "杭州"


# --- Category C: append ---

class TestAppendPreferences:
    def test_append_string_items(self, plan):
        from state.plan_writers import append_preferences
        append_preferences(plan, ["美食", "自然风光"])
        assert len(plan.preferences) == 2
        assert plan.preferences[0].key == "美食"

    def test_append_dict_items(self, plan):
        from state.plan_writers import append_preferences
        append_preferences(plan, [{"key": "cuisine", "value": "日料"}])
        assert len(plan.preferences) == 1
        assert plan.preferences[0].key == "cuisine"


class TestAppendConstraints:
    def test_append_dict_items(self, plan):
        from state.plan_writers import append_constraints
        append_constraints(plan, [
            {"type": "hard", "description": "不坐红眼航班"},
        ])
        assert len(plan.constraints) == 1
        assert plan.constraints[0].type == "hard"


class TestAppendDestinationCandidate:
    def test_appends_one(self, plan):
        from state.plan_writers import append_destination_candidate
        append_destination_candidate(plan, {"name": "东京", "score": 0.9})
        assert len(plan.destination_candidates) == 1


class TestReplaceDestinationCandidates:
    def test_replaces_all(self, plan):
        from state.plan_writers import replace_destination_candidates
        plan.destination_candidates = [{"name": "old"}]
        replace_destination_candidates(plan, [{"name": "A"}, {"name": "B"}])
        assert len(plan.destination_candidates) == 2


# --- Category D: backtrack ---

class TestExecuteBacktrack:
    def test_backtrack_from_3_to_1(self, plan):
        from state.plan_writers import execute_backtrack
        plan.phase = 3
        plan.destination = "东京"
        result = execute_backtrack(plan, to_phase=1, reason="换目的地")
        assert result["backtracked"] is True
        assert result["from_phase"] == 3
        assert result["to_phase"] == 1
        assert plan.phase == 1
        assert plan.destination is None

    def test_backtrack_to_same_phase_raises(self, plan):
        from state.plan_writers import execute_backtrack
        plan.phase = 3
        with pytest.raises(ValueError, match="只能回退到更早的阶段"):
            execute_backtrack(plan, to_phase=3, reason="test")

    def test_backtrack_phase2_normalizes_to_1(self, plan):
        from state.plan_writers import execute_backtrack
        plan.phase = 3
        result = execute_backtrack(plan, to_phase=2, reason="test")
        assert result["to_phase"] == 1
```

- [ ] **Step 4: Run tests**

```bash
cd backend && python -m pytest tests/test_plan_writers.py -v
```

Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/state/plan_writers.py backend/tests/test_plan_writers.py
git commit -m "feat(state): add plan_writers thin write layer + tests

- One pure function per write operation (19 functions total)
- Defensive assertions for internal-contract checks
- Both new tools and transitional adapter will call this layer
- Full unit test coverage for all categories (A/B/C/D)

Part of: split update_plan_state into single-responsibility tools (Task 1/15)"
```

---

### Task 2: Create `tools/plan_tools/phase3_tools.py` — Category A Phase 3 tools (Part 1)

**Files:**
- Create: `backend/tools/plan_tools/__init__.py` (empty, makes it a package)
- Create: `backend/tools/plan_tools/phase3_tools.py`

This file contains all 11 Category A Phase 3 tools. It is large but logically cohesive — all tools deal with Phase 3 state writing with strong-typed schemas.

- [ ] **Step 1: Create `backend/tools/plan_tools/` directory and `__init__.py`**

```bash
mkdir -p backend/tools/plan_tools
touch backend/tools/plan_tools/__init__.py
```

The `__init__.py` will be populated in Task 10 with the `make_all_plan_tools()` aggregator. For now it's empty to make the directory a Python package.

- [ ] **Step 2: Create `backend/tools/plan_tools/phase3_tools.py`**

```python
# backend/tools/plan_tools/phase3_tools.py
"""Category A: high-risk strong-schema Phase 3 tools.

These tools receive structured list[dict] or nested dict. Their JSON Schemas
forbid strings; this is where stringification is eradicated.
"""
from __future__ import annotations

from typing import Any

from state.models import Accommodation, TravelPlanState
from state.plan_writers import (
    write_accommodation,
    write_accommodation_options,
    write_alternatives,
    write_candidate_pool,
    write_risks,
    write_selected_skeleton_id,
    write_selected_transport,
    write_shortlist,
    write_skeleton_plans,
    write_transport_options,
    write_trip_brief,
)
from tools.base import ToolError, tool


# ---------------------------------------------------------------------------
# set_skeleton_plans
# ---------------------------------------------------------------------------

_SET_SKELETON_PLANS_PARAMS = {
    "type": "object",
    "properties": {
        "plans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "days": {"type": "array", "items": {"type": "object"}},
                    "tradeoffs": {"type": "object"},
                },
                "required": ["id", "name"],
            },
            "description": "骨架方案列表，每个方案包含 id, name, days, tradeoffs",
        },
    },
    "required": ["plans"],
}


def make_set_skeleton_plans_tool(plan: TravelPlanState):
    @tool(
        name="set_skeleton_plans",
        description="写入骨架方案列表（整体替换）。每个方案必须包含 id 和 name。",
        phases=[3],
        parameters=_SET_SKELETON_PLANS_PARAMS,
        side_effect="write",
        human_label="写入骨架方案",
    )
    async def set_skeleton_plans(plans: list) -> dict:
        if not isinstance(plans, list):
            raise ToolError(
                f"plans 必须是 list，收到 {type(plans).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, p in enumerate(plans):
            if not isinstance(p, dict):
                raise ToolError(
                    f"plans[{i}] 必须是 dict，收到 {type(p).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个骨架方案必须是 JSON 对象",
                )
            if "id" not in p:
                raise ToolError(
                    f"plans[{i}] 缺少必填字段 'id'",
                    error_code="INVALID_VALUE",
                    suggestion='每个骨架必须有 id 字段，如 {"id": "plan_a", "name": "轻松版", ...}',
                )
        prev_count = len(plan.skeleton_plans)
        write_skeleton_plans(plan, plans)
        return {"updated_field": "skeleton_plans", "count": len(plans), "previous_count": prev_count}

    return set_skeleton_plans


# ---------------------------------------------------------------------------
# select_skeleton
# ---------------------------------------------------------------------------

_SELECT_SKELETON_PARAMS = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "要锁定的骨架方案 ID（必须匹配 skeleton_plans 中某项的 id）",
        },
    },
    "required": ["id"],
}


def make_select_skeleton_tool(plan: TravelPlanState):
    @tool(
        name="select_skeleton",
        description="锁定一套骨架方案。id 必须匹配已写入 skeleton_plans 中的某个方案 id。",
        phases=[3],
        parameters=_SELECT_SKELETON_PARAMS,
        side_effect="write",
        human_label="锁定骨架方案",
    )
    async def select_skeleton(id: str) -> dict:
        if not isinstance(id, str) or not id.strip():
            raise ToolError(
                "id 必须是非空字符串",
                error_code="INVALID_VALUE",
                suggestion="请传入骨架方案的 id 字段值",
            )
        existing_ids = [s.get("id") for s in plan.skeleton_plans if isinstance(s, dict)]
        if id not in existing_ids:
            raise ToolError(
                f"未找到 id={id!r} 的骨架方案",
                error_code="INVALID_VALUE",
                suggestion=f"可选 id: {', '.join(existing_ids) if existing_ids else '(无已写入骨架)'}",
            )
        prev = plan.selected_skeleton_id
        write_selected_skeleton_id(plan, id)
        return {"updated_field": "selected_skeleton_id", "new_value": id, "previous_value": prev}

    return select_skeleton


# ---------------------------------------------------------------------------
# set_candidate_pool
# ---------------------------------------------------------------------------

_SET_CANDIDATE_POOL_PARAMS = {
    "type": "object",
    "properties": {
        "pool": {
            "type": "array",
            "items": {"type": "object"},
            "description": "候选池列表",
        },
    },
    "required": ["pool"],
}


def make_set_candidate_pool_tool(plan: TravelPlanState):
    @tool(
        name="set_candidate_pool",
        description="写入候选池（整体替换）。每个候选项必须是 JSON 对象。",
        phases=[3],
        parameters=_SET_CANDIDATE_POOL_PARAMS,
        side_effect="write",
        human_label="写入候选池",
    )
    async def set_candidate_pool(pool: list) -> dict:
        if not isinstance(pool, list):
            raise ToolError(
                f"pool 必须是 list，收到 {type(pool).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(pool):
            if not isinstance(item, dict):
                raise ToolError(
                    f"pool[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个候选项必须是 JSON 对象",
                )
        prev_count = len(plan.candidate_pool)
        write_candidate_pool(plan, pool)
        return {"updated_field": "candidate_pool", "count": len(pool), "previous_count": prev_count}

    return set_candidate_pool


# ---------------------------------------------------------------------------
# set_shortlist
# ---------------------------------------------------------------------------

_SET_SHORTLIST_PARAMS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object"},
            "description": "候选短名单",
        },
    },
    "required": ["items"],
}


def make_set_shortlist_tool(plan: TravelPlanState):
    @tool(
        name="set_shortlist",
        description="写入候选短名单（整体替换）。",
        phases=[3],
        parameters=_SET_SHORTLIST_PARAMS,
        side_effect="write",
        human_label="写入候选短名单",
    )
    async def set_shortlist(items: list) -> dict:
        if not isinstance(items, list):
            raise ToolError(
                f"items 必须是 list，收到 {type(items).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ToolError(
                    f"items[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个短名单项必须是 JSON 对象",
                )
        prev_count = len(plan.shortlist)
        write_shortlist(plan, items)
        return {"updated_field": "shortlist", "count": len(items), "previous_count": prev_count}

    return set_shortlist


# ---------------------------------------------------------------------------
# set_transport_options
# ---------------------------------------------------------------------------

_SET_TRANSPORT_OPTIONS_PARAMS = {
    "type": "object",
    "properties": {
        "options": {
            "type": "array",
            "items": {"type": "object"},
            "description": "交通候选列表",
        },
    },
    "required": ["options"],
}


def make_set_transport_options_tool(plan: TravelPlanState):
    @tool(
        name="set_transport_options",
        description="写入交通候选列表（整体替换）。",
        phases=[3],
        parameters=_SET_TRANSPORT_OPTIONS_PARAMS,
        side_effect="write",
        human_label="写入交通候选",
    )
    async def set_transport_options(options: list) -> dict:
        if not isinstance(options, list):
            raise ToolError(
                f"options 必须是 list，收到 {type(options).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(options):
            if not isinstance(item, dict):
                raise ToolError(
                    f"options[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个交通选项必须是 JSON 对象",
                )
        prev_count = len(plan.transport_options)
        write_transport_options(plan, options)
        return {"updated_field": "transport_options", "count": len(options), "previous_count": prev_count}

    return set_transport_options


# ---------------------------------------------------------------------------
# select_transport
# ---------------------------------------------------------------------------

_SELECT_TRANSPORT_PARAMS = {
    "type": "object",
    "properties": {
        "choice": {
            "type": "object",
            "description": "选中的交通方案",
        },
    },
    "required": ["choice"],
}


def make_select_transport_tool(plan: TravelPlanState):
    @tool(
        name="select_transport",
        description="锁定交通方案。传入选中的交通对象。",
        phases=[3],
        parameters=_SELECT_TRANSPORT_PARAMS,
        side_effect="write",
        human_label="锁定交通方案",
    )
    async def select_transport(choice: dict) -> dict:
        if not isinstance(choice, dict):
            raise ToolError(
                f"choice 必须是 dict，收到 {type(choice).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 JSON 对象",
            )
        prev = plan.selected_transport
        write_selected_transport(plan, choice)
        return {"updated_field": "selected_transport", "new_value": choice, "previous_value": prev}

    return select_transport


# ---------------------------------------------------------------------------
# set_accommodation_options
# ---------------------------------------------------------------------------

_SET_ACCOMMODATION_OPTIONS_PARAMS = {
    "type": "object",
    "properties": {
        "options": {
            "type": "array",
            "items": {"type": "object"},
            "description": "住宿候选列表",
        },
    },
    "required": ["options"],
}


def make_set_accommodation_options_tool(plan: TravelPlanState):
    @tool(
        name="set_accommodation_options",
        description="写入住宿候选列表（整体替换）。",
        phases=[3],
        parameters=_SET_ACCOMMODATION_OPTIONS_PARAMS,
        side_effect="write",
        human_label="写入住宿候选",
    )
    async def set_accommodation_options(options: list) -> dict:
        if not isinstance(options, list):
            raise ToolError(
                f"options 必须是 list，收到 {type(options).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(options):
            if not isinstance(item, dict):
                raise ToolError(
                    f"options[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个住宿选项必须是 JSON 对象",
                )
        prev_count = len(plan.accommodation_options)
        write_accommodation_options(plan, options)
        return {"updated_field": "accommodation_options", "count": len(options), "previous_count": prev_count}

    return set_accommodation_options


# ---------------------------------------------------------------------------
# set_accommodation
# ---------------------------------------------------------------------------

_SET_ACCOMMODATION_PARAMS = {
    "type": "object",
    "properties": {
        "area": {
            "type": "string",
            "description": "住宿区域/地址",
        },
        "hotel": {
            "type": "string",
            "description": "酒店名称（可选）",
        },
    },
    "required": ["area"],
}


def make_set_accommodation_tool(plan: TravelPlanState):
    @tool(
        name="set_accommodation",
        description="锁定住宿区域和酒店。",
        phases=[3, 5],
        parameters=_SET_ACCOMMODATION_PARAMS,
        side_effect="write",
        human_label="锁定住宿",
    )
    async def set_accommodation(area: str, hotel: str | None = None) -> dict:
        if not isinstance(area, str) or not area.strip():
            raise ToolError(
                "area 必须是非空字符串",
                error_code="INVALID_VALUE",
                suggestion='示例: "新宿"',
            )
        prev = plan.accommodation.to_dict() if plan.accommodation else None
        write_accommodation(plan, area=area.strip(), hotel=hotel)
        return {"updated_field": "accommodation", "new_value": plan.accommodation.to_dict(), "previous_value": prev}

    return set_accommodation


# ---------------------------------------------------------------------------
# set_risks
# ---------------------------------------------------------------------------

_SET_RISKS_PARAMS = {
    "type": "object",
    "properties": {
        "list": {
            "type": "array",
            "items": {"type": "object"},
            "description": "风险点列表",
        },
    },
    "required": ["list"],
}


def make_set_risks_tool(plan: TravelPlanState):
    @tool(
        name="set_risks",
        description="写入风险点列表（整体替换）。",
        phases=[3, 5],
        parameters=_SET_RISKS_PARAMS,
        side_effect="write",
        human_label="写入风险点",
    )
    async def set_risks(list: list) -> dict:
        items = list  # alias to avoid shadowed builtin
        if not isinstance(items, type([])):
            raise ToolError(
                f"list 必须是 list，收到 {type(items).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ToolError(
                    f"list[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个风险点必须是 JSON 对象",
                )
        prev_count = len(plan.risks)
        write_risks(plan, items)
        return {"updated_field": "risks", "count": len(items), "previous_count": prev_count}

    return set_risks


# ---------------------------------------------------------------------------
# set_alternatives
# ---------------------------------------------------------------------------

_SET_ALTERNATIVES_PARAMS = {
    "type": "object",
    "properties": {
        "list": {
            "type": "array",
            "items": {"type": "object"},
            "description": "备选方案列表",
        },
    },
    "required": ["list"],
}


def make_set_alternatives_tool(plan: TravelPlanState):
    @tool(
        name="set_alternatives",
        description="写入备选方案列表（整体替换）。",
        phases=[3, 5],
        parameters=_SET_ALTERNATIVES_PARAMS,
        side_effect="write",
        human_label="写入备选方案",
    )
    async def set_alternatives(list: list) -> dict:
        items = list
        if not isinstance(items, type([])):
            raise ToolError(
                f"list 必须是 list，收到 {type(items).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ToolError(
                    f"list[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个备选方案必须是 JSON 对象",
                )
        prev_count = len(plan.alternatives)
        write_alternatives(plan, items)
        return {"updated_field": "alternatives", "count": len(items), "previous_count": prev_count}

    return set_alternatives


# ---------------------------------------------------------------------------
# set_trip_brief
# ---------------------------------------------------------------------------

_SET_TRIP_BRIEF_PARAMS = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "object",
            "description": "旅行画像字段，增量合并到 trip_brief 中",
        },
    },
    "required": ["fields"],
}


def make_set_trip_brief_tool(plan: TravelPlanState):
    @tool(
        name="set_trip_brief",
        description="更新旅行画像（增量合并到现有 trip_brief）。",
        phases=[3],
        parameters=_SET_TRIP_BRIEF_PARAMS,
        side_effect="write",
        human_label="更新旅行画像",
    )
    async def set_trip_brief(fields: dict) -> dict:
        if not isinstance(fields, dict):
            raise ToolError(
                f"fields 必须是 dict，收到 {type(fields).__name__}",
                error_code="INVALID_VALUE",
                suggestion='示例: {"goal": "慢旅行", "pace": "relaxed"}',
            )
        prev = dict(plan.trip_brief)
        write_trip_brief(plan, fields)
        return {"updated_field": "trip_brief", "new_value": plan.trip_brief, "previous_value": prev}

    return set_trip_brief
```

> **Note on `set_risks` / `set_alternatives`:** The spec names the parameter `list`, which shadows the Python builtin. The type guard uses `type([])` to avoid the shadowing issue. An alternative would be renaming the parameter to `items`, but we match the spec's schema exactly for LLM compatibility.

- [ ] **Step 3: Run import check**

```bash
cd backend && python -c "
from tools.plan_tools.phase3_tools import (
    make_set_skeleton_plans_tool,
    make_select_skeleton_tool,
    make_set_candidate_pool_tool,
    make_set_shortlist_tool,
    make_set_transport_options_tool,
    make_select_transport_tool,
    make_set_accommodation_options_tool,
    make_set_accommodation_tool,
    make_set_risks_tool,
    make_set_alternatives_tool,
    make_set_trip_brief_tool,
)
print('OK: 11 tools importable')
"
```

Expected: `OK: 11 tools importable`

- [ ] **Step 4: Commit**

```bash
git add backend/tools/plan_tools/__init__.py backend/tools/plan_tools/phase3_tools.py
git commit -m "feat(plan-tools): add 11 Category A Phase 3 tools

- set_skeleton_plans, select_skeleton, set_candidate_pool, set_shortlist,
  set_transport_options, select_transport, set_accommodation_options,
  set_accommodation, set_risks, set_alternatives, set_trip_brief
- Strong-typed JSON Schemas forbid string payloads
- L2 runtime type guards with ToolError for LLM self-correction
- L3 delegation to state/plan_writers for actual mutations

Part of: split update_plan_state into single-responsibility tools (Task 2/15)"
```

---

### Task 3: Create `tests/test_plan_tools/__init__.py` and `test_phase3_tools.py`

**Files:**
- Create: `backend/tests/test_plan_tools/__init__.py`
- Create: `backend/tests/test_plan_tools/test_phase3_tools.py`

- [ ] **Step 1: Create test directory**

```bash
mkdir -p backend/tests/test_plan_tools
touch backend/tests/test_plan_tools/__init__.py
```

- [ ] **Step 2: Create `backend/tests/test_plan_tools/test_phase3_tools.py`**

```python
# backend/tests/test_plan_tools/test_phase3_tools.py
"""Unit tests for Category A Phase 3 tools."""
from __future__ import annotations

import pytest

from state.models import TravelPlanState
from tools.base import ToolError
from tools.plan_tools.phase3_tools import (
    make_select_skeleton_tool,
    make_select_transport_tool,
    make_set_accommodation_options_tool,
    make_set_accommodation_tool,
    make_set_alternatives_tool,
    make_set_candidate_pool_tool,
    make_set_risks_tool,
    make_set_shortlist_tool,
    make_set_skeleton_plans_tool,
    make_set_transport_options_tool,
    make_set_trip_brief_tool,
)


@pytest.fixture
def plan():
    return TravelPlanState(session_id="phase3-test")


# --- set_skeleton_plans ---

class TestSetSkeletonPlans:
    @pytest.fixture
    def tool_fn(self, plan):
        return make_set_skeleton_plans_tool(plan)

    @pytest.mark.asyncio
    async def test_valid_plans(self, tool_fn, plan):
        result = await tool_fn(plans=[
            {"id": "a", "name": "轻松版", "days": [], "tradeoffs": {}},
            {"id": "b", "name": "平衡版", "days": [], "tradeoffs": {}},
        ])
        assert result["updated_field"] == "skeleton_plans"
        assert result["count"] == 2
        assert len(plan.skeleton_plans) == 2

    @pytest.mark.asyncio
    async def test_rejects_string(self, tool_fn):
        with pytest.raises(ToolError) as exc:
            await tool_fn(plans="not a list")
        assert exc.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_rejects_non_dict_elements(self, tool_fn):
        with pytest.raises(ToolError) as exc:
            await tool_fn(plans=["plan_a", "plan_b"])
        assert exc.value.error_code == "INVALID_VALUE"
        assert "[0]" in str(exc.value)

    @pytest.mark.asyncio
    async def test_rejects_missing_id(self, tool_fn):
        with pytest.raises(ToolError) as exc:
            await tool_fn(plans=[{"name": "no id"}])
        assert exc.value.error_code == "INVALID_VALUE"
        assert "id" in str(exc.value)

    @pytest.mark.asyncio
    async def test_has_write_side_effect(self, tool_fn):
        assert tool_fn.side_effect == "write"

    @pytest.mark.asyncio
    async def test_has_human_label(self, tool_fn):
        assert tool_fn.human_label == "写入骨架方案"


# --- select_skeleton ---

class TestSelectSkeleton:
    @pytest.fixture
    def tool_fn(self, plan):
        plan.skeleton_plans = [
            {"id": "plan_a", "name": "轻松版"},
            {"id": "plan_b", "name": "平衡版"},
        ]
        return make_select_skeleton_tool(plan)

    @pytest.mark.asyncio
    async def test_valid_selection(self, tool_fn, plan):
        result = await tool_fn(id="plan_a")
        assert result["new_value"] == "plan_a"
        assert plan.selected_skeleton_id == "plan_a"

    @pytest.mark.asyncio
    async def test_rejects_unknown_id(self, tool_fn):
        with pytest.raises(ToolError) as exc:
            await tool_fn(id="nonexistent")
        assert exc.value.error_code == "INVALID_VALUE"
        assert "plan_a" in exc.value.suggestion

    @pytest.mark.asyncio
    async def test_rejects_empty_id(self, tool_fn):
        with pytest.raises(ToolError) as exc:
            await tool_fn(id="")
        assert exc.value.error_code == "INVALID_VALUE"


# --- set_candidate_pool ---

class TestSetCandidatePool:
    @pytest.fixture
    def tool_fn(self, plan):
        return make_set_candidate_pool_tool(plan)

    @pytest.mark.asyncio
    async def test_valid_pool(self, tool_fn, plan):
        result = await tool_fn(pool=[{"name": "A"}, {"name": "B"}])
        assert result["count"] == 2
        assert len(plan.candidate_pool) == 2

    @pytest.mark.asyncio
    async def test_rejects_string(self, tool_fn):
        with pytest.raises(ToolError):
            await tool_fn(pool="not a list")

    @pytest.mark.asyncio
    async def test_rejects_non_dict_element(self, tool_fn):
        with pytest.raises(ToolError):
            await tool_fn(pool=[1, 2, 3])


# --- set_shortlist ---

class TestSetShortlist:
    @pytest.fixture
    def tool_fn(self, plan):
        return make_set_shortlist_tool(plan)

    @pytest.mark.asyncio
    async def test_valid_items(self, tool_fn, plan):
        result = await tool_fn(items=[{"name": "A"}])
        assert result["count"] == 1
        assert len(plan.shortlist) == 1

    @pytest.mark.asyncio
    async def test_rejects_string(self, tool_fn):
        with pytest.raises(ToolError):
            await tool_fn(items="not a list")


# --- set_transport_options ---

class TestSetTransportOptions:
    @pytest.fixture
    def tool_fn(self, plan):
        return make_set_transport_options_tool(plan)

    @pytest.mark.asyncio
    async def test_valid_options(self, tool_fn, plan):
        result = await tool_fn(options=[{"type": "flight"}, {"type": "train"}])
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_rejects_string(self, tool_fn):
        with pytest.raises(ToolError):
            await tool_fn(options="flight")


# --- select_transport ---

class TestSelectTransport:
    @pytest.fixture
    def tool_fn(self, plan):
        return make_select_transport_tool(plan)

    @pytest.mark.asyncio
    async def test_valid_selection(self, tool_fn, plan):
        result = await tool_fn(choice={"type": "flight", "price": 1200})
        assert plan.selected_transport["type"] == "flight"

    @pytest.mark.asyncio
    async def test_rejects_string(self, tool_fn):
        with pytest.raises(ToolError):
            await tool_fn(choice="flight")


# --- set_accommodation_options ---

class TestSetAccommodationOptions:
    @pytest.fixture
    def tool_fn(self, plan):
        return make_set_accommodation_options_tool(plan)

    @pytest.mark.asyncio
    async def test_valid_options(self, tool_fn, plan):
        result = await tool_fn(options=[{"name": "Hotel A"}])
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_rejects_string(self, tool_fn):
        with pytest.raises(ToolError):
            await tool_fn(options="hotel")


# --- set_accommodation ---

class TestSetAccommodation:
    @pytest.fixture
    def tool_fn(self, plan):
        return make_set_accommodation_tool(plan)

    @pytest.mark.asyncio
    async def test_valid_area_and_hotel(self, tool_fn, plan):
        result = await tool_fn(area="新宿", hotel="Hyatt Regency")
        assert plan.accommodation.area == "新宿"
        assert plan.accommodation.hotel == "Hyatt Regency"

    @pytest.mark.asyncio
    async def test_valid_area_only(self, tool_fn, plan):
        await tool_fn(area="银座")
        assert plan.accommodation.area == "银座"
        assert plan.accommodation.hotel is None

    @pytest.mark.asyncio
    async def test_rejects_empty_area(self, tool_fn):
        with pytest.raises(ToolError):
            await tool_fn(area="")


# --- set_risks ---

class TestSetRisks:
    @pytest.fixture
    def tool_fn(self, plan):
        return make_set_risks_tool(plan)

    @pytest.mark.asyncio
    async def test_valid_risks(self, tool_fn, plan):
        result = await tool_fn(list=[{"type": "weather", "desc": "台风"}])
        assert result["count"] == 1
        assert len(plan.risks) == 1

    @pytest.mark.asyncio
    async def test_rejects_non_dict_element(self, tool_fn):
        with pytest.raises(ToolError):
            await tool_fn(list=["typhoon"])


# --- set_alternatives ---

class TestSetAlternatives:
    @pytest.fixture
    def tool_fn(self, plan):
        return make_set_alternatives_tool(plan)

    @pytest.mark.asyncio
    async def test_valid_alternatives(self, tool_fn, plan):
        result = await tool_fn(list=[{"name": "备选A"}, {"name": "备选B"}])
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_rejects_non_dict_element(self, tool_fn):
        with pytest.raises(ToolError):
            await tool_fn(list=["alt_a"])


# --- set_trip_brief ---

class TestSetTripBrief:
    @pytest.fixture
    def tool_fn(self, plan):
        return make_set_trip_brief_tool(plan)

    @pytest.mark.asyncio
    async def test_valid_fields(self, tool_fn, plan):
        result = await tool_fn(fields={"goal": "慢旅行", "pace": "relaxed"})
        assert plan.trip_brief["goal"] == "慢旅行"
        assert plan.trip_brief["pace"] == "relaxed"

    @pytest.mark.asyncio
    async def test_merges_with_existing(self, tool_fn, plan):
        plan.trip_brief = {"goal": "old"}
        await tool_fn(fields={"pace": "relaxed"})
        assert plan.trip_brief == {"goal": "old", "pace": "relaxed"}

    @pytest.mark.asyncio
    async def test_rejects_string(self, tool_fn):
        with pytest.raises(ToolError):
            await tool_fn(fields="not a dict")
```

- [ ] **Step 3: Run tests**

```bash
cd backend && python -m pytest tests/test_plan_tools/test_phase3_tools.py -v
```

Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_plan_tools/__init__.py backend/tests/test_plan_tools/test_phase3_tools.py
git commit -m "test(plan-tools): add unit tests for 11 Category A Phase 3 tools

- Tests for schema shape, type guards, successful writes,
  human_label presence, side_effect declaration
- Covers: set_skeleton_plans, select_skeleton, set_candidate_pool,
  set_shortlist, set_transport_options, select_transport,
  set_accommodation_options, set_accommodation, set_risks,
  set_alternatives, set_trip_brief

Part of: split update_plan_state into single-responsibility tools (Task 3/15)"
```

---

### Task 4: Extend `backend/tests/test_plan_writers.py` — L4 reader defense

**Files:**
- Modify: `backend/state/models.py:333-367` (infer_phase3_step_from_state)

This task adds the L4 reader-side defense: `infer_phase3_step_from_state` should filter non-dict elements before `.get()` to handle any contaminated data gracefully.

- [ ] **Step 1: Write a failing test**

Add to `backend/tests/test_plan_writers.py` (append at end):

```python
# --- L4: reader-side defense ---

class TestInferPhase3StepRobustness:
    """infer_phase3_step_from_state must handle non-dict skeleton elements."""

    def test_filters_string_elements(self):
        from state.models import infer_phase3_step_from_state, DateRange
        result = infer_phase3_step_from_state(
            phase=3,
            dates=DateRange(start="2026-05-01", end="2026-05-05"),
            trip_brief={"goal": "test"},
            candidate_pool=[{"name": "A"}],
            shortlist=[{"name": "A"}],
            skeleton_plans=["corrupted_string", {"id": "plan_a", "name": "ok"}],
            selected_skeleton_id="plan_a",
            accommodation=None,
        )
        assert result == "lock"

    def test_filters_int_elements(self):
        from state.models import infer_phase3_step_from_state, DateRange
        result = infer_phase3_step_from_state(
            phase=3,
            dates=DateRange(start="2026-05-01", end="2026-05-05"),
            trip_brief={"goal": "test"},
            candidate_pool=None,
            shortlist=None,
            skeleton_plans=[42, {"id": "plan_b", "name": "ok"}],
            selected_skeleton_id="plan_b",
            accommodation=None,
        )
        assert result == "lock"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && python -m pytest tests/test_plan_writers.py::TestInferPhase3StepRobustness -v
```

Expected: FAIL with `AttributeError: 'str' object has no attribute 'get'` (this is the exact production incident)

- [ ] **Step 3: Fix `infer_phase3_step_from_state`**

In `backend/state/models.py`, replace lines 357-363:

Old:
```python
    if skeleton_plans:
        matched = any(
            s.get("id") == selected_skeleton_id or s.get("name") == selected_skeleton_id
            for s in skeleton_plans
        )
```

New:
```python
    if skeleton_plans:
        matched = any(
            s.get("id") == selected_skeleton_id or s.get("name") == selected_skeleton_id
            for s in skeleton_plans
            if isinstance(s, dict)
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && python -m pytest tests/test_plan_writers.py::TestInferPhase3StepRobustness -v
```

Expected: All PASS

- [ ] **Step 5: Run full plan_writers + existing test suite**

```bash
cd backend && python -m pytest tests/test_plan_writers.py tests/test_update_plan_state.py -v
```

Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add backend/state/models.py backend/tests/test_plan_writers.py
git commit -m "fix(state): add L4 reader defense to infer_phase3_step_from_state

- Filter non-dict elements before .get() in skeleton_plans iteration
- Prevents the exact production incident: AttributeError on corrupted string
- Test coverage for string and int contaminated elements

Part of: split update_plan_state into single-responsibility tools (Task 4/15)"
```

---

### Task 5: Verify full test suite passes (Step 1 integration gate)

**Files:**
- No new files; verification only

This is the Step 1 integration gate. All new code is additive — no runtime behavior has changed.

- [ ] **Step 1: Run ALL new tests together**

```bash
cd backend && python -m pytest tests/test_plan_writers.py tests/test_plan_tools/ -v
```

Expected: All PASS (plan_writers + phase3_tools tests)

- [ ] **Step 2: Run the existing test suite to verify no regressions**

```bash
cd backend && python -m pytest tests/ -q
```

Expected: All existing tests pass unchanged (Step 1 is line-only addition, zero runtime change).

- [ ] **Step 3: Commit tag for Step 1 completion**

```bash
git tag -a step1-scaffold-complete -m "Step 1 scaffold complete: plan_writers + phase3_tools + tests
No runtime changes. New tools not registered in ToolEngine."
```

---

### Task 6: Create daily_plans tools (`tools/plan_tools/daily_plans.py` + tests)

**Files:**
- Create: `backend/tools/plan_tools/daily_plans.py`
- Create: `backend/tests/test_plan_tools/test_daily_plans.py`

- [ ] **Step 1: Create `backend/tools/plan_tools/daily_plans.py`**

```python
# backend/tools/plan_tools/daily_plans.py
from __future__ import annotations

import re
from typing import Any

from state.models import TravelPlanState
from state.plan_writers import append_one_day_plan, replace_all_daily_plans
from tools.base import ToolError, tool

_REQUIRED_ACTIVITY_FIELDS = {"name", "location", "start_time", "end_time", "category", "cost"}

_APPEND_DAY_PLAN_PARAMETERS = {
    "type": "object",
    "properties": {
        "day": {"type": "integer", "description": "第几天（从1开始）"},
        "date": {"type": "string", "description": "日期，格式 YYYY-MM-DD"},
        "activities": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "活动列表，每个必须包含 name, location({name,lat,lng}), "
                "start_time(HH:MM), end_time(HH:MM), category, cost(number)"
            ),
        },
    },
    "required": ["day", "date", "activities"],
}

_REPLACE_DAILY_PLANS_PARAMETERS = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "integer"},
                    "date": {"type": "string", "description": "格式 YYYY-MM-DD"},
                    "activities": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["day", "date", "activities"],
            },
            "description": "完整的逐日行程列表，每天包含 day, date, activities",
        },
    },
    "required": ["days"],
}


def _validate_date_format(date: str) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ToolError(
            f"date 格式错误: {date!r}，需要 YYYY-MM-DD",
            error_code="INVALID_VALUE",
            suggestion='示例: "2026-05-01"',
        )


def _validate_activities(activities: Any) -> None:
    if not isinstance(activities, list):
        raise ToolError(
            f"activities 必须是 list，收到 {type(activities).__name__}",
            error_code="INVALID_VALUE",
            suggestion="activities 应为 list[dict]，每个 dict 包含 name, location, start_time, end_time, category, cost",
        )
    for i, act in enumerate(activities):
        if not isinstance(act, dict):
            raise ToolError(
                f"activities[{i}] 必须是 dict，收到 {type(act).__name__}",
                error_code="INVALID_VALUE",
                suggestion="每个 activity 必须是 dict",
            )
        missing = _REQUIRED_ACTIVITY_FIELDS - set(act.keys())
        if missing:
            raise ToolError(
                f"activities[{i}] 缺少必填字段: {', '.join(sorted(missing))}",
                error_code="INVALID_VALUE",
                suggestion=f"每个 activity 必须包含: {', '.join(sorted(_REQUIRED_ACTIVITY_FIELDS))}",
            )


def make_append_day_plan_tool(plan: TravelPlanState):
    @tool(
        name="append_day_plan",
        description="追加一天的行程计划。传入天数编号、日期和活动列表。",
        phases=[5],
        parameters=_APPEND_DAY_PLAN_PARAMETERS,
        side_effect="write",
        human_label="追加一天行程",
    )
    async def append_day_plan(day: int, date: str, activities: list) -> dict:
        if not isinstance(day, int):
            raise ToolError(
                f"day 必须是整数，收到 {type(day).__name__}",
                error_code="INVALID_VALUE",
                suggestion="day 应为正整数，如 1、2、3",
            )
        _validate_date_format(date)
        _validate_activities(activities)

        day_payload = {"day": day, "date": date, "activities": activities}
        previous_count = len(plan.daily_plans)
        append_one_day_plan(plan, day_payload)
        return {
            "updated_field": "daily_plans",
            "action": "append",
            "day": day,
            "date": date,
            "activity_count": len(activities),
            "total_days": len(plan.daily_plans),
            "previous_days": previous_count,
        }

    return append_day_plan


def make_replace_daily_plans_tool(plan: TravelPlanState):
    @tool(
        name="replace_daily_plans",
        description="整体替换所有逐日行程。传入完整的天数列表。",
        phases=[5],
        parameters=_REPLACE_DAILY_PLANS_PARAMETERS,
        side_effect="write",
        human_label="整体替换逐日行程",
    )
    async def replace_daily_plans(days: list) -> dict:
        if not isinstance(days, list):
            raise ToolError(
                f"days 必须是 list，收到 {type(days).__name__}",
                error_code="INVALID_VALUE",
                suggestion="days 应为 list[dict]，每个 dict 包含 day, date, activities",
            )
        for i, d in enumerate(days):
            if not isinstance(d, dict):
                raise ToolError(
                    f"days[{i}] 必须是 dict，收到 {type(d).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每天的数据必须是 dict，包含 day, date, activities",
                )
            missing = {"day", "date", "activities"} - set(d.keys())
            if missing:
                raise ToolError(
                    f"days[{i}] 缺少必填字段: {', '.join(sorted(missing))}",
                    error_code="INVALID_VALUE",
                    suggestion="每天必须包含 day, date, activities",
                )
            if not isinstance(d["day"], int):
                raise ToolError(
                    f"days[{i}].day 必须是整数，收到 {type(d['day']).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="day 应为正整数，如 1、2、3",
                )
            _validate_date_format(str(d["date"]))
            _validate_activities(d["activities"])

        previous_count = len(plan.daily_plans)
        replace_all_daily_plans(plan, days)
        return {
            "updated_field": "daily_plans",
            "action": "replace",
            "total_days": len(plan.daily_plans),
            "previous_days": previous_count,
        }

    return replace_daily_plans
```

- [ ] **Step 2: Create `backend/tests/test_plan_tools/test_daily_plans.py`**

```python
# backend/tests/test_plan_tools/test_daily_plans.py
from __future__ import annotations

import pytest

from state.models import TravelPlanState
from tools.plan_tools.daily_plans import (
    make_append_day_plan_tool,
    make_replace_daily_plans_tool,
)


def _make_plan(phase: int = 5) -> TravelPlanState:
    plan = TravelPlanState(session_id="test-daily")
    plan.phase = phase
    return plan


def _sample_activity() -> dict:
    return {
        "name": "故宫博物院",
        "location": {"name": "故宫", "lat": 39.916, "lng": 116.397},
        "start_time": "09:00",
        "end_time": "12:00",
        "category": "景点",
        "cost": 60,
    }


class TestAppendDayPlan:
    @pytest.mark.asyncio
    async def test_append_day_plan_success(self):
        plan = _make_plan()
        tool = make_append_day_plan_tool(plan)
        result = await tool(day=1, date="2026-05-01", activities=[_sample_activity()])
        assert result["action"] == "append"
        assert result["day"] == 1
        assert result["total_days"] == 1
        assert len(plan.daily_plans) == 1
        assert plan.daily_plans[0].day == 1
        assert plan.daily_plans[0].date == "2026-05-01"
        assert len(plan.daily_plans[0].activities) == 1

    @pytest.mark.asyncio
    async def test_append_day_plan_rejects_string_activities(self):
        plan = _make_plan()
        tool = make_append_day_plan_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(day=1, date="2026-05-01", activities="not a list")
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_append_day_plan_validates_date_format(self):
        plan = _make_plan()
        tool = make_append_day_plan_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(day=1, date="5月1日", activities=[_sample_activity()])
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    def test_append_day_plan_side_effect(self):
        plan = _make_plan()
        tool = make_append_day_plan_tool(plan)
        assert tool.side_effect == "write"

    def test_append_day_plan_human_label(self):
        plan = _make_plan()
        tool = make_append_day_plan_tool(plan)
        assert tool.human_label == "追加一天行程"


class TestReplaceDailyPlans:
    @pytest.mark.asyncio
    async def test_replace_daily_plans_success(self):
        plan = _make_plan()
        tool = make_replace_daily_plans_tool(plan)
        days = [
            {"day": 1, "date": "2026-05-01", "activities": [_sample_activity()]},
            {"day": 2, "date": "2026-05-02", "activities": [_sample_activity()]},
        ]
        result = await tool(days=days)
        assert result["action"] == "replace"
        assert result["total_days"] == 2
        assert len(plan.daily_plans) == 2
        assert plan.daily_plans[0].day == 1
        assert plan.daily_plans[1].day == 2

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_non_list(self):
        plan = _make_plan()
        tool = make_replace_daily_plans_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(days="not a list")
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_string_item(self):
        plan = _make_plan()
        tool = make_replace_daily_plans_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(days=["not a dict"])
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_missing_day(self):
        plan = _make_plan()
        tool = make_replace_daily_plans_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(days=[{"date": "2026-05-01", "activities": [_sample_activity()]}])
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_replace_daily_plans_rejects_bad_date_format(self):
        plan = _make_plan()
        tool = make_replace_daily_plans_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(days=[{"day": 1, "date": "5月1日", "activities": [_sample_activity()]}])
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    def test_replace_daily_plans_side_effect(self):
        plan = _make_plan()
        tool = make_replace_daily_plans_tool(plan)
        assert tool.side_effect == "write"

    def test_replace_daily_plans_human_label(self):
        plan = _make_plan()
        tool = make_replace_daily_plans_tool(plan)
        assert tool.human_label == "整体替换逐日行程"
```

- [ ] **Step 3: Run tests**

```bash
cd backend && python -m pytest tests/test_plan_tools/test_daily_plans.py -v
```

Expected:

```
tests/test_plan_tools/test_daily_plans.py::TestAppendDayPlan::test_append_day_plan_success PASSED
tests/test_plan_tools/test_daily_plans.py::TestAppendDayPlan::test_append_day_plan_rejects_string_activities PASSED
tests/test_plan_tools/test_daily_plans.py::TestAppendDayPlan::test_append_day_plan_validates_date_format PASSED
tests/test_plan_tools/test_daily_plans.py::TestAppendDayPlan::test_append_day_plan_side_effect PASSED
tests/test_plan_tools/test_daily_plans.py::TestAppendDayPlan::test_append_day_plan_human_label PASSED
tests/test_plan_tools/test_daily_plans.py::TestReplaceDailyPlans::test_replace_daily_plans_success PASSED
tests/test_plan_tools/test_daily_plans.py::TestReplaceDailyPlans::test_replace_daily_plans_rejects_non_list PASSED
tests/test_plan_tools/test_daily_plans.py::TestReplaceDailyPlans::test_replace_daily_plans_rejects_string_item PASSED
tests/test_plan_tools/test_daily_plans.py::TestReplaceDailyPlans::test_replace_daily_plans_rejects_missing_day PASSED
tests/test_plan_tools/test_daily_plans.py::TestReplaceDailyPlans::test_replace_daily_plans_rejects_bad_date_format PASSED
tests/test_plan_tools/test_daily_plans.py::TestReplaceDailyPlans::test_replace_daily_plans_side_effect PASSED
tests/test_plan_tools/test_daily_plans.py::TestReplaceDailyPlans::test_replace_daily_plans_human_label PASSED
```

- [ ] **Step 4: Commit**

```bash
cd backend && git add tools/plan_tools/daily_plans.py tests/test_plan_tools/test_daily_plans.py
git commit -m "feat(plan-tools): add append_day_plan and replace_daily_plans tools

- append_day_plan: validates day/date/activities, appends one DayPlan via writer
- replace_daily_plans: validates required day/date/activities for every item, replaces all via writer
- Both enforce required activity fields (name, location, start_time, end_time, category, cost)
- Full test coverage for success, type rejection, side_effect, human_label"
```

---

### Task 7: Create update_trip_basics tool (`tools/plan_tools/trip_basics.py` + tests)

**Files:**
- Create: `backend/tools/plan_tools/trip_basics.py`
- Create: `backend/tests/test_plan_tools/test_trip_basics.py`

- [ ] **Step 1: Create `backend/tools/plan_tools/trip_basics.py`**

```python
# backend/tools/plan_tools/trip_basics.py
from __future__ import annotations

from typing import Any

from state.intake import parse_budget_value, parse_dates_value, parse_travelers_value
from state.models import TravelPlanState
from state.plan_writers import (
    write_budget,
    write_dates,
    write_departure_city,
    write_destination,
    write_travelers,
)
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "destination": {
            "description": "目的地名称",
            "anyOf": [{"type": "string"}, {"type": "object"}],
        },
        "dates": {
            "description": "出行日期。结构化: {start, end}；或短语: '5月1日到5日'",
            "anyOf": [{"type": "string"}, {"type": "object"}],
        },
        "travelers": {
            "description": "同行人数。结构化: {adults, children?}；或短语: '2个人'",
            "anyOf": [{"type": "string"}, {"type": "object"}, {"type": "integer"}],
        },
        "budget": {
            "description": "预算。结构化: {total, currency?}；或短语: '人均2000'；或数字",
            "anyOf": [{"type": "string"}, {"type": "object"}, {"type": "number"}],
        },
        "departure_city": {
            "description": "出发城市。可传字符串，或对象如 {city: '上海'} / {name: '上海'}",
            "anyOf": [{"type": "string"}, {"type": "object"}],
        },
    },
}


def make_update_trip_basics_tool(plan: TravelPlanState):
    @tool(
        name="update_trip_basics",
        description=(
            "更新行程基础信息（目的地、日期、人数、预算、出发城市）。"
            "每个字段均可选，只传需要更新的字段。"
            "支持结构化输入和自然语言短语。"
        ),
        phases=[1, 3],
        parameters=_PARAMETERS,
        side_effect="write",
        human_label="更新行程基础信息",
    )
    async def update_trip_basics(
        destination: str | dict | None = None,
        dates: str | dict | None = None,
        travelers: str | dict | int | None = None,
        budget: str | dict | float | int | None = None,
        departure_city: str | dict | None = None,
    ) -> dict:
        updated_fields: list[str] = []

        if (
            destination is None
            and dates is None
            and travelers is None
            and budget is None
            and departure_city is None
        ):
            raise ToolError(
                "至少需要提供一个字段进行更新",
                error_code="INVALID_VALUE",
                suggestion="可更新字段: destination, dates, travelers, budget, departure_city",
            )

        if destination is not None:
            write_destination(plan, destination)
            updated_fields.append("destination")

        if dates is not None:
            parsed = parse_dates_value(dates)
            if parsed is None:
                raise ToolError(
                    f"无法解析日期: {dates!r}",
                    error_code="INVALID_VALUE",
                    suggestion='请传入 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"} 或可解析的短语',
                )
            write_dates(plan, dates)
            updated_fields.append("dates")

        if travelers is not None:
            parsed_t = parse_travelers_value(travelers)
            if parsed_t is None:
                raise ToolError(
                    f"无法解析人数: {travelers!r}",
                    error_code="INVALID_VALUE",
                    suggestion='请传入 {"adults": 2} 或 "2个人" 等可解析格式',
                )
            write_travelers(plan, travelers)
            updated_fields.append("travelers")

        if budget is not None:
            parsed_b = parse_budget_value(budget)
            if parsed_b is None:
                raise ToolError(
                    f"无法解析预算: {budget!r}",
                    error_code="INVALID_VALUE",
                    suggestion='请传入 {"total": 10000} 或 "1万" 或数字',
                )
            write_budget(plan, budget)
            updated_fields.append("budget")

        if departure_city is not None:
            write_departure_city(plan, departure_city)
            updated_fields.append("departure_city")

        return {
            "updated_fields": updated_fields,
            "count": len(updated_fields),
        }

    return update_trip_basics
```

- [ ] **Step 2: Create `backend/tests/test_plan_tools/test_trip_basics.py`**

```python
# backend/tests/test_plan_tools/test_trip_basics.py
from __future__ import annotations

import pytest

from state.models import TravelPlanState
from tools.plan_tools.trip_basics import make_update_trip_basics_tool


def _make_plan(phase: int = 1) -> TravelPlanState:
    plan = TravelPlanState(session_id="test-basics")
    plan.phase = phase
    return plan


class TestUpdateTripBasics:
    @pytest.mark.asyncio
    async def test_update_destination_string(self):
        plan = _make_plan()
        tool = make_update_trip_basics_tool(plan)
        result = await tool(destination="东京")
        assert "destination" in result["updated_fields"]
        assert plan.destination == "东京"

    @pytest.mark.asyncio
    async def test_update_dates_structured(self):
        plan = _make_plan()
        tool = make_update_trip_basics_tool(plan)
        result = await tool(dates={"start": "2026-05-01", "end": "2026-05-05"})
        assert "dates" in result["updated_fields"]
        assert plan.dates is not None
        assert plan.dates.start == "2026-05-01"
        assert plan.dates.end == "2026-05-05"

    @pytest.mark.asyncio
    async def test_update_dates_phrase(self):
        plan = _make_plan()
        tool = make_update_trip_basics_tool(plan)
        result = await tool(dates="5月1日到5月5日")
        assert "dates" in result["updated_fields"]
        assert plan.dates is not None
        assert "05-01" in plan.dates.start
        assert "05-05" in plan.dates.end

    @pytest.mark.asyncio
    async def test_update_travelers_phrase(self):
        plan = _make_plan()
        tool = make_update_trip_basics_tool(plan)
        result = await tool(travelers="2个人")
        assert "travelers" in result["updated_fields"]
        assert plan.travelers is not None
        assert plan.travelers.adults == 2

    @pytest.mark.asyncio
    async def test_update_budget_number(self):
        plan = _make_plan()
        tool = make_update_trip_basics_tool(plan)
        result = await tool(budget=10000)
        assert "budget" in result["updated_fields"]
        assert plan.budget is not None
        assert plan.budget.total == 10000.0

    @pytest.mark.asyncio
    async def test_update_multiple_fields_at_once(self):
        plan = _make_plan()
        tool = make_update_trip_basics_tool(plan)
        result = await tool(
            destination="大阪",
            budget=20000,
            travelers={"adults": 2, "children": 1},
        )
        assert result["count"] == 3
        assert plan.destination == "大阪"
        assert plan.budget.total == 20000.0
        assert plan.travelers.adults == 2
        assert plan.travelers.children == 1

    @pytest.mark.asyncio
    async def test_no_fields_provided_raises_error(self):
        plan = _make_plan()
        tool = make_update_trip_basics_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool()
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_departure_city_updates_trip_brief(self):
        plan = _make_plan()
        tool = make_update_trip_basics_tool(plan)
        result = await tool(departure_city="上海")
        assert "departure_city" in result["updated_fields"]
        assert plan.trip_brief.get("departure_city") == "上海"

    @pytest.mark.asyncio
    async def test_departure_city_object_updates_trip_brief(self):
        plan = _make_plan()
        tool = make_update_trip_basics_tool(plan)
        result = await tool(departure_city={"city": "杭州"})
        assert "departure_city" in result["updated_fields"]
        assert plan.trip_brief.get("departure_city") == "杭州"

    def test_side_effect_is_write(self):
        plan = _make_plan()
        tool = make_update_trip_basics_tool(plan)
        assert tool.side_effect == "write"

    def test_human_label(self):
        plan = _make_plan()
        tool = make_update_trip_basics_tool(plan)
        assert tool.human_label == "更新行程基础信息"
```

- [ ] **Step 3: Run tests**

```bash
cd backend && python -m pytest tests/test_plan_tools/test_trip_basics.py -v
```

Expected:

```
tests/test_plan_tools/test_trip_basics.py::TestUpdateTripBasics::test_update_destination_string PASSED
tests/test_plan_tools/test_trip_basics.py::TestUpdateTripBasics::test_update_dates_structured PASSED
tests/test_plan_tools/test_trip_basics.py::TestUpdateTripBasics::test_update_dates_phrase PASSED
tests/test_plan_tools/test_trip_basics.py::TestUpdateTripBasics::test_update_travelers_phrase PASSED
tests/test_plan_tools/test_trip_basics.py::TestUpdateTripBasics::test_update_budget_number PASSED
tests/test_plan_tools/test_trip_basics.py::TestUpdateTripBasics::test_update_multiple_fields_at_once PASSED
tests/test_plan_tools/test_trip_basics.py::TestUpdateTripBasics::test_no_fields_provided_raises_error PASSED
tests/test_plan_tools/test_trip_basics.py::TestUpdateTripBasics::test_departure_city_updates_trip_brief PASSED
tests/test_plan_tools/test_trip_basics.py::TestUpdateTripBasics::test_departure_city_object_updates_trip_brief PASSED
tests/test_plan_tools/test_trip_basics.py::TestUpdateTripBasics::test_side_effect_is_write PASSED
tests/test_plan_tools/test_trip_basics.py::TestUpdateTripBasics::test_human_label PASSED
```

- [ ] **Step 4: Commit**

```bash
cd backend && git add tools/plan_tools/trip_basics.py tests/test_plan_tools/test_trip_basics.py
git commit -m "feat(plan-tools): add update_trip_basics tool with anyOf phrase tolerance

- Supports destination, dates, travelers, budget, departure_city
- Each field uses anyOf schema for structured input or natural language phrases
- Uses parse_dates_value, parse_travelers_value, parse_budget_value from state.intake
- departure_city accepts string or object and writes normalized city text to plan.trip_brief
- Raises ToolError when no fields provided or parse fails
- Full test coverage including phrase parsing paths"
```

---

### Task 8: Create append tools (`tools/plan_tools/append_tools.py` + tests)

**Files:**
- Create: `backend/tools/plan_tools/append_tools.py`
- Create: `backend/tests/test_plan_tools/test_append_tools.py`

- [ ] **Step 1: Create `backend/tools/plan_tools/append_tools.py`**

```python
# backend/tools/plan_tools/append_tools.py
from __future__ import annotations

from typing import Any

from state.models import TravelPlanState
from state.plan_writers import (
    append_constraints,
    append_destination_candidate,
    append_preferences,
    replace_destination_candidates,
)
from tools.base import ToolError, tool

_ADD_PREFERENCES_PARAMETERS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "value": {"type": "string"},
                        },
                    },
                ],
            },
            "description": "偏好列表，每项可以是字符串或 {key, value} 对象",
        },
    },
    "required": ["items"],
}

_ADD_CONSTRAINTS_PARAMETERS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "description": "hard 或 soft"},
                            "description": {"type": "string"},
                        },
                    },
                ],
            },
            "description": "约束列表，每项可以是字符串或 {type, description} 对象",
        },
    },
    "required": ["items"],
}

_ADD_DESTINATION_CANDIDATE_PARAMETERS = {
    "type": "object",
    "properties": {
        "item": {
            "type": "object",
            "description": "一个目的地候选对象",
        },
    },
    "required": ["item"],
}

_SET_DESTINATION_CANDIDATES_PARAMETERS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object"},
            "description": "完整的目的地候选列表",
        },
    },
    "required": ["items"],
}


def _validate_preference_item(item: Any) -> None:
    if isinstance(item, (str, dict)):
        return
    raise ToolError(
        f"偏好项格式错误: 期望 string 或 dict，收到 {type(item).__name__}",
        error_code="INVALID_VALUE",
        suggestion='每项应为字符串如 "喜欢海鲜" 或对象如 {"key": "美食", "value": "喜欢海鲜"}',
    )


def _validate_constraint_item(item: Any) -> None:
    if isinstance(item, (str, dict)):
        return
    raise ToolError(
        f"约束项格式错误: 期望 string 或 dict，收到 {type(item).__name__}",
        error_code="INVALID_VALUE",
        suggestion='每项应为字符串如 "不坐红眼航班" 或对象如 {"type": "hard", "description": "不坐红眼航班"}',
    )


def make_add_preferences_tool(plan: TravelPlanState):
    @tool(
        name="add_preferences",
        description="记录用户偏好。追加到现有偏好列表，不会覆盖已有条目。",
        phases=[1, 3, 5],
        parameters=_ADD_PREFERENCES_PARAMETERS,
        side_effect="write",
        human_label="记录用户偏好",
    )
    async def add_preferences(items: list) -> dict:
        if not isinstance(items, list):
            raise ToolError(
                f"items 必须是 list，收到 {type(items).__name__}",
                error_code="INVALID_VALUE",
                suggestion="items 应为 list，每项是字符串或 {key, value} 对象",
            )
        for item in items:
            _validate_preference_item(item)
        previous_count = len(plan.preferences)
        append_preferences(plan, items)
        return {
            "updated_field": "preferences",
            "added_count": len(items),
            "total_count": len(plan.preferences),
            "previous_count": previous_count,
        }

    return add_preferences


def make_add_constraints_tool(plan: TravelPlanState):
    @tool(
        name="add_constraints",
        description="记录用户约束条件。追加到现有约束列表，不会覆盖已有条目。",
        phases=[1, 3, 5],
        parameters=_ADD_CONSTRAINTS_PARAMETERS,
        side_effect="write",
        human_label="记录用户约束",
    )
    async def add_constraints(items: list) -> dict:
        if not isinstance(items, list):
            raise ToolError(
                f"items 必须是 list，收到 {type(items).__name__}",
                error_code="INVALID_VALUE",
                suggestion="items 应为 list，每项是字符串或 {type, description} 对象",
            )
        for item in items:
            _validate_constraint_item(item)
        previous_count = len(plan.constraints)
        append_constraints(plan, items)
        return {
            "updated_field": "constraints",
            "added_count": len(items),
            "total_count": len(plan.constraints),
            "previous_count": previous_count,
        }

    return add_constraints


def make_add_destination_candidate_tool(plan: TravelPlanState):
    @tool(
        name="add_destination_candidate",
        description="追加一个目的地候选到列表末尾。",
        phases=[1],
        parameters=_ADD_DESTINATION_CANDIDATE_PARAMETERS,
        side_effect="write",
        human_label="追加目的地候选",
    )
    async def add_destination_candidate(item: dict) -> dict:
        if not isinstance(item, dict):
            raise ToolError(
                f"item 必须是 dict，收到 {type(item).__name__}",
                error_code="INVALID_VALUE",
                suggestion="item 应为目的地候选对象",
            )
        previous_count = len(plan.destination_candidates)
        append_destination_candidate(plan, item)
        return {
            "updated_field": "destination_candidates",
            "action": "append",
            "total_count": len(plan.destination_candidates),
            "previous_count": previous_count,
        }

    return add_destination_candidate


def make_set_destination_candidates_tool(plan: TravelPlanState):
    @tool(
        name="set_destination_candidates",
        description="整体替换目的地候选列表。",
        phases=[1],
        parameters=_SET_DESTINATION_CANDIDATES_PARAMETERS,
        side_effect="write",
        human_label="整体替换候选列表",
    )
    async def set_destination_candidates(items: list) -> dict:
        if not isinstance(items, list):
            raise ToolError(
                f"items 必须是 list，收到 {type(items).__name__}",
                error_code="INVALID_VALUE",
                suggestion="items 应为 list[dict]",
            )
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ToolError(
                    f"items[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个候选必须是 dict",
                )
        previous_count = len(plan.destination_candidates)
        replace_destination_candidates(plan, items)
        return {
            "updated_field": "destination_candidates",
            "action": "replace",
            "total_count": len(plan.destination_candidates),
            "previous_count": previous_count,
        }

    return set_destination_candidates
```

- [ ] **Step 2: Create `backend/tests/test_plan_tools/test_append_tools.py`**

```python
# backend/tests/test_plan_tools/test_append_tools.py
from __future__ import annotations

import pytest

from state.models import TravelPlanState
from tools.plan_tools.append_tools import (
    make_add_constraints_tool,
    make_add_destination_candidate_tool,
    make_add_preferences_tool,
    make_set_destination_candidates_tool,
)


def _make_plan(phase: int = 1) -> TravelPlanState:
    plan = TravelPlanState(session_id="test-append")
    plan.phase = phase
    return plan


class TestAddPreferences:
    @pytest.mark.asyncio
    async def test_add_string_preferences(self):
        plan = _make_plan()
        tool = make_add_preferences_tool(plan)
        result = await tool(items=["喜欢海鲜", "想去海边"])
        assert result["added_count"] == 2
        assert len(plan.preferences) == 2
        assert plan.preferences[0].key == "喜欢海鲜"

    @pytest.mark.asyncio
    async def test_add_dict_preferences(self):
        plan = _make_plan()
        tool = make_add_preferences_tool(plan)
        result = await tool(items=[{"key": "美食", "value": "喜欢海鲜"}])
        assert result["added_count"] == 1
        assert plan.preferences[0].key == "美食"
        assert plan.preferences[0].value == "喜欢海鲜"

    @pytest.mark.asyncio
    async def test_add_preferences_rejects_non_list(self):
        plan = _make_plan()
        tool = make_add_preferences_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(items="not a list")
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_add_preferences_appends_to_existing(self):
        plan = _make_plan()
        tool = make_add_preferences_tool(plan)
        await tool(items=["第一条"])
        result = await tool(items=["第二条"])
        assert result["total_count"] == 2
        assert result["previous_count"] == 1

    def test_side_effect_is_write(self):
        plan = _make_plan()
        tool = make_add_preferences_tool(plan)
        assert tool.side_effect == "write"

    def test_human_label(self):
        plan = _make_plan()
        tool = make_add_preferences_tool(plan)
        assert tool.human_label == "记录用户偏好"


class TestAddConstraints:
    @pytest.mark.asyncio
    async def test_add_string_constraints(self):
        plan = _make_plan()
        tool = make_add_constraints_tool(plan)
        result = await tool(items=["不坐红眼航班"])
        assert result["added_count"] == 1
        assert plan.constraints[0].type == "soft"
        assert plan.constraints[0].description == "不坐红眼航班"

    @pytest.mark.asyncio
    async def test_add_dict_constraints(self):
        plan = _make_plan()
        tool = make_add_constraints_tool(plan)
        result = await tool(items=[{"type": "hard", "description": "必须直飞"}])
        assert result["added_count"] == 1
        assert plan.constraints[0].type == "hard"
        assert plan.constraints[0].description == "必须直飞"

    @pytest.mark.asyncio
    async def test_add_constraints_rejects_non_list(self):
        plan = _make_plan()
        tool = make_add_constraints_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(items="not a list")
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    def test_side_effect_is_write(self):
        plan = _make_plan()
        tool = make_add_constraints_tool(plan)
        assert tool.side_effect == "write"

    def test_human_label(self):
        plan = _make_plan()
        tool = make_add_constraints_tool(plan)
        assert tool.human_label == "记录用户约束"


class TestAddDestinationCandidate:
    @pytest.mark.asyncio
    async def test_add_candidate_success(self):
        plan = _make_plan()
        tool = make_add_destination_candidate_tool(plan)
        result = await tool(item={"name": "东京", "reason": "美食丰富"})
        assert result["action"] == "append"
        assert result["total_count"] == 1
        assert plan.destination_candidates[0]["name"] == "东京"

    @pytest.mark.asyncio
    async def test_add_candidate_rejects_non_dict(self):
        plan = _make_plan()
        tool = make_add_destination_candidate_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(item="not a dict")
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    def test_side_effect_is_write(self):
        plan = _make_plan()
        tool = make_add_destination_candidate_tool(plan)
        assert tool.side_effect == "write"

    def test_human_label(self):
        plan = _make_plan()
        tool = make_add_destination_candidate_tool(plan)
        assert tool.human_label == "追加目的地候选"


class TestSetDestinationCandidates:
    @pytest.mark.asyncio
    async def test_replace_candidates_success(self):
        plan = _make_plan()
        plan.destination_candidates = [{"name": "旧候选"}]
        tool = make_set_destination_candidates_tool(plan)
        result = await tool(items=[{"name": "东京"}, {"name": "大阪"}])
        assert result["action"] == "replace"
        assert result["total_count"] == 2
        assert result["previous_count"] == 1
        assert plan.destination_candidates[0]["name"] == "东京"

    @pytest.mark.asyncio
    async def test_replace_candidates_rejects_non_list(self):
        plan = _make_plan()
        tool = make_set_destination_candidates_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(items="not a list")
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_replace_candidates_rejects_non_dict_item(self):
        plan = _make_plan()
        tool = make_set_destination_candidates_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(items=["not a dict"])
        assert "INVALID_VALUE" in str(exc_info.value.error_code)

    def test_side_effect_is_write(self):
        plan = _make_plan()
        tool = make_set_destination_candidates_tool(plan)
        assert tool.side_effect == "write"

    def test_human_label(self):
        plan = _make_plan()
        tool = make_set_destination_candidates_tool(plan)
        assert tool.human_label == "整体替换候选列表"
```

- [ ] **Step 3: Run tests**

```bash
cd backend && python -m pytest tests/test_plan_tools/test_append_tools.py -v
```

Expected:

```
tests/test_plan_tools/test_append_tools.py::TestAddPreferences::test_add_string_preferences PASSED
tests/test_plan_tools/test_append_tools.py::TestAddPreferences::test_add_dict_preferences PASSED
tests/test_plan_tools/test_append_tools.py::TestAddPreferences::test_add_preferences_rejects_non_list PASSED
tests/test_plan_tools/test_append_tools.py::TestAddPreferences::test_add_preferences_appends_to_existing PASSED
tests/test_plan_tools/test_append_tools.py::TestAddPreferences::test_side_effect_is_write PASSED
tests/test_plan_tools/test_append_tools.py::TestAddPreferences::test_human_label PASSED
tests/test_plan_tools/test_append_tools.py::TestAddConstraints::test_add_string_constraints PASSED
tests/test_plan_tools/test_append_tools.py::TestAddConstraints::test_add_dict_constraints PASSED
tests/test_plan_tools/test_append_tools.py::TestAddConstraints::test_add_constraints_rejects_non_list PASSED
tests/test_plan_tools/test_append_tools.py::TestAddConstraints::test_side_effect_is_write PASSED
tests/test_plan_tools/test_append_tools.py::TestAddConstraints::test_human_label PASSED
tests/test_plan_tools/test_append_tools.py::TestAddDestinationCandidate::test_add_candidate_success PASSED
tests/test_plan_tools/test_append_tools.py::TestAddDestinationCandidate::test_add_candidate_rejects_non_dict PASSED
tests/test_plan_tools/test_append_tools.py::TestAddDestinationCandidate::test_side_effect_is_write PASSED
tests/test_plan_tools/test_append_tools.py::TestAddDestinationCandidate::test_human_label PASSED
tests/test_plan_tools/test_append_tools.py::TestSetDestinationCandidates::test_replace_candidates_success PASSED
tests/test_plan_tools/test_append_tools.py::TestSetDestinationCandidates::test_replace_candidates_rejects_non_list PASSED
tests/test_plan_tools/test_append_tools.py::TestSetDestinationCandidates::test_replace_candidates_rejects_non_dict_item PASSED
tests/test_plan_tools/test_append_tools.py::TestSetDestinationCandidates::test_side_effect_is_write PASSED
tests/test_plan_tools/test_append_tools.py::TestSetDestinationCandidates::test_human_label PASSED
```

- [ ] **Step 4: Commit**

```bash
cd backend && git add tools/plan_tools/append_tools.py tests/test_plan_tools/test_append_tools.py
git commit -m "feat(plan-tools): add preferences, constraints, and destination candidate tools

- add_preferences: accepts string or {key, value} items, appends via writer
- add_constraints: accepts string or {type, description} items, appends via writer
- add_destination_candidate: appends one candidate dict
- set_destination_candidates: replaces entire candidate list
- All tools validate input types, have write side_effect and Chinese human_labels
- Full test coverage for all 4 tools"
```

---

### Task 9: Create request_backtrack tool (`tools/plan_tools/backtrack.py` + tests)

**Files:**
- Create: `backend/tools/plan_tools/backtrack.py`
- Create: `backend/tests/test_plan_tools/test_backtrack.py`

- [ ] **Step 1: Create `backend/tools/plan_tools/backtrack.py`**

```python
# backend/tools/plan_tools/backtrack.py
from __future__ import annotations

from state.models import TravelPlanState
from state.plan_writers import execute_backtrack
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "to_phase": {
            "type": "integer",
            "description": "要回退到的目标阶段（必须小于当前阶段）",
        },
        "reason": {
            "type": "string",
            "description": "回退原因",
        },
    },
    "required": ["to_phase", "reason"],
}


def make_request_backtrack_tool(plan: TravelPlanState):
    @tool(
        name="request_backtrack",
        description=(
            "请求回退到更早的规划阶段。"
            "当用户想推翻之前的阶段决策时使用。"
            "目标阶段必须小于当前阶段。"
        ),
        phases=[1, 3, 5, 7],
        parameters=_PARAMETERS,
        side_effect="write",
        human_label="请求回退阶段",
    )
    async def request_backtrack(to_phase: int, reason: str) -> dict:
        if not isinstance(to_phase, int):
            raise ToolError(
                f"to_phase 必须是整数，收到 {type(to_phase).__name__}",
                error_code="INVALID_VALUE",
                suggestion="to_phase 应为整数，如 1、3、5",
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ToolError(
                "reason 必须是非空字符串",
                error_code="INVALID_VALUE",
                suggestion="请提供回退原因",
            )

        # Phase 2 does not exist as a standalone phase; adjust to 1
        if to_phase == 2:
            to_phase = 1

        if to_phase >= plan.phase:
            raise ToolError(
                f"只能回退到更早的阶段，当前阶段: {plan.phase}，目标: {to_phase}",
                error_code="INVALID_BACKTRACK",
                suggestion=f"目标阶段必须小于当前阶段 {plan.phase}",
            )

        from_phase = plan.phase
        execute_backtrack(plan, to_phase, reason)

        return {
            "backtracked": True,
            "from_phase": from_phase,
            "to_phase": to_phase,
            "reason": reason,
            "next_action": "请向用户确认回退结果",
        }

    return request_backtrack
```

- [ ] **Step 2: Create `backend/tests/test_plan_tools/test_backtrack.py`**

```python
# backend/tests/test_plan_tools/test_backtrack.py
from __future__ import annotations

import pytest

from state.models import TravelPlanState
from tools.plan_tools.backtrack import make_request_backtrack_tool


def _make_plan(phase: int = 5) -> TravelPlanState:
    plan = TravelPlanState(session_id="test-backtrack")
    plan.phase = phase
    return plan


class TestRequestBacktrack:
    @pytest.mark.asyncio
    async def test_backtrack_success(self):
        plan = _make_plan(phase=5)
        tool = make_request_backtrack_tool(plan)
        result = await tool(to_phase=3, reason="用户想换目的地")
        assert result["backtracked"] is True
        assert result["from_phase"] == 5
        assert result["to_phase"] == 3
        assert result["reason"] == "用户想换目的地"
        assert plan.phase == 3
        assert len(plan.backtrack_history) == 1

    @pytest.mark.asyncio
    async def test_backtrack_rejects_forward(self):
        plan = _make_plan(phase=3)
        tool = make_request_backtrack_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(to_phase=5, reason="想跳到后面")
        assert "INVALID_BACKTRACK" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_backtrack_rejects_same_phase(self):
        plan = _make_plan(phase=3)
        tool = make_request_backtrack_tool(plan)
        with pytest.raises(Exception) as exc_info:
            await tool(to_phase=3, reason="回退到当前阶段")
        assert "INVALID_BACKTRACK" in str(exc_info.value.error_code)

    @pytest.mark.asyncio
    async def test_backtrack_phase2_adjusted_to_1(self):
        plan = _make_plan(phase=5)
        tool = make_request_backtrack_tool(plan)
        result = await tool(to_phase=2, reason="回到最初")
        assert result["to_phase"] == 1
        assert plan.phase == 1

    def test_backtrack_has_correct_human_label(self):
        plan = _make_plan()
        tool = make_request_backtrack_tool(plan)
        assert tool.human_label == "请求回退阶段"

    def test_backtrack_side_effect_is_write(self):
        plan = _make_plan()
        tool = make_request_backtrack_tool(plan)
        assert tool.side_effect == "write"

    @pytest.mark.asyncio
    async def test_backtrack_clears_downstream(self):
        plan = _make_plan(phase=5)
        plan.daily_plans = []
        plan.skeleton_plans = [{"id": "a"}]
        tool = make_request_backtrack_tool(plan)
        await tool(to_phase=3, reason="重新规划")
        # Phase 3 downstream includes daily_plans and skeleton_plans
        assert plan.daily_plans == []
        assert plan.skeleton_plans == []

    @pytest.mark.asyncio
    async def test_backtrack_records_history(self):
        plan = _make_plan(phase=5)
        tool = make_request_backtrack_tool(plan)
        await tool(to_phase=1, reason="从头开始")
        assert len(plan.backtrack_history) == 1
        event = plan.backtrack_history[0]
        assert event.from_phase == 5
        assert event.to_phase == 1
        assert event.reason == "从头开始"
```

- [ ] **Step 3: Run tests**

```bash
cd backend && python -m pytest tests/test_plan_tools/test_backtrack.py -v
```

Expected:

```
tests/test_plan_tools/test_backtrack.py::TestRequestBacktrack::test_backtrack_success PASSED
tests/test_plan_tools/test_backtrack.py::TestRequestBacktrack::test_backtrack_rejects_forward PASSED
tests/test_plan_tools/test_backtrack.py::TestRequestBacktrack::test_backtrack_rejects_same_phase PASSED
tests/test_plan_tools/test_backtrack.py::TestRequestBacktrack::test_backtrack_phase2_adjusted_to_1 PASSED
tests/test_plan_tools/test_backtrack.py::TestRequestBacktrack::test_backtrack_has_correct_human_label PASSED
tests/test_plan_tools/test_backtrack.py::TestRequestBacktrack::test_backtrack_side_effect_is_write PASSED
tests/test_plan_tools/test_backtrack.py::TestRequestBacktrack::test_backtrack_clears_downstream PASSED
tests/test_plan_tools/test_backtrack.py::TestRequestBacktrack::test_backtrack_records_history PASSED
```

- [ ] **Step 4: Commit**

```bash
cd backend && git add tools/plan_tools/backtrack.py tests/test_plan_tools/test_backtrack.py
git commit -m "feat(plan-tools): add request_backtrack tool

- Validates to_phase < current phase, adjusts phase 2 to 1
- Delegates to execute_backtrack writer (BacktrackService)
- Clears downstream state, records backtrack history event
- Returns structured result with next_action guidance
- Full test coverage: success, forward rejection, phase 2 adjustment,
  downstream clearing, history recording"
```

---

### Task 10: Create `tools/plan_tools/__init__.py` + test helper + integration tests

**Files:**
- Create: `backend/tools/plan_tools/__init__.py`
- Create: `backend/tests/helpers/__init__.py`
- Create: `backend/tests/helpers/register_plan_tools.py`
- Create: `backend/tests/test_plan_tools/test_init.py`

- [ ] **Step 1: Create `backend/tools/plan_tools/__init__.py`**

```python
# backend/tools/plan_tools/__init__.py
from __future__ import annotations

from state.models import TravelPlanState
from tools.base import ToolDef

from tools.plan_tools.append_tools import (
    make_add_constraints_tool,
    make_add_destination_candidate_tool,
    make_add_preferences_tool,
    make_set_destination_candidates_tool,
)
from tools.plan_tools.backtrack import make_request_backtrack_tool
from tools.plan_tools.daily_plans import (
    make_append_day_plan_tool,
    make_replace_daily_plans_tool,
)
from tools.plan_tools.phase3_tools import (
    make_select_skeleton_tool,
    make_select_transport_tool,
    make_set_accommodation_options_tool,
    make_set_accommodation_tool,
    make_set_alternatives_tool,
    make_set_candidate_pool_tool,
    make_set_risks_tool,
    make_set_shortlist_tool,
    make_set_skeleton_plans_tool,
    make_set_transport_options_tool,
    make_set_trip_brief_tool,
)
from tools.plan_tools.trip_basics import make_update_trip_basics_tool


def make_all_plan_tools(plan: TravelPlanState) -> list[ToolDef]:
    """Create all 19 plan-writing tools bound to the given plan instance."""
    return [
        # Category A: high-risk strong-schema tools (Phase 3)
        make_set_skeleton_plans_tool(plan),
        make_select_skeleton_tool(plan),
        make_set_candidate_pool_tool(plan),
        make_set_shortlist_tool(plan),
        make_set_transport_options_tool(plan),
        make_select_transport_tool(plan),
        make_set_accommodation_options_tool(plan),
        make_set_accommodation_tool(plan),
        make_set_risks_tool(plan),
        make_set_alternatives_tool(plan),
        make_set_trip_brief_tool(plan),
        # Category A: daily plans (Phase 5)
        make_append_day_plan_tool(plan),
        make_replace_daily_plans_tool(plan),
        # Category B: phrase-tolerant basics (Phase 1, 3)
        make_update_trip_basics_tool(plan),
        # Category C: append-semantics (Phase 1, 3, 5)
        make_add_preferences_tool(plan),
        make_add_constraints_tool(plan),
        make_add_destination_candidate_tool(plan),
        make_set_destination_candidates_tool(plan),
        # Category D: standalone action
        make_request_backtrack_tool(plan),
    ]
```

> **Note:** design spec Section 4 的表格实际列出了 **19** 个工具（A 类 13 个 + B 类 1 个 + C 类 4 个 + D 类 1 个）。后文所有实现、测试和提交说明都以 **19 ToolDefs / 19 single-responsibility tools** 为准，不再保留 “18 tools” 的旧表述。

- [ ] **Step 2: Create `backend/tests/helpers/__init__.py`**

```python
# backend/tests/helpers/__init__.py
```

(空文件，使 `tests/helpers` 成为包)

- [ ] **Step 3: Create `backend/tests/helpers/register_plan_tools.py`**

```python
# backend/tests/helpers/register_plan_tools.py
from __future__ import annotations

from state.models import TravelPlanState
from tools.engine import ToolEngine
from tools.plan_tools import make_all_plan_tools


def register_all_plan_tools(engine: ToolEngine, plan: TravelPlanState) -> None:
    """Register every plan-writing tool bound to the given plan on the engine."""
    for tool_def in make_all_plan_tools(plan):
        engine.register(tool_def)
```

- [ ] **Step 4: Create `backend/tests/test_plan_tools/test_init.py`**

```python
# backend/tests/test_plan_tools/test_init.py
from __future__ import annotations

import pytest

from state.models import TravelPlanState
from tools.plan_tools import make_all_plan_tools


def _make_plan() -> TravelPlanState:
    return TravelPlanState(session_id="test-init")


class TestMakeAllPlanTools:
    def test_make_all_plan_tools_returns_19_tools(self):
        plan = _make_plan()
        tools = make_all_plan_tools(plan)
        assert len(tools) == 19, (
            f"Expected 19 tools, got {len(tools)}: {[t.name for t in tools]}"
        )

    def test_all_tools_have_unique_names(self):
        plan = _make_plan()
        tools = make_all_plan_tools(plan)
        names = [t.name for t in tools]
        assert len(names) == len(set(names)), (
            f"Duplicate names found: {[n for n in names if names.count(n) > 1]}"
        )

    def test_all_tools_have_human_labels(self):
        plan = _make_plan()
        tools = make_all_plan_tools(plan)
        for t in tools:
            assert t.human_label is not None and len(t.human_label) > 0, (
                f"Tool {t.name!r} is missing human_label"
            )

    def test_all_tools_have_write_side_effect(self):
        plan = _make_plan()
        tools = make_all_plan_tools(plan)
        for t in tools:
            assert t.side_effect == "write", (
                f"Tool {t.name!r} has side_effect={t.side_effect!r}, expected 'write'"
            )

    def test_expected_tool_names_present(self):
        plan = _make_plan()
        tools = make_all_plan_tools(plan)
        names = {t.name for t in tools}
        expected = {
            "set_skeleton_plans",
            "select_skeleton",
            "set_candidate_pool",
            "set_shortlist",
            "set_transport_options",
            "select_transport",
            "set_accommodation_options",
            "set_accommodation",
            "set_risks",
            "set_alternatives",
            "set_trip_brief",
            "append_day_plan",
            "replace_daily_plans",
            "update_trip_basics",
            "add_preferences",
            "add_constraints",
            "add_destination_candidate",
            "set_destination_candidates",
            "request_backtrack",
        }
        assert names == expected, (
            f"Missing: {expected - names}, Extra: {names - expected}"
        )

    def test_all_tools_have_phases(self):
        plan = _make_plan()
        tools = make_all_plan_tools(plan)
        for t in tools:
            assert len(t.phases) > 0, f"Tool {t.name!r} has empty phases list"

    def test_all_tools_are_callable(self):
        plan = _make_plan()
        tools = make_all_plan_tools(plan)
        for t in tools:
            assert callable(t), f"Tool {t.name!r} is not callable"
```

- [ ] **Step 5: Run tests**

```bash
cd backend && python -m pytest tests/test_plan_tools/test_init.py -v
```

Expected:

```
tests/test_plan_tools/test_init.py::TestMakeAllPlanTools::test_make_all_plan_tools_returns_19_tools PASSED
tests/test_plan_tools/test_init.py::TestMakeAllPlanTools::test_all_tools_have_unique_names PASSED
tests/test_plan_tools/test_init.py::TestMakeAllPlanTools::test_all_tools_have_human_labels PASSED
tests/test_plan_tools/test_init.py::TestMakeAllPlanTools::test_all_tools_have_write_side_effect PASSED
tests/test_plan_tools/test_init.py::TestMakeAllPlanTools::test_expected_tool_names_present PASSED
tests/test_plan_tools/test_init.py::TestMakeAllPlanTools::test_all_tools_have_phases PASSED
tests/test_plan_tools/test_init.py::TestMakeAllPlanTools::test_all_tools_are_callable PASSED
```

- [ ] **Step 6: Run full plan_tools test suite to verify no cross-module issues**

```bash
cd backend && python -m pytest tests/test_plan_tools/ -v
```

Expected: All tests across `test_daily_plans.py`, `test_trip_basics.py`, `test_append_tools.py`, `test_backtrack.py`, `test_init.py` (and any earlier Task 1-5 tests) PASS.

- [ ] **Step 7: Commit**

```bash
cd backend && git add \
    tools/plan_tools/__init__.py \
    tests/helpers/__init__.py \
    tests/helpers/register_plan_tools.py \
    tests/test_plan_tools/test_init.py
git commit -m "feat(plan-tools): add __init__.py aggregator, test helper, and integration tests

- make_all_plan_tools() creates all 19 plan-writing ToolDefs bound to a plan
- register_all_plan_tools() helper for batch test migration (Step 4 prep)
- Integration tests verify: count, unique names, human_labels, write side_effect,
  expected tool names, non-empty phases, callability"
```

---

### Task 11: Rewrite `update_plan_state` as adapter (Step 2 — bug fix lands)

**Files:**
- Modify: `backend/tools/update_plan_state.py`
- Modify: `backend/tests/test_update_plan_state.py`
- Create: `backend/tests/test_update_plan_state_strict.py`

This is the critical step. `update_plan_state` keeps its existing tool signature `(field, value)` so the LLM sees no change, but internally adds strict type validation for structured fields and delegates writes to `state/plan_writers`.

- [ ] **Step 1: Define `_STRUCTURED_LIST_FIELDS` and update `_coerce_jsonish`**

In `backend/tools/update_plan_state.py`, add a constant after `_ALLOWED_FIELDS` (line 66):

```python
_STRUCTURED_LIST_FIELDS = {
    "skeleton_plans",
    "candidate_pool",
    "shortlist",
    "transport_options",
    "accommodation_options",
    "risks",
    "alternatives",
    "daily_plans",
}
```

Then replace `_coerce_jsonish` (lines 109-122):

Old:
```python
def _coerce_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text and text[0] in "[{" and text[-1] in "]}":
            try:
                return _coerce_jsonish(json.loads(text))
            except json.JSONDecodeError:
                return value
        return value
    if isinstance(value, list):
        return [_coerce_jsonish(item) for item in value]
    if isinstance(value, dict):
        return {key: _coerce_jsonish(item) for key, item in value.items()}
    return value
```

New:
```python
def _coerce_jsonish(value: Any, *, field: str | None = None) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text and text[0] in "[{" and text[-1] in "]}":
            try:
                return _coerce_jsonish(json.loads(text), field=field)
            except json.JSONDecodeError:
                if field and field in _STRUCTURED_LIST_FIELDS:
                    raise ToolError(
                        f"{field} 的值看起来是 JSON 字符串但解析失败，"
                        "请传原生 JSON 数组而非字符串",
                        error_code="INVALID_VALUE",
                        suggestion="请直接传 list[object]，不要用字符串包裹",
                    )
                return value
        return value
    if isinstance(value, list):
        return [_coerce_jsonish(item, field=field) for item in value]
    if isinstance(value, dict):
        return {key: _coerce_jsonish(item, field=field) for key, item in value.items()}
    return value
```

- [ ] **Step 2: Add strict type validation in the main function body**

In `backend/tools/update_plan_state.py`, replace the single line at line 255:

Old:
```python
        value = _coerce_jsonish(value)
```

New:
```python
        value = _coerce_jsonish(value, field=field)

        # Strict type validation for structured list fields
        if field in _STRUCTURED_LIST_FIELDS:
            if isinstance(value, str):
                raise ToolError(
                    f"{field} 必须是 list[object]，不能是字符串",
                    error_code="INVALID_VALUE",
                    suggestion="请传原生 JSON 数组，不要用引号包裹",
                )
            if isinstance(value, list):
                for i, item in enumerate(value):
                    if not isinstance(item, dict):
                        raise ToolError(
                            f"{field}[{i}] 必须是 object，实际是 {type(item).__name__}",
                            error_code="INVALID_VALUE",
                            suggestion="数组中每个元素必须是 JSON 对象",
                        )
```

- [ ] **Step 3: Remove `phase3_step` from `_ALLOWED_FIELDS` and all handler code**

In `backend/tools/update_plan_state.py`, remove `"phase3_step"` from `_ALLOWED_FIELDS` (line 47).

Remove the `phase3_step` handler branch (lines 297-304):
```python
        elif field == "phase3_step":
            step = str(value)
            if step not in {"brief", "candidate", "skeleton", "lock"}:
                raise ToolError(
                    f"不支持的 phase3_step: {step}",
                    error_code="INVALID_VALUE",
                    suggestion="可选值: brief, candidate, skeleton, lock",
                )
            plan.phase3_step = step
```

Remove `phase3_step` from `_snapshot_field` (lines 33-34):
```python
    if field == "phase3_step":
        return plan.phase3_step
```

Remove `phase3_step` from `_normalize_comparable_value` (lines 168-169):
```python
    if field == "phase3_step":
        return str(value)
```

Remove `phase3_step` from `_current_comparable_value` (lines 186-187):
```python
    if field == "phase3_step":
        return plan.phase3_step
```

- [ ] **Step 4: Delegate writes to `plan_writers` functions**

Add import at top of `backend/tools/update_plan_state.py`:

```python
from state.plan_writers import (
    write_skeleton_plans,
    write_candidate_pool,
    write_shortlist,
    write_transport_options,
    write_accommodation_options,
    write_risks,
    write_alternatives,
    write_trip_brief,
)
```

Replace each structured field's write logic in the main function. Changes summary:

| Field | Old direct mutation | New writer call |
|-------|-------------------|-----------------|
| `skeleton_plans` (list) | `plan.skeleton_plans = value` / `.append(value)` | `write_skeleton_plans(plan, value if isinstance(value, list) else [*plan.skeleton_plans, value])` |
| `candidate_pool` (list) | `plan.candidate_pool = value` / `.append(value)` | `write_candidate_pool(plan, value if isinstance(value, list) else [*plan.candidate_pool, value])` |
| `shortlist` (list) | `plan.shortlist = value` / `.append(value)` | `write_shortlist(plan, value if isinstance(value, list) else [*plan.shortlist, value])` |
| `transport_options` (list) | `plan.transport_options = value` / `.append(value)` | `write_transport_options(plan, value if isinstance(value, list) else [*plan.transport_options, value])` |
| `accommodation_options` (list) | `plan.accommodation_options = value` / `.append(value)` | `write_accommodation_options(plan, value if isinstance(value, list) else [*plan.accommodation_options, value])` |
| `risks` (list) | `plan.risks = value` / `.append(value)` | `write_risks(plan, value if isinstance(value, list) else [*plan.risks, value])` |
| `alternatives` (list) | `plan.alternatives = value` / `.append(value)` | `write_alternatives(plan, value if isinstance(value, list) else [*plan.alternatives, value])` |
| `trip_brief` | `plan.trip_brief.update(value)` | `write_trip_brief(plan, value)` |

Example for `skeleton_plans` (lines 323-327):

Old:
```python
        elif field == "skeleton_plans":
            if isinstance(value, list):
                plan.skeleton_plans = value
            else:
                plan.skeleton_plans.append(value)
```

New:
```python
        elif field == "skeleton_plans":
            if isinstance(value, list):
                write_skeleton_plans(plan, value)
            else:
                write_skeleton_plans(plan, [*plan.skeleton_plans, value])
```

Apply the same pattern for each field in the table above. During the adapter window, even the legacy “append one item” path must rebuild the full list through the writer layer so `update_plan_state` and the new tools stay behaviorally identical.

For `backtrack`, the existing logic at lines 257-285 already delegates to `BacktrackService`; leave as-is.

- [ ] **Step 5: Update existing tests in `test_update_plan_state.py`**

In `backend/tests/test_update_plan_state.py`, rename test at line 146 and add `phase3_step` rejection test:

Old:
```python
@pytest.mark.asyncio
async def test_phase3_structured_fields_accept_json_strings(tool_fn, plan):
```

New:
```python
@pytest.mark.asyncio
async def test_phase3_structured_fields_accept_valid_json_strings(tool_fn, plan):
    """Valid JSON strings are still parsed via _coerce_jsonish.
    Only malformed JSON strings or plain strings are now rejected."""
```

Add at end of file:
```python
@pytest.mark.asyncio
async def test_phase3_step_no_longer_writable(tool_fn):
    """phase3_step was removed from _ALLOWED_FIELDS in Step 2."""
    from tools.base import ToolError

    with pytest.raises(ToolError) as exc_info:
        await tool_fn(field="phase3_step", value="brief")
    assert exc_info.value.error_code == "INVALID_FIELD"
```

- [ ] **Step 6: Create `tests/test_update_plan_state_strict.py`**

New file `backend/tests/test_update_plan_state_strict.py`:

```python
# backend/tests/test_update_plan_state_strict.py
"""Strict validation tests for update_plan_state adapter (Step 2).

These tests verify that structured list fields reject stringified
and malformed inputs that previously caused silent corruption.
"""
import pytest

from state.models import TravelPlanState
from tools.base import ToolError
from tools.update_plan_state import make_update_plan_state_tool


@pytest.fixture
def plan():
    return TravelPlanState(session_id="strict-test")


@pytest.fixture
def tool_fn(plan):
    return make_update_plan_state_tool(plan)


class TestStringifiedStructuredFieldsRejected:
    """After Step 2, passing a plain string to structured list fields
    must raise INVALID_VALUE instead of silently appending."""

    @pytest.mark.asyncio
    async def test_stringified_skeleton_plans_raises(self, tool_fn):
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(
                field="skeleton_plans",
                value="这是一段骨架方案的文字描述",
            )
        assert exc_info.value.error_code == "INVALID_VALUE"
        assert "list[object]" in str(exc_info.value) or "字符串" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_stringified_candidate_pool_raises(self, tool_fn):
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(
                field="candidate_pool",
                value="浅草寺,明治神宫,涩谷",
            )
        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_stringified_shortlist_raises(self, tool_fn):
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(field="shortlist", value="top3 picks")
        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_stringified_transport_options_raises(self, tool_fn):
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(field="transport_options", value="飞机和高铁")
        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_stringified_accommodation_options_raises(self, tool_fn):
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(field="accommodation_options", value="新宿酒店")
        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_stringified_risks_raises(self, tool_fn):
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(field="risks", value="台风季节")
        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_stringified_alternatives_raises(self, tool_fn):
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(field="alternatives", value="备选方案A")
        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_stringified_daily_plans_raises(self, tool_fn):
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(field="daily_plans", value="第一天去浅草寺")
        assert exc_info.value.error_code == "INVALID_VALUE"


class TestNonDictListElementsRejected:
    """Lists containing non-dict elements must be rejected."""

    @pytest.mark.asyncio
    async def test_skeleton_plans_with_string_elements(self, tool_fn):
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(
                field="skeleton_plans",
                value=["plan_a", "plan_b"],
            )
        assert exc_info.value.error_code == "INVALID_VALUE"
        assert "[0]" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_candidate_pool_with_int_elements(self, tool_fn):
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(
                field="candidate_pool",
                value=[1, 2, 3],
            )
        assert exc_info.value.error_code == "INVALID_VALUE"

    @pytest.mark.asyncio
    async def test_mixed_valid_invalid_elements(self, tool_fn):
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(
                field="shortlist",
                value=[{"name": "ok"}, "not_a_dict"],
            )
        assert exc_info.value.error_code == "INVALID_VALUE"
        assert "[1]" in str(exc_info.value)


class TestMalformedJsonStringRejected:
    """Malformed JSON strings for structured fields now raise
    instead of silently returning the raw string."""

    @pytest.mark.asyncio
    async def test_broken_json_skeleton_plans(self, tool_fn):
        broken_json = '[{"id":"plan_a","name":"轻松版","days":[{"day":1'  # truncated
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(field="skeleton_plans", value=broken_json)
        assert exc_info.value.error_code == "INVALID_VALUE"
        assert "解析失败" in str(exc_info.value) or "字符串" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_broken_json_candidate_pool(self, tool_fn):
        broken_json = '[{"name":"浅草寺"'  # truncated
        with pytest.raises(ToolError) as exc_info:
            await tool_fn(field="candidate_pool", value=broken_json)
        assert exc_info.value.error_code == "INVALID_VALUE"


class TestValidInputsStillWork:
    """Ensure valid operations are not broken by strict validation."""

    @pytest.mark.asyncio
    async def test_valid_skeleton_plans_list(self, tool_fn, plan):
        await tool_fn(
            field="skeleton_plans",
            value=[
                {"id": "plan_a", "name": "轻松版", "days": [], "tradeoffs": {}},
                {"id": "plan_b", "name": "平衡版", "days": [], "tradeoffs": {}},
            ],
        )
        assert len(plan.skeleton_plans) == 2
        assert plan.skeleton_plans[0]["id"] == "plan_a"

    @pytest.mark.asyncio
    async def test_valid_json_string_skeleton_plans(self, tool_fn, plan):
        """Valid JSON strings are still parsed by _coerce_jsonish."""
        await tool_fn(
            field="skeleton_plans",
            value='[{"id":"plan_a","name":"轻松版","days":[],"tradeoffs":{}}]',
        )
        assert len(plan.skeleton_plans) == 1
        assert plan.skeleton_plans[0]["id"] == "plan_a"

    @pytest.mark.asyncio
    async def test_valid_single_dict_appended(self, tool_fn, plan):
        """Single dict appends to existing list (unchanged behavior)."""
        await tool_fn(
            field="candidate_pool",
            value={"name": "浅草寺", "area": "浅草"},
        )
        assert len(plan.candidate_pool) == 1

    @pytest.mark.asyncio
    async def test_basic_fields_still_tolerant(self, tool_fn, plan):
        """destination, dates, budget, travelers keep phrase tolerance."""
        await tool_fn(field="destination", value="东京")
        await tool_fn(field="budget", value="1万元")
        await tool_fn(field="travelers", value="2个大人")
        assert plan.destination == "东京"
        assert plan.budget.total == 10000
        assert plan.travelers.adults == 2

    @pytest.mark.asyncio
    async def test_daily_plans_valid_single_dict(self, tool_fn, plan):
        """Single dict for daily_plans still works (append mode)."""
        await tool_fn(
            field="daily_plans",
            value={
                "day": 1,
                "date": "2026-05-01",
                "activities": [
                    {
                        "name": "明治神宫",
                        "location": {"name": "明治神宫", "lat": 35.6764, "lng": 139.6993},
                        "start_time": "09:00",
                        "end_time": "11:00",
                        "category": "shrine",
                        "cost": 0,
                    }
                ],
            },
        )
        assert len(plan.daily_plans) == 1
```

- [ ] **Step 7: Run tests**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && python -m pytest tests/test_update_plan_state.py tests/test_update_plan_state_strict.py -v
```

Expected: All PASS

- [ ] **Step 8: Commit**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && \
git add backend/tools/update_plan_state.py \
        backend/tests/test_update_plan_state.py \
        backend/tests/test_update_plan_state_strict.py && \
git commit -m "feat(tools): rewrite update_plan_state as strict adapter (Step 2)

- Add strict type validation for structured list fields:
  skeleton_plans, candidate_pool, shortlist, transport_options,
  accommodation_options, risks, alternatives, daily_plans
- Stringified values for these fields now raise INVALID_VALUE
- Malformed JSON for structured fields raises instead of silent fallback
- Non-dict list elements are rejected with indexed error messages
- Remove phase3_step from writable fields (system-inferred only)
- Delegate writes to state/plan_writers functions
- Basic fields (destination/dates/budget/travelers) keep phrase tolerance

This permanently prevents the production incident where stringified
skeleton_plans caused AttributeError in infer_phase3_step_from_state.

Part of: split update_plan_state into 19 tools (Task 11/15)"
```

---

### Task 12: Register new tools + update engine whitelist (Step 3 part 1)

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/tools/engine.py`
- Modify: `backend/tools/plan_tools/__init__.py`
- Modify: `backend/tests/test_tool_engine.py`

- [ ] **Step 1: Add `PLAN_WRITER_TOOL_NAMES` and `make_all_plan_tools` to `tools/plan_tools/__init__.py`**

Task 10 already created the `__init__.py` with `make_all_plan_tools`. In this step, ensure `PLAN_WRITER_TOOL_NAMES` is also exported. Add at the top of `backend/tools/plan_tools/__init__.py` (before the `make_all_plan_tools` function):

```python
# All plan-writing tool names (used by guardrail, loop, context_manager)
PLAN_WRITER_TOOL_NAMES: set[str] = {
    "update_plan_state",  # transitional adapter (removed in Step 4)
    "update_trip_basics",
    "set_trip_brief",
    "set_candidate_pool",
    "set_shortlist",
    "set_skeleton_plans",
    "select_skeleton",
    "set_transport_options",
    "select_transport",
    "set_accommodation_options",
    "set_accommodation",
    "set_risks",
    "set_alternatives",
    "add_preferences",
    "add_constraints",
    "add_destination_candidate",
    "set_destination_candidates",
    "append_day_plan",
    "replace_daily_plans",
    "request_backtrack",
}
```

- [ ] **Step 2: Add import and registration in `main.py`**

In `backend/main.py`, add import after existing line 72 (`from tools.update_plan_state import make_update_plan_state_tool`):

```python
from tools.plan_tools import make_all_plan_tools
```

Then after line 420 (`tool_engine.register(make_update_plan_state_tool(plan))`), add:

```python
        # Register all 19 single-responsibility plan-writing tools (Step 3)
        for plan_tool in make_all_plan_tools(plan):
            tool_engine.register(plan_tool)
```

- [ ] **Step 3: Update `_phase3_tool_names` in `engine.py`**

In `backend/tools/engine.py`, replace `_phase3_tool_names` method (lines 43-81):

Old:
```python
    def _phase3_tool_names(self, step: str) -> set[str]:
        step_order = {
            "brief": {
                "update_plan_state",
                "web_search",
                "xiaohongshu_search",
            },
            "candidate": {
                "update_plan_state",
                "web_search",
                "xiaohongshu_search",
                "quick_travel_search",
                "get_poi_info",
            },
            "skeleton": {
                "update_plan_state",
                "web_search",
                "xiaohongshu_search",
                "quick_travel_search",
                "get_poi_info",
                "calculate_route",
                "assemble_day_plan",
                "check_availability",
            },
            "lock": {
                "update_plan_state",
                "web_search",
                "xiaohongshu_search",
                "quick_travel_search",
                "get_poi_info",
                "calculate_route",
                "assemble_day_plan",
                "check_availability",
                "search_flights",
                "search_trains",
                "search_accommodations",
            },
        }
        return step_order.get(step, step_order["brief"])
```

New:
```python
    def _phase3_tool_names(self, step: str) -> set[str]:
        # Common plan-writing tools available in all Phase 3 sub-stages
        _common_plan_writers = {
            "update_plan_state",      # transitional adapter (removed in Step 4)
            "update_trip_basics",
            "request_backtrack",
        }

        step_order = {
            "brief": {
                *_common_plan_writers,
                "set_trip_brief",
                "add_preferences",
                "add_constraints",
                "web_search",
                "xiaohongshu_search",
            },
            "candidate": {
                *_common_plan_writers,
                "set_trip_brief",
                "set_candidate_pool",
                "set_shortlist",
                "add_preferences",
                "add_constraints",
                "web_search",
                "xiaohongshu_search",
                "quick_travel_search",
                "get_poi_info",
            },
            "skeleton": {
                *_common_plan_writers,
                "set_skeleton_plans",
                "select_skeleton",
                "set_candidate_pool",
                "set_shortlist",
                "add_preferences",
                "add_constraints",
                "web_search",
                "xiaohongshu_search",
                "quick_travel_search",
                "get_poi_info",
                "calculate_route",
                "assemble_day_plan",
                "check_availability",
            },
            "lock": {
                *_common_plan_writers,
                "set_skeleton_plans",
                "select_skeleton",
                "set_transport_options",
                "select_transport",
                "set_accommodation_options",
                "set_accommodation",
                "set_risks",
                "set_alternatives",
                "add_preferences",
                "add_constraints",
                "web_search",
                "xiaohongshu_search",
                "quick_travel_search",
                "get_poi_info",
                "calculate_route",
                "assemble_day_plan",
                "check_availability",
                "search_flights",
                "search_trains",
                "search_accommodations",
            },
        }
        return step_order.get(step, step_order["brief"])
```

- [ ] **Step 4: Update `_phase3_builtin_tool_names` in `engine.py`**

In `backend/tools/engine.py`, replace `_phase3_builtin_tool_names` method (lines 83-96):

Old:
```python
    def _phase3_builtin_tool_names(self) -> set[str]:
        return {
            "update_plan_state",
            "web_search",
            "xiaohongshu_search",
            "quick_travel_search",
            "get_poi_info",
            "calculate_route",
            "assemble_day_plan",
            "check_availability",
            "search_flights",
            "search_trains",
            "search_accommodations",
        }
```

New:
```python
    def _phase3_builtin_tool_names(self) -> set[str]:
        return {
            "update_plan_state",
            # New plan-writing tools
            "update_trip_basics",
            "set_trip_brief",
            "set_candidate_pool",
            "set_shortlist",
            "set_skeleton_plans",
            "select_skeleton",
            "set_transport_options",
            "select_transport",
            "set_accommodation_options",
            "set_accommodation",
            "set_risks",
            "set_alternatives",
            "add_preferences",
            "add_constraints",
            "request_backtrack",
            # Existing tools
            "web_search",
            "xiaohongshu_search",
            "quick_travel_search",
            "get_poi_info",
            "calculate_route",
            "assemble_day_plan",
            "check_availability",
            "search_flights",
            "search_trains",
            "search_accommodations",
        }
```

- [ ] **Step 5: Write tests for engine whitelist updates**

Append to `backend/tests/test_tool_engine.py`:

```python
class TestEnginePhase3NewTools:
    """Verify new plan-writing tools appear in correct Phase 3 sub-stage whitelists."""

    def test_engine_phase3_brief_includes_set_trip_brief(self):
        engine = ToolEngine()
        names = engine._phase3_tool_names("brief")
        assert "set_trip_brief" in names
        assert "update_trip_basics" in names
        assert "add_preferences" in names
        assert "set_skeleton_plans" not in names  # not available in brief

    def test_engine_phase3_candidate_includes_candidate_tools(self):
        engine = ToolEngine()
        names = engine._phase3_tool_names("candidate")
        assert "set_candidate_pool" in names
        assert "set_shortlist" in names

    def test_engine_phase3_skeleton_includes_set_skeleton_plans(self):
        engine = ToolEngine()
        names = engine._phase3_tool_names("skeleton")
        assert "set_skeleton_plans" in names
        assert "select_skeleton" in names

    def test_engine_phase3_lock_includes_set_accommodation(self):
        engine = ToolEngine()
        names = engine._phase3_tool_names("lock")
        assert "set_accommodation" in names
        assert "set_transport_options" in names
        assert "select_transport" in names
        assert "set_risks" in names
        assert "set_alternatives" in names
        assert "search_flights" in names

    def test_engine_phase3_builtin_names_includes_all_new_tools(self):
        engine = ToolEngine()
        builtin = engine._phase3_builtin_tool_names()
        new_tools = {
            "update_trip_basics", "set_trip_brief", "set_candidate_pool",
            "set_shortlist", "set_skeleton_plans", "select_skeleton",
            "set_transport_options", "select_transport",
            "set_accommodation_options", "set_accommodation",
            "set_risks", "set_alternatives", "add_preferences",
            "add_constraints", "request_backtrack",
        }
        assert new_tools.issubset(builtin)
```

- [ ] **Step 6: Run tests**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && python -m pytest tests/test_tool_engine.py -v -k "Phase3NewTools"
```

Expected: All PASS

- [ ] **Step 7: Commit**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && \
git add backend/main.py \
        backend/tools/engine.py \
        backend/tools/plan_tools/__init__.py \
        backend/tests/test_tool_engine.py && \
git commit -m "feat(engine): register 19 new plan-writing tools + update Phase 3 whitelists (Step 3.1)

- Add PLAN_WRITER_TOOL_NAMES set for cross-module reference
- Register all new plan tools alongside existing update_plan_state
- Update _phase3_tool_names with per-sub-stage visibility:
  brief: set_trip_brief, add_preferences, add_constraints
  candidate: +set_candidate_pool, +set_shortlist
  skeleton: +set_skeleton_plans, +select_skeleton
  lock: +transport/accommodation/risks/alternatives tools
- Update _phase3_builtin_tool_names to include all 15 new Phase 3 tools

Part of: split update_plan_state into 19 tools (Task 12/15)"
```

---

### Task 13: Migrate prompts (Step 3 part 2)

**Files:**
- Modify: `backend/phase/prompts.py`
- Modify: `backend/tests/test_prompt_architecture.py`
- Modify: `backend/evals/golden_cases/failure-005-multi-constraint.yaml`
- Modify: `backend/evals/golden_cases/failure-008-greedy-itinerary.yaml`
- Modify: `backend/tests/test_eval_pipeline.py`

- [ ] **Step 1: Update `GLOBAL_RED_FLAGS` — 2 replacements**

In `backend/phase/prompts.py`:

Replacement 1 (line 7):

Old:
```
- 你在正文中给出了候选池、骨架方案或逐日行程，但没有通过 update_plan_state 写入状态。
```

New:
```
- 你在正文中给出了候选池、骨架方案或逐日行程，但没有通过状态写入工具写入状态。
```

Replacement 2 (line 11):

Old:
```
- 用户要求推翻前序决策，你没有使用 update_plan_state(field="backtrack", ...)。
```

New:
```
- 用户要求推翻前序决策，你没有使用 `request_backtrack(to_phase=..., reason="...")`。
```

- [ ] **Step 2: Update `PHASE1_PROMPT` — 3 replacements**

Replacement 1 — state write contract (line 66):

Old:
```
- 用户明确拍板目的地后，立即调用 `update_plan_state(field="destination", value="目的地名称")`，value 为纯字符串。
```

New:
```
- 用户明确拍板目的地后，立即调用 `update_trip_basics(destination="目的地名称")`。
```

Replacement 2 — completion gate (line 74):

Old:
```
- 已调用 `update_plan_state(field="destination", value="...")` 写入。
```

New:
```
- 已调用 `update_trip_basics(destination="...")` 写入。
```

Replacement 3 — pressure scenario D (line 112):

Old:
```
正确：立即调用 update_plan_state 写入 destination、budget、travelers，自然结束阶段 1。
```

New:
```
正确：立即调用 update_trip_basics 写入 destination、budget、travelers，自然结束阶段 1。
```

- [ ] **Step 3: Update `PHASE3_STEP_PROMPTS["brief"]` — 1 replacement**

Line 200:

Old:
```
- 当你已经拿到足够信息形成旅行画像后，调用 `update_plan_state(field="trip_brief", value={...})` 写入 brief。
```

New:
```
- 当你已经拿到足够信息形成旅行画像后，调用 `set_trip_brief(fields={...})` 写入 brief。
```

- [ ] **Step 4: Update `PHASE3_STEP_PROMPTS["skeleton"]` — 1 replacement**

Line 327:

Old:
```
- 用户明确选中某一套后，调用 `update_plan_state(field="selected_skeleton_id", value="...")`，value 必须精确等于骨架的 `id` 字段。
```

New:
```
- 用户明确选中某一套后，调用 `select_skeleton(id="...")`，id 必须精确等于骨架的 `id` 字段。
```

- [ ] **Step 5: Update `PHASE5_PROMPT` — 7 replacements**

Replacement 1 (line 412):

Old:
```
- 每完成 1-2 天的行程就调用 update_plan_state 写入 daily_plans，让用户即时看到进度并给反馈。
```

New:
```
- 每完成 1-2 天的行程就调用 `replace_daily_plans` 或 `append_day_plan` 写入 daily_plans，让用户即时看到进度并给反馈。
```

Replacement 2 (line 425):

Old:
```
如果前置条件明显不完整或骨架不可执行，不要硬排假行程；应指出问题并在必要时调用 update_plan_state(field="backtrack", value={"to_phase": 3, "reason": "..."}) 回退。
```

New:
```
如果前置条件明显不完整或骨架不可执行，不要硬排假行程；应指出问题并在必要时调用 `request_backtrack(to_phase=3, reason="...")` 回退。
```

Replacement 3 (line 454):

Old:
```
- 每完成 1-2 天就调用 update_plan_state(field="daily_plans", value=...) 写入
```

New:
```
- 每完成 1-2 天就调用 `append_day_plan(...)` 追加或 `replace_daily_plans(days=[...])` 批量写入
```

Replacement 4 (line 460):

Old:
```
调用 update_plan_state(field="daily_plans", value=...) 时必须遵守：
```

New:
```
调用 `append_day_plan` 或 `replace_daily_plans` 时必须遵守以下 DayPlan 结构：
```

Replacement 5 (line 495):

Old:
```
- update_plan_state：写入 daily_plans，必要时执行回退
```

New:
```
- append_day_plan / replace_daily_plans：写入逐日行程
- request_backtrack：必要时执行阶段回退
```

Replacement 6 (line 507):

Old:
```
- 行程数据必须通过 update_plan_state(field="daily_plans", ...) 写入，不允许只在正文描述而不写状态。
```

New:
```
- 行程数据必须通过 `append_day_plan` 或 `replace_daily_plans` 写入，不允许只在正文描述而不写状态。
```

Replacement 7 (line 522):

Old:
```
- 你生成了逐日行程但没有调用 update_plan_state 写入 daily_plans。
```

New:
```
- 你生成了逐日行程但没有调用 `append_day_plan` 或 `replace_daily_plans` 写入 daily_plans。
```

- [ ] **Step 6: Update `PHASE7_PROMPT` — 1 replacement**

Line 627:

Old:
```
- 如果发现行程有严重问题需要回退，调用 update_plan_state(field="backtrack", ...) 而非擅自修改。
```

New:
```
- 如果发现行程有严重问题需要回退，调用 `request_backtrack(to_phase=..., reason="...")` 而非擅自修改。
```

- [ ] **Step 7: Write prompt migration tests**

Append to `backend/tests/test_prompt_architecture.py` (ensure imports include all needed constants):

```python
from phase.prompts import (
    GLOBAL_RED_FLAGS,
    PHASE1_PROMPT,
    PHASE3_BASE_PROMPT,
    PHASE3_STEP_PROMPTS,
    PHASE5_PROMPT,
    PHASE7_PROMPT,
    PHASE_PROMPTS,
)


class TestNoUpdatePlanStateInPrompts:
    """After Step 3, prompts must not reference update_plan_state as a tool call."""

    def test_no_update_plan_state_call_in_phase1(self):
        assert "update_plan_state(" not in PHASE1_PROMPT
        assert "update_plan_state(field=" not in PHASE1_PROMPT

    def test_no_update_plan_state_call_in_phase3_base(self):
        assert "update_plan_state(" not in PHASE3_BASE_PROMPT

    def test_no_update_plan_state_call_in_phase3_steps(self):
        for step_name, step_prompt in PHASE3_STEP_PROMPTS.items():
            assert "update_plan_state(" not in step_prompt, (
                f"Phase 3 sub-stage '{step_name}' still references update_plan_state("
            )

    def test_no_update_plan_state_call_in_phase5(self):
        assert "update_plan_state(" not in PHASE5_PROMPT
        assert "update_plan_state" not in PHASE5_PROMPT

    def test_no_update_plan_state_call_in_phase7(self):
        assert "update_plan_state(" not in PHASE7_PROMPT

    def test_no_update_plan_state_call_in_global_red_flags(self):
        assert "update_plan_state(" not in GLOBAL_RED_FLAGS

    def test_phase3_skeleton_prompt_mentions_select_skeleton(self):
        skeleton = PHASE3_STEP_PROMPTS["skeleton"]
        assert "select_skeleton" in skeleton

    def test_phase3_brief_prompt_mentions_set_trip_brief(self):
        brief = PHASE3_STEP_PROMPTS["brief"]
        assert "set_trip_brief" in brief

    def test_phase5_mentions_append_day_plan(self):
        assert "append_day_plan" in PHASE5_PROMPT

    def test_phase5_mentions_replace_daily_plans(self):
        assert "replace_daily_plans" in PHASE5_PROMPT

    def test_phase5_mentions_request_backtrack(self):
        assert "request_backtrack" in PHASE5_PROMPT

    def test_phase7_mentions_request_backtrack(self):
        assert "request_backtrack" in PHASE7_PROMPT

    def test_phase1_mentions_update_trip_basics(self):
        assert "update_trip_basics" in PHASE1_PROMPT

    def test_global_red_flags_mentions_request_backtrack(self):
        assert "request_backtrack" in GLOBAL_RED_FLAGS
```

- [ ] **Step 8: Migrate golden-case eval assertions**

Update the two golden cases that still assert `update_plan_state` as the generic state-write tool:

```yaml
# backend/evals/golden_cases/failure-005-multi-constraint.yaml
assertions:
  - type: contains_text
    target: 素食
  - type: tool_called
    target: update_trip_basics
```

```yaml
# backend/evals/golden_cases/failure-008-greedy-itinerary.yaml
assertions:
  - type: contains_text
    target: 紧凑
  - type: tool_called
    target: update_trip_basics
```

Then update `backend/tests/test_eval_pipeline.py` to validate against the exported tool-name set instead of a stale hard-coded list. Add import near the top:

```python
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES
```

Replace `test_golden_cases_use_registered_tool_names` with:

```python
    def test_golden_cases_use_registered_tool_names(self):
        cases = load_golden_cases(Path("evals/golden_cases"))
        known_tools = PLAN_WRITER_TOOL_NAMES | {
            "search_flights",
            "search_trains",
            "ai_travel_search",
            "search_accommodations",
            "get_poi_info",
            "calculate_route",
            "assemble_day_plan",
            "check_availability",
            "check_weather",
            "generate_summary",
            "quick_travel_search",
            "search_travel_services",
            "web_search",
            "xiaohongshu_search",
        }
        bad_targets = [
            (case.id, assertion.target)
            for case in cases
            for assertion in case.assertions
            if assertion.type in {AssertionType.TOOL_CALLED, AssertionType.TOOL_NOT_CALLED}
            and assertion.target not in known_tools
        ]

        assert bad_targets == []
```

- [ ] **Step 9: Run tests + eval smoke check**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && \
python -m pytest tests/test_prompt_architecture.py -v -k "NoUpdatePlanState" && \
python -m pytest tests/test_eval_pipeline.py -v -k "registered_tool_names" && \
TMPDIR=$(mktemp -d) && \
python ../scripts/eval-stability.py --mock --cases failure-005,failure-008 --k 2 --output "$TMPDIR/step3-eval"
```

Expected:

```text
tests/test_prompt_architecture.py::TestNoUpdatePlanStateInPrompts::... PASSED
tests/test_eval_pipeline.py::TestLoadGoldenCases::test_golden_cases_use_registered_tool_names PASSED
Loaded 2 golden case(s)
Running pass@2 stability evaluation
```

- [ ] **Step 10: Commit**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && \
git add backend/phase/prompts.py \
        backend/tests/test_prompt_architecture.py \
        backend/evals/golden_cases/failure-005-multi-constraint.yaml \
        backend/evals/golden_cases/failure-008-greedy-itinerary.yaml \
        backend/tests/test_eval_pipeline.py && \
git commit -m "feat(prompts): migrate all update_plan_state references to new tool names (Step 3.2)

- GLOBAL_RED_FLAGS: generic '状态写入工具' + request_backtrack
- Phase 1: update_plan_state → update_trip_basics (3 occurrences)
- Phase 3 brief: → set_trip_brief
- Phase 3 skeleton: → select_skeleton
- Phase 5: → append_day_plan / replace_daily_plans / request_backtrack (7 occurrences)
- Phase 7: → request_backtrack
- Golden cases failure-005 / failure-008: update_plan_state → update_trip_basics
- test_eval_pipeline now validates against PLAN_WRITER_TOOL_NAMES
- Add 16 tests asserting zero 'update_plan_state(' in all prompts
  and verifying new tool names appear in correct phase prompts

Part of: split update_plan_state into 19 tools (Task 13/15)"
```

---

### Task 14: Migrate guardrail, loop, reflection, tool_choice, context_manager (Step 3 part 3)

**Files:**
- Modify: `backend/agent/loop.py`
- Modify: `backend/agent/reflection.py`
- Modify: `backend/agent/tool_choice.py`
- Modify: `backend/harness/guardrail.py`
- Modify: `backend/context/manager.py`
- Modify: `backend/tests/test_guardrail.py`
- Modify: `backend/tests/test_context_manager.py`
- Modify: `backend/tests/test_reflection.py`

- [ ] **Step 1: Migrate `agent/loop.py` — import + `saw_state_update` checks**

Change import (line 18):

Old:
```python
from tools.update_plan_state import is_redundant_update_plan_state
```

New:
```python
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES
from tools.update_plan_state import is_redundant_update_plan_state
```

Change batch `saw_state_update` check (lines 288-292):

Old:
```python
                                if (
                                    batch_tc.name == "update_plan_state"
                                    and result.status == "success"
                                ):
                                    saw_state_update = True
```

New:
```python
                                if (
                                    batch_tc.name in PLAN_WRITER_TOOL_NAMES
                                    and result.status == "success"
                                ):
                                    saw_state_update = True
```

Change single `saw_state_update` check (lines 334-338):

Old:
```python
                        if (
                            tc.name == "update_plan_state"
                            and result.status == "success"
                        ):
                            saw_state_update = True
```

New:
```python
                        if (
                            tc.name in PLAN_WRITER_TOOL_NAMES
                            and result.status == "success"
                        ):
                            saw_state_update = True
```

- [ ] **Step 2: Migrate `agent/loop.py` — 4 repair messages**

Replacement 1 — brief repair (lines 725-729):

Old:
```python
            return (
                "[状态同步提醒]\n"
                "你刚刚已经完成了旅行画像说明，但 `trip_brief` 仍为空。"
                '请先调用 `update_plan_state(field="trip_brief", value={...})`'
                " 写入结构化 brief；如果日期、预算、人数、偏好、约束是用户明确说过的，也要补写对应状态。"
                "写完后再继续，不要重复整段面向用户解释。"
            )
```

New:
```python
            return (
                "[状态同步提醒]\n"
                "你刚刚已经完成了旅行画像说明，但 `trip_brief` 仍为空。"
                "请先调用 `set_trip_brief(fields={...})`"
                " 写入结构化 brief；如果日期、预算、人数、偏好、约束是用户明确说过的，"
                "也要用 `update_trip_basics` 和 `add_preferences` / `add_constraints` 补写。"
                "写完后再继续，不要重复整段面向用户解释。"
            )
```

Replacement 2 — candidate repair (lines 740-744):

Old:
```python
            return (
                "[状态同步提醒]\n"
                "你刚刚已经给出了候选筛选结果，但 `candidate_pool` / `shortlist` 仍为空。"
                "请先调用 `update_plan_state` 把候选全集写入 `candidate_pool`，把第一轮筛选结果写入 `shortlist`。"
                "写入 shortlist 后系统会自动推进子阶段。"
            )
```

New:
```python
            return (
                "[状态同步提醒]\n"
                "你刚刚已经给出了候选筛选结果，但 `candidate_pool` / `shortlist` 仍为空。"
                "请先调用 `set_candidate_pool(pool=[...])` 写入候选全集，"
                "再调用 `set_shortlist(items=[...])` 写入第一轮筛选结果。"
                "写入 shortlist 后系统会自动推进子阶段。"
            )
```

Replacement 3 — skeleton repair (lines 756-761):

Old:
```python
            return (
                "[状态同步提醒]\n"
                "你刚刚已经给出了 2-3 套骨架方案，但 `skeleton_plans` 仍为空。"
                '请先调用 `update_plan_state(field="skeleton_plans", value=[...])`'
                " 写入结构化骨架方案列表（传 list 整体替换）。"
                "如果用户已经明确选中某套方案，再写 `selected_skeleton_id`，系统会自动推进到 lock 子阶段。"
            )
```

New:
```python
            return (
                "[状态同步提醒]\n"
                "你刚刚已经给出了 2-3 套骨架方案，但 `skeleton_plans` 仍为空。"
                "请先调用 `set_skeleton_plans(plans=[...])`"
                " 写入结构化骨架方案列表。"
                '如果用户已经明确选中某套方案，再调用 `select_skeleton(id="...")`，'
                "系统会自动推进到 lock 子阶段。"
            )
```

Replacement 4 — Phase 5 daily_plans repair (lines 849-853):

Old:
```python
            return (
                "[状态同步提醒]\n"
                f"你刚刚已经给出了逐日行程安排，但 `daily_plans` 仍只有 {planned_count}/{total_days} 天。"
                f"还需要写入 {remaining} 天的行程。"
                '请立即调用 `update_plan_state(field="daily_plans", value=[...])` '
                "把你刚才描述的每一天行程以结构化 JSON 写入。"
                "每天必须包含 day、date、activities（含 name/location/start_time/end_time/category/cost）。"
                "可以一次性传入 list[dict] 写入全部天数，也可以逐天传入单个 dict 追加。"
            )
```

New:
```python
            return (
                "[状态同步提醒]\n"
                f"你刚刚已经给出了逐日行程安排，但 `daily_plans` 仍只有 {planned_count}/{total_days} 天。"
                f"还需要写入 {remaining} 天的行程。"
                "请立即调用 `replace_daily_plans(days=[...])` 批量写入全部天数，"
                "或调用 `append_day_plan(...)` 逐天追加。"
                "每天必须包含 day、date、activities（含 name/location/start_time/end_time/category/cost）。"
            )
```

- [ ] **Step 3: Migrate `agent/loop.py` — `_should_skip_redundant_update`**

Replace lines 856-866:

Old:
```python
    def _should_skip_redundant_update(self, tool_call: ToolCall) -> bool:
        if tool_call.name != "update_plan_state" or self.plan is None:
            return False
        field = tool_call.arguments.get("field")
        if not isinstance(field, str):
            return False
        return is_redundant_update_plan_state(
            self.plan,
            field=field,
            value=tool_call.arguments.get("value"),
        )
```

New:
```python
    def _should_skip_redundant_update(self, tool_call: ToolCall) -> bool:
        if self.plan is None:
            return False
        # Legacy adapter path
        if tool_call.name == "update_plan_state":
            field = tool_call.arguments.get("field")
            if not isinstance(field, str):
                return False
            return is_redundant_update_plan_state(
                self.plan,
                field=field,
                value=tool_call.arguments.get("value"),
            )
        # New single-responsibility tools have clear semantics;
        # redundancy checks are less needed. Skip for now.
        return False
```

- [ ] **Step 4: Simplify `agent/tool_choice.py`**

Replace entire file `backend/agent/tool_choice.py`:

Old (full file):
```python
from __future__ import annotations

import re
from typing import Any

from agent.types import Message, Role
from state.models import TravelPlanState

_FORCED = {"type": "function", "function": {"name": "update_plan_state"}}
...
```

New:
```python
from __future__ import annotations

from typing import Any

from agent.types import Message
from state.models import TravelPlanState


class ToolChoiceDecider:
    """Decides tool_choice parameter for LLM calls.

    After the migration to single-responsibility plan-writing tools,
    hard-forcing a specific tool is no longer appropriate because the
    LLM must choose among multiple tools. Always return "auto" and
    rely on prompt discipline instead.

    If eval metrics show the LLM skipping state writes too often,
    reintroduce context-aware selection in a follow-up.
    """

    def decide(
        self, plan: TravelPlanState, messages: list[Message], phase: int
    ) -> str | dict[str, Any]:
        return "auto"
```

- [ ] **Step 5: Migrate `harness/guardrail.py` — budget validation**

In `backend/harness/guardrail.py`, replace lines 108-121:

Old:
```python
        if (
            not self._is_disabled("invalid_budget")
            and tc.name == "update_plan_state"
            and tc.arguments.get("field") == "budget"
        ):
            value = tc.arguments.get("value")
            if isinstance(value, dict):
                total = value.get("total")
                if isinstance(total, (int, float)) and total <= 0:
                    return GuardrailResult(
                        allowed=False,
                        reason="budget.total 不能为负数或零",
                        level="error",
                    )
```

New:
```python
        if not self._is_disabled("invalid_budget"):
            budget_value = None
            if tc.name == "update_plan_state" and tc.arguments.get("field") == "budget":
                budget_value = tc.arguments.get("value")
            elif tc.name == "update_trip_basics" and "budget" in tc.arguments:
                budget_value = tc.arguments.get("budget")

            if isinstance(budget_value, dict):
                total = budget_value.get("total")
                if isinstance(total, (int, float)) and total <= 0:
                    return GuardrailResult(
                        allowed=False,
                        reason="budget.total 不能为负数或零",
                        level="error",
                    )
```

- [ ] **Step 6: Migrate `context/manager.py` — system prompt**

In `backend/context/manager.py`, replace lines 78-87:

Old:
```python
            "## 工具使用硬规则\n\n"
            "- 当用户提供了明确的规划信息（目的地、日期、预算、人数、偏好、约束、住宿、候选地等）时，如果这些信息尚未写入当前规划状态，或是在修改已有值，必须先调用 `update_plan_state` 写入状态，不能只在自然语言里复述。\n"
            "- 同一条用户消息里如果包含多个字段，可以连续调用多次 `update_plan_state`。\n"
            "- 如果某个字段已经准确体现在“当前规划状态”里，不要重复调用 `update_plan_state` 写入相同值。\n"
```

New:
```python
            "## 工具使用硬规则\n\n"
            "- 当用户提供了明确的规划信息（目的地、日期、预算、人数、偏好、约束、住宿、候选地等）时，如果这些信息尚未写入当前规划状态，或是在修改已有值，必须先调用对应的状态写入工具写入状态，不能只在自然语言里复述。\n"
            "- 同一条用户消息里如果包含多个字段，可以连续调用多个状态写入工具。\n"
            "- 如果某个字段已经准确体现在“当前规划状态”里，不要重复写入相同值。\n"
```

Also replace the backtrack rule in the same block (line 85):

Old:
```python
            "- 当用户要求推翻之前的阶段决策时，必须使用 `update_plan_state(field=\"backtrack\", value={...})`。\n"
```

New:
```python
            "- 当用户要求推翻之前的阶段决策时，必须使用 `request_backtrack(to_phase=..., reason=\"...\")`。\n"
```

- [ ] **Step 7: Migrate `agent/reflection.py` — remove legacy tool name**

In `backend/agent/reflection.py`, replace the Phase 5 completion prompt tail:

Old:
```python
            "如果发现问题，调用 update_plan_state 修正。如果没有问题，继续。"
```

New:
```python
            "如果发现问题，优先调用 `append_day_plan` / `replace_daily_plans` 修正；"
            "如果问题意味着需要回到上游重新决策，调用 `request_backtrack`。"
            "如果没有问题，继续。"
```

- [ ] **Step 8: Migrate `context/manager.py` — compaction rendering**

Add import at top of `backend/context/manager.py`:
```python
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES as _PLAN_WRITER_NAMES
```

In `_render_tool_event`, add a new branch after the existing `update_plan_state` block (after the closing `.strip()` at line 376, before the `if result.status == "success":` at line 378):

```python
        # New plan-writing tools — render as decisions too
        if tool_call and tool_call.name in _PLAN_WRITER_NAMES:
            args_preview = self._short_repr(tool_call.arguments)
            if result.status == "success":
                return f"决策: {tool_call.name} {args_preview}"
            if result.status == "skipped":
                return f"跳过: {tool_call.name}（{result.error_code or 'skipped'}）"
            return (
                f"失败: {tool_call.name} — {result.error_code or ''} "
                f"{(result.error or '').strip()}"
            ).strip()
```

- [ ] **Step 9: Update `tests/test_guardrail.py`**

Append to `backend/tests/test_guardrail.py`:

```python
def test_negative_budget_rejected_via_update_trip_basics(guardrail):
    tc = ToolCall(
        id="1",
        name="update_trip_basics",
        arguments={"budget": {"total": -500}},
    )
    result = guardrail.validate_input(tc)
    assert not result.allowed
    assert "负" in result.reason or "零" in result.reason


def test_valid_budget_via_update_trip_basics(guardrail):
    tc = ToolCall(
        id="1",
        name="update_trip_basics",
        arguments={"budget": {"total": 10000}, "destination": "东京"},
    )
    result = guardrail.validate_input(tc)
    assert result.allowed
```

- [ ] **Step 10: Update `tests/test_context_manager.py`**

Update assertions (lines 35-36):

Old:
```python
    assert "必须先调用 `update_plan_state`" in msg.content
    assert "不要重复调用 `update_plan_state` 写入相同值" in msg.content
```

New:
```python
    assert "必须先调用对应的状态写入工具" in msg.content
    assert "不要重复写入相同值" in msg.content
```

- [ ] **Step 11: Update `tests/test_reflection.py`**

Append:

```python
def test_phase5_complete_prompt_uses_new_tools(injector):
    from state.models import DayPlan, DateRange

    plan = _make_plan(
        phase=5,
        dates=DateRange(start="2026-04-10", end="2026-04-12"),
        daily_plans=[
            DayPlan(day=1, date="2026-04-10"),
            DayPlan(day=2, date="2026-04-11"),
        ],
    )
    result = injector.check_and_inject(messages=[], plan=plan, prev_step=None)
    assert result is not None
    assert "update_plan_state" not in result
    assert "append_day_plan" in result
    assert "request_backtrack" in result
```

- [ ] **Step 12: Run tests**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && python -m pytest tests/test_guardrail.py tests/test_context_manager.py tests/test_reflection.py tests/test_agent_loop.py -v --timeout=30
```

Expected: All PASS

- [ ] **Step 13: Commit**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && \
git add backend/agent/loop.py \
        backend/agent/reflection.py \
        backend/agent/tool_choice.py \
        backend/harness/guardrail.py \
        backend/context/manager.py \
        backend/tests/test_guardrail.py \
        backend/tests/test_context_manager.py \
        backend/tests/test_reflection.py && \
git commit -m "feat(agent): migrate guardrail, loop, reflection, tool_choice, context_manager to new tools (Step 3.3)

- loop.py: saw_state_update checks all PLAN_WRITER_TOOL_NAMES
- loop.py: 4 repair messages now reference set_trip_brief,
  set_candidate_pool, set_skeleton_plans, select_skeleton,
  replace_daily_plans, append_day_plan
- reflection.py: Phase 5 self-check now points to append_day_plan /
  replace_daily_plans / request_backtrack instead of update_plan_state
- tool_choice.py: remove _FORCED, always return 'auto'
- guardrail.py: budget validation covers update_trip_basics
- context/manager.py: system prompt uses generic phrasing;
  compaction renders new tool names as '决策' lines

Part of: split update_plan_state into 19 tools (Task 14/15)"
```

---

### Task 15: Remove `update_plan_state` + migrate tests (Step 4)

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/tools/engine.py`
- Modify: `backend/agent/loop.py`
- Modify: `backend/context/manager.py`
- Modify: `backend/tools/plan_tools/__init__.py`
- Delete: `backend/tools/update_plan_state.py`
- Modify: 30+ test files
- Modify: `PROJECT_OVERVIEW.md`
- Modify: `docs/superpowers/specs/2026-04-04-backtrack-into-update-plan-state-design.md`

- [ ] **Step 1: Remove `update_plan_state` from `main.py`**

Remove import (line 72):
```python
from tools.update_plan_state import make_update_plan_state_tool
```

Remove registration (around line 420):
```python
        tool_engine.register(make_update_plan_state_tool(plan))
```

Keep only the `make_all_plan_tools` registration block.

- [ ] **Step 2: Remove from engine whitelists**

In `backend/tools/engine.py` `_phase3_tool_names`, remove from `_common_plan_writers`:

Old:
```python
        _common_plan_writers = {
            "update_plan_state",      # transitional adapter (removed in Step 4)
            "update_trip_basics",
            "request_backtrack",
        }
```

New:
```python
        _common_plan_writers = {
            "update_trip_basics",
            "request_backtrack",
        }
```

In `_phase3_builtin_tool_names`, remove `"update_plan_state",`.

- [ ] **Step 3: Remove from `PLAN_WRITER_TOOL_NAMES`**

In `backend/tools/plan_tools/__init__.py`, remove:
```python
    "update_plan_state",  # transitional adapter (removed in Step 4)
```

- [ ] **Step 4: Clean up `agent/loop.py`**

Remove import:
```python
from tools.update_plan_state import is_redundant_update_plan_state
```

Replace `_should_skip_redundant_update`:

Old:
```python
    def _should_skip_redundant_update(self, tool_call: ToolCall) -> bool:
        if self.plan is None:
            return False
        # Legacy adapter path
        if tool_call.name == "update_plan_state":
            field = tool_call.arguments.get("field")
            if not isinstance(field, str):
                return False
            return is_redundant_update_plan_state(
                self.plan,
                field=field,
                value=tool_call.arguments.get("value"),
            )
        # New single-responsibility tools have clear semantics;
        # redundancy checks are less needed. Skip for now.
        return False
```

New:
```python
    def _should_skip_redundant_update(self, tool_call: ToolCall) -> bool:
        """New single-responsibility tools have clear semantics;
        redundancy checks are less needed than with the omnibus tool.
        Always return False for now; re-evaluate if needed."""
        return False
```

- [ ] **Step 5: Remove legacy rendering in `context/manager.py`**

In `_render_tool_event`, remove the entire `update_plan_state` branch (lines 364-376). Keep only the new `_PLAN_WRITER_NAMES` branch added in Task 14.

Old (two branches):
```python
        # update_plan_state is the richest signal — render it as a decision.
        if tool_call and tool_call.name == "update_plan_state":
            field = tool_call.arguments.get("field", "?")
            value = tool_call.arguments.get("value")
            value_preview = self._short_repr(value)
            if result.status == "success":
                return f"决策: update_plan_state {field} = {value_preview}"
            if result.status == "skipped":
                return f"跳过: update_plan_state {field}（{result.error_code or 'skipped'}）"
            return (
                f"失败: update_plan_state {field} — {result.error_code or ''} "
                f"{(result.error or '').strip()}"
            ).strip()

        # New plan-writing tools — render as decisions too
        if tool_call and tool_call.name in _PLAN_WRITER_NAMES:
```

New (single branch):
```python
        # Plan-writing tools — render as decisions
        if tool_call and tool_call.name in _PLAN_WRITER_NAMES:
```

- [ ] **Step 6: Delete `backend/tools/update_plan_state.py`**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && git rm tools/update_plan_state.py
```

- [ ] **Step 7: Migrate `tests/test_agent_loop.py` (concrete example)**

Import change (line 18):

Old:
```python
from tools.update_plan_state import make_update_plan_state_tool
```

New:
```python
from tests.helpers.register_plan_tools import register_all_plan_tools
```

Registration changes — every occurrence of:
```python
engine.register(make_update_plan_state_tool(plan))
```
becomes:
```python
register_all_plan_tools(engine, plan)
```

This applies to lines: 804, 878, 1050, 1132, 1268, 1322.

Tool call data changes — mapping table:

| Old `name` | Old `arguments` | New `name` | New `arguments` |
|------------|----------------|------------|-----------------|
| `"update_plan_state"` | `{"field": "destination", "value": "东京"}` | `"update_trip_basics"` | `{"destination": "东京"}` |
| `"update_plan_state"` | `{"field": "trip_brief", "value": {...}}` | `"set_trip_brief"` | `{"fields": {...}}` |
| `"update_plan_state"` | `{"field": "skeleton_plans", "value": [...]}` | `"set_skeleton_plans"` | `{"plans": [...]}` |
| `"update_plan_state"` | `{"field": "daily_plans", "value": [...]}` | `"replace_daily_plans"` | `{"days": [...]}` |
| `"update_plan_state"` | `{"field": "backtrack", "value": {...}}` | `"request_backtrack"` | `{"to_phase": N, "reason": "..."}` |
| `"update_plan_state"` | `{"field": "budget", "value": {...}}` | `"update_trip_basics"` | `{"budget": {...}}` |

Assertion changes — example:
```python
# Old
assert observed_tool_names[0] == ["update_plan_state"]
# New
assert "update_trip_basics" in observed_tool_names[0]
```

For the inline tool definition in `test_parallel_write_serialization` (lines 729-752): replace with `register_all_plan_tools` and use `update_trip_basics` in test data.

For `test_redundant_update_plan_state_is_skipped_after_phase_rebuild` (line 1046): since `_should_skip_redundant_update` now always returns `False`, rename to `test_duplicate_write_is_not_skipped_with_new_tools` and update assertions.

- [ ] **Step 8: Delete superseded test files**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && \
git rm tests/test_update_plan_state.py && \
git rm tests/test_update_plan_state_strict.py
```

Rationale: `test_update_plan_state.py` tested the deleted tool; `test_update_plan_state_strict.py` tested the adapter. Both are superseded by per-tool tests in `tests/test_plan_tools/`.

- [ ] **Step 9: Migrate all other test files**

Each file needs the same patterns applied. Full list:

| File | Changes |
|------|---------|
| `tests/test_appendix_issues.py` | `make_update_plan_state_tool` → `register_all_plan_tools`; tool call names |
| `tests/test_anthropic_provider.py` | `tool_block.name = "update_plan_state"` → `"update_trip_basics"` (line 88); assertion at line 123 |
| `tests/test_api.py` | Tool call names in mock responses (lines 355, 439, 521) |
| `tests/test_context_manager.py` | Tool call names in mock data (line 132); assertion at line 162: `"决策: update_plan_state destination = 东京"` → `"决策: update_trip_basics"` |
| `tests/test_e2e_golden_path.py` | Tool call names (lines 100, 108, 123, 128, 182, 190, 201, 212, 273) |
| `tests/test_error_paths.py` | Tool call name (line 252) |
| `tests/test_eval_pipeline.py` | Tool name string (line 100) |
| `tests/test_failure_report.py` | Tool name string (line 92) |
| `tests/test_guardrail.py` | Tool call names in existing budget tests (lines 49, 60) |
| `tests/test_memory_integration.py` | Tool call name (line 514) |
| `tests/test_phase_router.py` | If references `update_plan_state` in prompt assertions |
| `tests/test_tool_human_label.py` | Tool name string |
| `tests/test_tool_engine.py` | Remove `"update_plan_state"` from whitelist checks |
| `tests/test_realtime_validation_hook.py` | Tool name in mock |
| `tests/test_trace_api.py` | Tool name string |
| `tests/test_trace_phase_groups.py` | Tool name string |
| `tests/test_tool_choice.py` | Remove `_FORCED` references; assert always returns `"auto"` |

- [ ] **Step 10: Update `PROJECT_OVERVIEW.md` and legacy backtrack spec**

Replace the `update_plan_state` entry in the tool catalog with the 19 new tools:

```markdown
### 状态写入工具（Plan Writers）

| 工具名 | 说明 | 阶段 |
|--------|------|------|
| `update_trip_basics` | 写入目的地、日期、预算、人数、出发城市（anyOf schema 允许短语或结构化值） | 1, 3 |
| `set_trip_brief` | 增量合并旅行画像字段（trip_brief） | 3 |
| `set_candidate_pool` | 整体替换候选池 | 3 |
| `set_shortlist` | 整体替换候选短名单 | 3 |
| `set_skeleton_plans` | 整体替换骨架方案列表 | 3 |
| `select_skeleton` | 锁定选中的骨架方案 | 3 |
| `set_transport_options` | 整体替换交通候选列表 | 3 |
| `select_transport` | 锁定选中的交通方案 | 3 |
| `set_accommodation_options` | 整体替换住宿候选列表 | 3 |
| `set_accommodation` | 锁定住宿选择（区域 + 可选酒店名） | 3, 5 |
| `set_risks` | 整体替换风险点列表 | 3, 5 |
| `set_alternatives` | 整体替换备选方案列表 | 3, 5 |
| `append_day_plan` | 追加一天行程到 daily_plans | 5 |
| `replace_daily_plans` | 整体替换全部逐日行程 | 5 |
| `add_preferences` | 追加用户偏好 | 1, 3, 5 |
| `add_constraints` | 追加用户约束 | 1, 3, 5 |
| `add_destination_candidate` | 追加单个目的地候选 | 1 |
| `set_destination_candidates` | 整体替换目的地候选列表 | 1 |
| `request_backtrack` | 请求回退到更早的阶段 | 1, 3, 5, 7 |
```

Then append a short note to `docs/superpowers/specs/2026-04-04-backtrack-into-update-plan-state-design.md`:

```markdown
> **Superseded note (2026-04-15):** backtrack 不再作为 `update_plan_state(field="backtrack", ...)` 的特殊分支实现。
> 该设计已被 `docs/superpowers/specs/2026-04-15-split-update-plan-state-design.md` 取代；当前方案使用独立工具 `request_backtrack(to_phase, reason)`。
```

- [ ] **Step 11: Clean residual literals + verify zero references remain**

Before running the grep gate, do one explicit cleanup pass for literal `update_plan_state` strings left in comments, docstrings, and metadata. At minimum, check:

- `backend/context/manager.py` — legacy branch comments around `_render_tool_event`
- `backend/agent/loop.py` — comments/docstrings like `update_plan_state_direct` and “forgets to call update_plan_state”
- any migrated test file that still contains `"update_plan_state"` only in explanatory text, not behavior

After that cleanup pass, run:

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && grep -rn "update_plan_state\|make_update_plan_state_tool" --include="*.py" | grep -v __pycache__
```

Expected: zero matches. If any remain, fix before committing.

- [ ] **Step 12: Run full test suite**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro/backend && python -m pytest tests/ -v --timeout=60
```

Expected: All PASS

- [ ] **Step 13: Commit**

```bash
cd /Users/zhaoxiwei/solo-dev/travel_agent_pro && \
git add -A && \
git commit -m "feat(tools): remove update_plan_state, complete migration to 19 single-responsibility tools (Step 4)

- Delete tools/update_plan_state.py
- Remove from main.py registration, engine whitelists, PLAN_WRITER_TOOL_NAMES
- Remove is_redundant_update_plan_state and legacy rendering path
- Delete tests/test_update_plan_state.py and test_update_plan_state_strict.py
- Migrate 30+ test files from make_update_plan_state_tool to
  register_all_plan_tools + new tool names
- Update PROJECT_OVERVIEW.md tool catalog with 19 new tools
- Add superseded note to legacy backtrack spec
- grep 'update_plan_state' in backend/ returns zero hits

Part of: split update_plan_state into 19 tools (Task 15/15)"
```
