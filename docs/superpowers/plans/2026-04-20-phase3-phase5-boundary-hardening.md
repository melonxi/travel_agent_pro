# Phase 3/Phase 5 Boundary Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the serial-parallel quality gap by enhancing Orchestrator validation, injecting cross-day constraints into Workers, upgrading Phase 3 skeleton schema, and adding a backtrack protocol.

**Architecture:** Four modules built incrementally: (A) Enhanced `_global_validate` with 4 new checks + severity-based re-dispatch, (B) Orchestrator compiler layer that derives `forbidden_pois` / `mobility_envelope` from skeleton and injects them into Worker prompts, (C) Phase 3 skeleton schema upgrade with `area_cluster` / `locked_pois` / `candidate_pois` as required day fields, (D) `NEEDS_PHASE3_REPLAN` backtrack protocol from Worker → Orchestrator → Phase 3.

**Tech Stack:** Python 3.12, pytest, dataclasses, existing `harness/validator.py` utilities

**Spec:** `docs/superpowers/specs/2026-04-20-phase3-phase5-boundary-hardening-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/agent/orchestrator.py` | Modify | Add severity to GlobalValidationIssue, 4 new validators, `_compile_day_tasks`, re-dispatch loop, backtrack trigger |
| `backend/agent/worker_prompt.py` | Modify | Upgrade DayTask dataclass, add constraint block to `build_day_suffix`, extract new fields in `split_skeleton_to_day_tasks` |
| `backend/agent/day_worker.py` | Modify | Add `NEEDS_PHASE3_REPLAN` error code detection |
| `backend/tools/plan_tools/phase3_tools.py` | Modify | Upgrade skeleton day schema, add `_validate_skeleton_days` |
| `backend/phase/prompts.py` | Modify | Update skeleton prompt minimum fields + Red Flags |
| `backend/tests/test_orchestrator.py` | Modify | Tests for 4 new validators, re-dispatch, backtrack |
| `backend/tests/test_worker_prompt.py` | Modify | Tests for DayTask upgrade, constraint block, compile |
| `backend/tests/test_day_worker.py` | Modify | Test for NEEDS_PHASE3_REPLAN |
| `backend/tests/test_plan_tools/test_skeleton_schema.py` | Create | Tests for skeleton day schema validation |

---

### Task 1: Add severity to GlobalValidationIssue + time conflict validator

**Files:**
- Modify: `backend/agent/orchestrator.py:54-58` (GlobalValidationIssue), `backend/agent/orchestrator.py:95-158` (_global_validate)
- Modify: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: Write failing tests for time conflict validation**

Add to `backend/tests/test_orchestrator.py`:

```python
class TestTimeConflictValidation:
    def _make_dayplan_dict(self, day: int, date: str, activities: list[dict]) -> dict:
        return {"day": day, "date": date, "notes": "", "activities": activities}

    def _make_timed_activity(
        self, name: str, start: str, end: str, transport_min: int = 0
    ) -> dict:
        return {
            "name": name,
            "location": {"name": name, "lat": 35.0, "lng": 139.0},
            "start_time": start,
            "end_time": end,
            "category": "activity",
            "cost": 0,
            "transport_from_prev": "徒步",
            "transport_duration_min": transport_min,
        }

    def test_no_time_conflict(self):
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [
                self._make_timed_activity("A", "09:00", "10:00"),
                self._make_timed_activity("B", "10:30", "12:00", transport_min=15),
            ]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        time_issues = [i for i in issues if i.issue_type == "time_conflict"]
        assert len(time_issues) == 0

    def test_detects_time_conflict(self):
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [
                self._make_timed_activity("A", "09:00", "10:30"),
                self._make_timed_activity("B", "10:00", "12:00", transport_min=20),
            ]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        time_issues = [i for i in issues if i.issue_type == "time_conflict"]
        assert len(time_issues) == 1
        assert time_issues[0].severity == "error"
        assert time_issues[0].affected_days == [1]

    def test_severity_field_exists_on_all_issues(self):
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [
                self._make_timed_activity("浅草寺", "09:00", "10:00"),
            ]),
            self._make_dayplan_dict(2, "2026-05-02", [
                self._make_timed_activity("浅草寺", "09:00", "10:00"),
            ]),
            self._make_dayplan_dict(3, "2026-05-03", [
                self._make_timed_activity("C", "09:00", "10:00"),
            ]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        for issue in issues:
            assert hasattr(issue, "severity")
            assert issue.severity in ("error", "warning")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_orchestrator.py::TestTimeConflictValidation -v`
Expected: FAIL — `severity` attribute missing on GlobalValidationIssue, no `time_conflict` issues produced

- [ ] **Step 3: Implement severity field + time conflict validator**

In `backend/agent/orchestrator.py`, update `GlobalValidationIssue`:

```python
@dataclass
class GlobalValidationIssue:
    issue_type: str  # "poi_duplicate" | "budget_overrun" | "coverage_gap"
                     # | "time_conflict" | "transport_connection" | "semantic_duplicate" | "pace_mismatch"
    description: str
    affected_days: list[int] = field(default_factory=list)
    severity: str = "warning"  # "error" | "warning"
```

Add `_time_to_minutes` helper at module level (same logic as `harness/validator.py`):

```python
def _time_to_minutes(t: str) -> int | None:
    try:
        h, m = map(int, t.split(":"))
        return h * 60 + m
    except (ValueError, AttributeError):
        return None
```

Add time conflict method inside `Phase5Orchestrator`:

```python
def _validate_time_conflicts(self, dayplans: list[dict[str, Any]]) -> list[GlobalValidationIssue]:
    issues: list[GlobalValidationIssue] = []
    for dp in dayplans:
        day = dp.get("day", 0)
        activities = dp.get("activities", [])
        for i in range(1, len(activities)):
            prev = activities[i - 1]
            curr = activities[i]
            prev_end = _time_to_minutes(prev.get("end_time", ""))
            curr_start = _time_to_minutes(curr.get("start_time", ""))
            travel = curr.get("transport_duration_min", 0) or 0
            if prev_end is not None and curr_start is not None:
                if prev_end + travel > curr_start:
                    issues.append(GlobalValidationIssue(
                        issue_type="time_conflict",
                        description=(
                            f"Day {day}: '{prev.get('name')}'→'{curr.get('name')}' "
                            f"时间冲突（{prev.get('end_time')} 结束 + 交通 {travel}min "
                            f"> {curr.get('start_time')} 开始）"
                        ),
                        affected_days=[day],
                        severity="error",
                    ))
    return issues
```

Update existing `_global_validate` to:
1. Add `severity="error"` to `poi_duplicate` issues
2. Add `severity="warning"` to `budget_overrun` and `coverage_gap` issues
3. Call `self._validate_time_conflicts(dayplans)` and extend issues

In `_global_validate`, after the existing checks, add:

```python
        # 4. Time conflicts
        issues.extend(self._validate_time_conflicts(dayplans))
```

And update existing issue creations to include `severity`:
- `poi_duplicate` → `severity="error"`
- `budget_overrun` → `severity="warning"`
- `coverage_gap` → `severity="warning"`

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_orchestrator.py::TestTimeConflictValidation -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Run full orchestrator test suite for regressions**

Run: `cd backend && python -m pytest tests/test_orchestrator.py -v`
Expected: All tests PASS (existing tests need no changes since `severity` has a default)

- [ ] **Step 6: Commit**

```bash
git add backend/agent/orchestrator.py backend/tests/test_orchestrator.py
git commit -m "feat(orchestrator): add severity to GlobalValidationIssue + time conflict validator

- GlobalValidationIssue now has severity field ('error'|'warning')
- New _validate_time_conflicts checks adjacent activities within each day
- poi_duplicate upgraded to severity='error'

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Add semantic POI duplicate validator

**Files:**
- Modify: `backend/agent/orchestrator.py`
- Modify: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: Write failing tests for semantic duplicate detection**

Add to `backend/tests/test_orchestrator.py`:

```python
class TestSemanticDuplicateValidation:
    def _make_dayplan_dict(self, day: int, date: str, activities: list[dict]) -> dict:
        return {"day": day, "date": date, "notes": "", "activities": activities}

    def _make_geo_activity(self, name: str, lat: float, lng: float) -> dict:
        return {
            "name": name,
            "location": {"name": name, "lat": lat, "lng": lng},
            "start_time": "09:00",
            "end_time": "10:00",
            "category": "activity",
            "cost": 0,
        }

    def test_no_duplicate_when_far_apart(self):
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [
                self._make_geo_activity("浅草寺", 35.7148, 139.7967),
            ]),
            self._make_dayplan_dict(2, "2026-05-02", [
                self._make_geo_activity("明治神宫", 35.6764, 139.6993),
            ]),
            self._make_dayplan_dict(3, "2026-05-03", [
                self._make_geo_activity("上野公園", 35.7146, 139.7734),
            ]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        sem_issues = [i for i in issues if i.issue_type == "semantic_duplicate"]
        assert len(sem_issues) == 0

    def test_detects_nearby_with_similar_name(self):
        """Same location (< 200m), similar name → semantic duplicate."""
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [
                self._make_geo_activity("浅草寺", 35.71478, 139.79675),
            ]),
            self._make_dayplan_dict(2, "2026-05-02", [
                self._make_geo_activity("浅草寺観音堂", 35.71485, 139.79680),
            ]),
            self._make_dayplan_dict(3, "2026-05-03", [
                self._make_geo_activity("C", 35.0, 139.0),
            ]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        sem_issues = [i for i in issues if i.issue_type == "semantic_duplicate"]
        assert len(sem_issues) == 1
        assert sem_issues[0].severity == "error"

    def test_nearby_but_different_name_no_duplicate(self):
        """< 200m but completely different names → NOT duplicate."""
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [
                self._make_geo_activity("浅草寺", 35.71478, 139.79675),
            ]),
            self._make_dayplan_dict(2, "2026-05-02", [
                self._make_geo_activity("雷門前蕎麦屋", 35.71490, 139.79670),
            ]),
            self._make_dayplan_dict(3, "2026-05-03", [
                self._make_geo_activity("C", 35.0, 139.0),
            ]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        sem_issues = [i for i in issues if i.issue_type == "semantic_duplicate"]
        assert len(sem_issues) == 0

    def test_same_day_nearby_not_flagged(self):
        """Same day duplicates are NOT flagged (only cross-day)."""
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [
                self._make_geo_activity("浅草寺", 35.71478, 139.79675),
                self._make_geo_activity("浅草寺観音堂", 35.71485, 139.79680),
            ]),
            self._make_dayplan_dict(2, "2026-05-02", [
                self._make_geo_activity("B", 35.0, 139.0),
            ]),
            self._make_dayplan_dict(3, "2026-05-03", [
                self._make_geo_activity("C", 35.0, 139.0),
            ]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        sem_issues = [i for i in issues if i.issue_type == "semantic_duplicate"]
        assert len(sem_issues) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_orchestrator.py::TestSemanticDuplicateValidation -v`
Expected: FAIL — no `semantic_duplicate` issues produced

- [ ] **Step 3: Implement semantic duplicate validator**

Add to `backend/agent/orchestrator.py` at module level:

```python
from math import radians, sin, cos, sqrt, atan2


def _haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6_371_000
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (ca != cb)))
        prev = curr
    return prev[-1]


def _names_similar(a: str, b: str) -> bool:
    a_norm = a.lower().strip()
    b_norm = b.lower().strip()
    if a_norm in b_norm or b_norm in a_norm:
        return True
    return _levenshtein(a_norm, b_norm) <= 2
```

Add method inside `Phase5Orchestrator`:

```python
def _validate_semantic_duplicates(self, dayplans: list[dict[str, Any]]) -> list[GlobalValidationIssue]:
    issues: list[GlobalValidationIssue] = []
    all_pois: list[tuple[int, str, float, float]] = []
    for dp in dayplans:
        day = dp.get("day", 0)
        for act in dp.get("activities", []):
            loc = act.get("location", {})
            if not isinstance(loc, dict):
                continue
            lat = loc.get("lat")
            lng = loc.get("lng")
            name = act.get("name", "")
            if name and lat is not None and lng is not None:
                all_pois.append((day, name, float(lat), float(lng)))

    seen_pairs: set[tuple[int, int]] = set()
    for i, (day_a, name_a, lat_a, lng_a) in enumerate(all_pois):
        for j, (day_b, name_b, lat_b, lng_b) in enumerate(all_pois):
            if i >= j or day_a == day_b:
                continue
            pair = (i, j)
            if pair in seen_pairs:
                continue
            dist = _haversine_meters(lat_a, lng_a, lat_b, lng_b)
            if dist < 200 and _names_similar(name_a, name_b):
                seen_pairs.add(pair)
                issues.append(GlobalValidationIssue(
                    issue_type="semantic_duplicate",
                    description=(
                        f"'{name_a}'(Day {day_a}) 与 '{name_b}'(Day {day_b}) "
                        f"疑似同一地点（距离 {dist:.0f}m）"
                    ),
                    affected_days=[day_b],
                    severity="error",
                ))
    return issues
```

Add call in `_global_validate` after time conflicts:

```python
        # 5. Semantic duplicates
        issues.extend(self._validate_semantic_duplicates(dayplans))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_orchestrator.py::TestSemanticDuplicateValidation -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/orchestrator.py backend/tests/test_orchestrator.py
git commit -m "feat(orchestrator): add semantic POI duplicate validator

Detects cross-day near-duplicate POIs using coordinate distance < 200m
combined with name substring/levenshtein similarity check.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: Add transport connection + pace validators

**Files:**
- Modify: `backend/agent/orchestrator.py`
- Modify: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: Write failing tests**

Add to `backend/tests/test_orchestrator.py`:

```python
class TestTransportConnectionValidation:
    def _make_dayplan_dict(self, day: int, date: str, activities: list[dict]) -> dict:
        return {"day": day, "date": date, "notes": "", "activities": activities}

    def _make_timed_activity(self, name: str, start: str, end: str) -> dict:
        return {
            "name": name,
            "location": {"name": name, "lat": 35.0, "lng": 139.0},
            "start_time": start,
            "end_time": end,
            "category": "activity",
            "cost": 0,
            "transport_from_prev": "地铁",
            "transport_duration_min": 0,
        }

    def test_arrival_too_early_flagged(self):
        plan = _make_plan_with_skeleton()
        plan.selected_transport = {
            "segments": [
                {"type": "flight", "departure_time": "08:00", "arrival_time": "11:00", "direction": "outbound"},
                {"type": "flight", "departure_time": "18:00", "arrival_time": "21:00", "direction": "return"},
            ]
        }
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [
                self._make_timed_activity("A", "11:30", "13:00"),  # 11:00 到 + 120min = 13:00, 11:30 < 13:00
            ]),
            self._make_dayplan_dict(2, "2026-05-02", [
                self._make_timed_activity("B", "09:00", "12:00"),
            ]),
            self._make_dayplan_dict(3, "2026-05-03", [
                self._make_timed_activity("C", "09:00", "12:00"),
            ]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        conn_issues = [i for i in issues if i.issue_type == "transport_connection"]
        assert len(conn_issues) >= 1
        assert any("首活动" in i.description or "到达" in i.description for i in conn_issues)

    def test_departure_too_late_flagged(self):
        plan = _make_plan_with_skeleton()
        plan.selected_transport = {
            "segments": [
                {"type": "flight", "departure_time": "08:00", "arrival_time": "11:00", "direction": "outbound"},
                {"type": "flight", "departure_time": "15:00", "arrival_time": "18:00", "direction": "return"},
            ]
        }
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [
                self._make_timed_activity("A", "14:00", "17:00"),
            ]),
            self._make_dayplan_dict(2, "2026-05-02", [
                self._make_timed_activity("B", "09:00", "12:00"),
            ]),
            self._make_dayplan_dict(3, "2026-05-03", [
                self._make_timed_activity("C", "09:00", "13:00"),  # ends 13:00, departure 15:00, gap=120 < 180
            ]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        conn_issues = [i for i in issues if i.issue_type == "transport_connection"]
        assert len(conn_issues) >= 1
        assert any("末活动" in i.description or "离开" in i.description for i in conn_issues)

    def test_no_transport_no_issue(self):
        plan = _make_plan_with_skeleton()
        plan.selected_transport = None
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_timed_activity("A", "06:00", "08:00")]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_timed_activity("B", "09:00", "12:00")]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_timed_activity("C", "09:00", "23:00")]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        conn_issues = [i for i in issues if i.issue_type == "transport_connection"]
        assert len(conn_issues) == 0


class TestPaceValidation:
    def _make_dayplan_dict(self, day: int, date: str, activities: list[dict]) -> dict:
        return {"day": day, "date": date, "notes": "", "activities": activities}

    def _make_activity(self, name: str) -> dict:
        return {
            "name": name,
            "location": {"name": name, "lat": 35.0, "lng": 139.0},
            "start_time": "09:00",
            "end_time": "10:00",
            "category": "activity",
            "cost": 0,
        }

    def test_relaxed_pace_over_limit(self):
        plan = _make_plan_with_skeleton()
        plan.trip_brief = {"pace": "relaxed"}
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [
                self._make_activity(f"Act{i}") for i in range(5)
            ]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("B")]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C")]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        pace_issues = [i for i in issues if i.issue_type == "pace_mismatch"]
        assert len(pace_issues) >= 1
        assert pace_issues[0].severity == "warning"

    def test_balanced_pace_within_limit(self):
        plan = _make_plan_with_skeleton()
        plan.trip_brief = {"pace": "balanced"}
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [
                self._make_activity(f"Act{i}") for i in range(4)
            ]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("B")]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C")]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        pace_issues = [i for i in issues if i.issue_type == "pace_mismatch"]
        assert len(pace_issues) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_orchestrator.py::TestTransportConnectionValidation tests/test_orchestrator.py::TestPaceValidation -v`
Expected: FAIL — no `transport_connection` or `pace_mismatch` issues produced

- [ ] **Step 3: Implement transport connection + pace validators**

Add transport time extraction helpers to `backend/agent/orchestrator.py`:

```python
def _extract_transport_time(transport: dict[str, Any], direction: str) -> int | None:
    """Extract arrival/departure time from selected_transport dict.

    direction: 'outbound' → arrival_time, 'return' → departure_time
    """
    segments = transport.get("segments")
    if isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_dir = seg.get("direction", "")
            if seg_dir == direction:
                if direction == "outbound":
                    return _time_to_minutes(seg.get("arrival_time", ""))
                else:
                    return _time_to_minutes(seg.get("departure_time", ""))
    # Fallback: single-segment transport
    if direction == "outbound":
        return _time_to_minutes(transport.get("arrival_time", ""))
    return _time_to_minutes(transport.get("departure_time", ""))
```

Add methods inside `Phase5Orchestrator`:

```python
def _validate_transport_connection(self, dayplans: list[dict[str, Any]]) -> list[GlobalValidationIssue]:
    issues: list[GlobalValidationIssue] = []
    transport = self.plan.selected_transport
    if not isinstance(transport, dict):
        return issues

    sorted_days = sorted(dayplans, key=lambda d: d.get("day", 0))
    if not sorted_days:
        return issues

    arrival_min = _extract_transport_time(transport, "outbound")
    if arrival_min is not None:
        first_day = sorted_days[0]
        acts = first_day.get("activities", [])
        if acts:
            first_start = _time_to_minutes(acts[0].get("start_time", ""))
            if first_start is not None and first_start < arrival_min + 120:
                issues.append(GlobalValidationIssue(
                    issue_type="transport_connection",
                    description=(
                        f"Day {first_day.get('day', 1)} 首活动开始时间过早，"
                        f"距到达不足 2 小时"
                    ),
                    affected_days=[first_day.get("day", 1)],
                    severity="error",
                ))

    departure_min = _extract_transport_time(transport, "return")
    if departure_min is not None:
        last_day = sorted_days[-1]
        acts = last_day.get("activities", [])
        if acts:
            last_end = _time_to_minutes(acts[-1].get("end_time", ""))
            if last_end is not None and last_end > departure_min - 180:
                issues.append(GlobalValidationIssue(
                    issue_type="transport_connection",
                    description=(
                        f"Day {last_day.get('day', len(sorted_days))} 末活动结束过晚，"
                        f"距离开不足 3 小时"
                    ),
                    affected_days=[last_day.get("day", len(sorted_days))],
                    severity="error",
                ))

    return issues

def _validate_pace(self, dayplans: list[dict[str, Any]]) -> list[GlobalValidationIssue]:
    issues: list[GlobalValidationIssue] = []
    pace = (self.plan.trip_brief or {}).get("pace", "balanced")
    max_activities = {"relaxed": 3, "balanced": 4, "intensive": 5}.get(pace, 4)

    for dp in dayplans:
        day = dp.get("day", 0)
        act_count = len(dp.get("activities", []))
        if act_count > max_activities:
            issues.append(GlobalValidationIssue(
                issue_type="pace_mismatch",
                description=(
                    f"Day {day}: {act_count} 个活动超出 {pace} 节奏上限 {max_activities}"
                ),
                affected_days=[day],
                severity="warning",
            ))
    return issues
```

Add calls in `_global_validate`:

```python
        # 6. Transport connection
        issues.extend(self._validate_transport_connection(dayplans))

        # 7. Pace check
        issues.extend(self._validate_pace(dayplans))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_orchestrator.py::TestTransportConnectionValidation tests/test_orchestrator.py::TestPaceValidation -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Run full test suite**

Run: `cd backend && python -m pytest tests/test_orchestrator.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/agent/orchestrator.py backend/tests/test_orchestrator.py
git commit -m "feat(orchestrator): add transport connection + pace validators

- Transport connection: checks Day 1 start >= arrival + 2h,
  last day end <= departure - 3h
- Pace mismatch: checks activity count vs pace limits (warning)

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Upgrade DayTask + compile layer + constraint block in worker prompt

**Files:**
- Modify: `backend/agent/worker_prompt.py:17-25` (DayTask), `backend/agent/worker_prompt.py:141-176` (build_day_suffix), `backend/agent/worker_prompt.py:179-205` (split_skeleton_to_day_tasks)
- Modify: `backend/tests/test_worker_prompt.py`

- [ ] **Step 1: Write failing tests for DayTask upgrade + constraint block**

Add to `backend/tests/test_worker_prompt.py`:

```python
class TestDayTaskConstraints:
    def test_day_task_has_constraint_fields(self):
        task = DayTask(
            day=1, date="2026-05-01", skeleton_slice={}, pace="balanced",
            locked_pois=["浅草寺"],
            candidate_pois=["上野公園"],
            forbidden_pois=["明治神宫"],
            area_cluster=["浅草", "上野"],
        )
        assert task.locked_pois == ["浅草寺"]
        assert task.forbidden_pois == ["明治神宫"]
        assert task.area_cluster == ["浅草", "上野"]

    def test_day_task_defaults_empty(self):
        task = DayTask(day=1, date="2026-05-01", skeleton_slice={}, pace="balanced")
        assert task.locked_pois == []
        assert task.forbidden_pois == []
        assert task.candidate_pois == []
        assert task.area_cluster == []
        assert task.mobility_envelope == {}
        assert task.fallback_slots == []
        assert task.date_role == "full_day"
        assert task.repair_hints == []

    def test_suffix_contains_constraint_block(self):
        task = DayTask(
            day=2, date="2026-05-02",
            skeleton_slice={"area": "浅草/上野", "theme": "传统文化"},
            pace="balanced",
            locked_pois=["浅草寺"],
            candidate_pois=["仲见世商店街", "上野公園"],
            forbidden_pois=["明治神宫", "涩谷Sky"],
            area_cluster=["浅草", "上野"],
            mobility_envelope={"max_cross_area_hops": 1, "max_transit_leg_min": 35},
        )
        suffix = build_day_suffix(task)
        assert "浅草寺" in suffix
        assert "明治神宫" in suffix
        assert "禁止" in suffix
        assert "候选" in suffix or "允许" in suffix
        assert "35" in suffix

    def test_suffix_contains_repair_hints(self):
        task = DayTask(
            day=1, date="2026-05-01", skeleton_slice={}, pace="balanced",
            repair_hints=["Day 1 时间冲突：A→B 间隔不足"],
        )
        suffix = build_day_suffix(task)
        assert "修复要求" in suffix or "修复" in suffix
        assert "时间冲突" in suffix

    def test_suffix_contains_arrival_day_note(self):
        task = DayTask(
            day=1, date="2026-05-01", skeleton_slice={}, pace="balanced",
            date_role="arrival_day",
        )
        suffix = build_day_suffix(task)
        assert "到达日" in suffix

    def test_suffix_contains_departure_day_note(self):
        task = DayTask(
            day=3, date="2026-05-03", skeleton_slice={}, pace="balanced",
            date_role="departure_day",
        )
        suffix = build_day_suffix(task)
        assert "离开日" in suffix


class TestSplitExtractsNewFields:
    def test_extracts_locked_and_candidate_pois(self):
        plan = _make_plan()
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{
            "id": "plan_A", "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草", "上野"],
                    "theme": "传统文化",
                    "locked_pois": ["浅草寺"],
                    "candidate_pois": ["上野公園", "仲见世商店街"],
                    "core_activities": ["寺庙", "散步"],
                },
            ],
        }]
        tasks = split_skeleton_to_day_tasks(plan.skeleton_plans[0], plan)
        assert tasks[0].locked_pois == ["浅草寺"]
        assert tasks[0].candidate_pois == ["上野公園", "仲见世商店街"]
        assert tasks[0].area_cluster == ["浅草", "上野"]

    def test_missing_new_fields_default_empty(self):
        plan = _make_plan()
        plan.selected_skeleton_id = "plan_A"
        plan.skeleton_plans = [{
            "id": "plan_A", "name": "平衡版",
            "days": [{"area": "新宿", "theme": "购物"}],
        }]
        tasks = split_skeleton_to_day_tasks(plan.skeleton_plans[0], plan)
        assert tasks[0].locked_pois == []
        assert tasks[0].candidate_pois == []
        assert tasks[0].area_cluster == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_worker_prompt.py::TestDayTaskConstraints tests/test_worker_prompt.py::TestSplitExtractsNewFields -v`
Expected: FAIL — DayTask missing new fields

- [ ] **Step 3: Implement DayTask upgrade + build_day_suffix constraint block + split extraction**

Update `DayTask` in `backend/agent/worker_prompt.py`:

```python
@dataclass
class DayTask:
    """A single day's task extracted from the skeleton."""

    day: int
    date: str
    skeleton_slice: dict[str, Any]
    pace: str
    locked_pois: list[str] = field(default_factory=list)
    candidate_pois: list[str] = field(default_factory=list)
    forbidden_pois: list[str] = field(default_factory=list)
    area_cluster: list[str] = field(default_factory=list)
    mobility_envelope: dict[str, Any] = field(default_factory=dict)
    fallback_slots: list[dict] = field(default_factory=list)
    date_role: str = "full_day"
    repair_hints: list[str] = field(default_factory=list)
```

Add `from dataclasses import dataclass, field` to imports.

Add constraint block builder:

```python
def _build_constraint_block(task: DayTask) -> str:
    lines: list[str] = []
    has_constraints = (
        task.locked_pois or task.candidate_pois or task.forbidden_pois
        or task.area_cluster or task.mobility_envelope
        or task.date_role != "full_day" or task.fallback_slots or task.repair_hints
    )
    if not has_constraints:
        return ""

    lines.append("\n## 硬约束（必须遵守）\n")

    if task.locked_pois:
        lines.append(f"- **必须包含的活动**: {', '.join(task.locked_pois)}")
    if task.candidate_pois:
        lines.append(f"- **允许使用的候选池**: {', '.join(task.candidate_pois)}")
        lines.append("- 优先从候选池选取，如需额外补充须在同 area_cluster 内")
    if task.forbidden_pois:
        lines.append(f"- **禁止使用（已分配给其他天）**: {', '.join(task.forbidden_pois)}")
    if task.area_cluster:
        lines.append(f"- **当日区域**: {', '.join(task.area_cluster)}")

    env = task.mobility_envelope
    if env:
        max_hops = env.get("max_cross_area_hops", "不限")
        max_leg = env.get("max_transit_leg_min", "不限")
        lines.append(f"- **移动限制**: 最多跨 {max_hops} 个区域, 单段交通 ≤ {max_leg} 分钟")

    if task.date_role == "arrival_day":
        lines.append("- **到达日**: 注意大交通到达时间，首活动须留足接驳缓冲")
    elif task.date_role == "departure_day":
        lines.append("- **离开日**: 注意大交通离开时间，末活动须留足前往交通枢纽的时间")

    if task.fallback_slots:
        lines.append("\n### 备选方案")
        for slot in task.fallback_slots:
            target = slot.get("replace_if_unavailable", "?")
            alts = slot.get("alternatives", [])
            lines.append(f"- 如 {target} 不可行 → 替换为: {', '.join(alts)}")

    if task.repair_hints:
        lines.append("\n### ⚠️ 修复要求（上一轮校验发现的问题）")
        for hint in task.repair_hints:
            lines.append(f"- {hint}")

    return "\n".join(lines)
```

Update `build_day_suffix` — append constraint block at the end:

```python
def build_day_suffix(task: DayTask) -> str:
    """Build the per-day suffix that differs across workers."""
    parts = [f"\n---\n\n## 你的任务：第 {task.day} 天（{task.date}）\n"]

    sk = task.skeleton_slice
    parts.append("骨架安排：")
    if "area" in sk:
        parts.append(f"- 主区域：{sk['area']}")
    if "theme" in sk:
        parts.append(f"- 主题：{sk['theme']}")
    if "core_activities" in sk:
        activities = sk["core_activities"]
        if isinstance(activities, list):
            parts.append(f"- 核心活动：{'、'.join(str(a) for a in activities)}")
        else:
            parts.append(f"- 核心活动：{activities}")
    if "fatigue" in sk:
        parts.append(f"- 疲劳等级：{sk['fatigue']}")
    if "budget_level" in sk:
        parts.append(f"- 预算等级：{sk['budget_level']}")

    pace = task.pace
    if pace == "relaxed":
        count_range = "2-3"
    elif pace == "intensive":
        count_range = "4-5"
    else:
        count_range = "3-4"
    parts.append(f"\n节奏要求：{pace} → 本天 {count_range} 个核心活动")

    constraint_block = _build_constraint_block(task)
    if constraint_block:
        parts.append(constraint_block)

    parts.append(
        "\n请为这一天生成完整的 DayPlan JSON。"
        "先用工具补齐信息和优化路线，最后输出 JSON。"
    )

    return "\n".join(parts)
```

Update `split_skeleton_to_day_tasks` to extract new fields:

```python
def split_skeleton_to_day_tasks(
    skeleton: dict[str, Any],
    plan: TravelPlanState,
) -> list[DayTask]:
    """Split a selected skeleton into per-day tasks."""
    from datetime import date as dt_date, timedelta

    days_data = skeleton.get("days", [])
    start = dt_date.fromisoformat(plan.dates.start) if plan.dates else None
    pace = plan.trip_brief.get("pace", "balanced") if plan.trip_brief else "balanced"

    tasks: list[DayTask] = []
    for i, day_skeleton in enumerate(days_data):
        day_num = i + 1
        if start:
            day_date = (start + timedelta(days=i)).isoformat()
        else:
            day_date = f"day-{day_num}"
        sk = day_skeleton if isinstance(day_skeleton, dict) else {}
        tasks.append(
            DayTask(
                day=day_num,
                date=day_date,
                skeleton_slice=sk,
                pace=pace,
                locked_pois=sk.get("locked_pois", []) if isinstance(sk.get("locked_pois"), list) else [],
                candidate_pois=sk.get("candidate_pois", []) if isinstance(sk.get("candidate_pois"), list) else [],
                area_cluster=sk.get("area_cluster", []) if isinstance(sk.get("area_cluster"), list) else [],
                mobility_envelope=sk.get("mobility_envelope", {}) if isinstance(sk.get("mobility_envelope"), dict) else {},
                fallback_slots=sk.get("fallback_slots", []) if isinstance(sk.get("fallback_slots"), list) else [],
                date_role=sk.get("date_role", "full_day") if isinstance(sk.get("date_role"), str) else "full_day",
            )
        )
    return tasks
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_worker_prompt.py -v`
Expected: All tests PASS (new and existing)

- [ ] **Step 5: Commit**

```bash
git add backend/agent/worker_prompt.py backend/tests/test_worker_prompt.py
git commit -m "feat(worker-prompt): upgrade DayTask with constraint fields + inject into suffix

- DayTask gains locked_pois, candidate_pois, forbidden_pois, area_cluster,
  mobility_envelope, fallback_slots, date_role, repair_hints
- build_day_suffix appends constraint block to worker prompt
- split_skeleton_to_day_tasks extracts new fields from skeleton days

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: Orchestrator compile + re-dispatch logic

**Files:**
- Modify: `backend/agent/orchestrator.py`
- Modify: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: Write failing tests for compile + re-dispatch**

Add to `backend/tests/test_orchestrator.py`:

```python
class TestCompileDayTasks:
    def test_forbidden_pois_derived_from_other_days_locked(self):
        plan = _make_plan_with_skeleton()
        plan.skeleton_plans = [{
            "id": "plan_A", "name": "平衡版",
            "days": [
                {"area_cluster": ["新宿"], "locked_pois": ["明治神宫"], "candidate_pois": ["竹下通"], "theme": "潮流"},
                {"area_cluster": ["浅草"], "locked_pois": ["浅草寺"], "candidate_pois": ["仲见世"], "theme": "传统"},
                {"area_cluster": ["涩谷"], "locked_pois": ["涩谷Sky"], "candidate_pois": ["银座"], "theme": "购物"},
            ],
        }]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        compiled = orch._compile_day_tasks(tasks)
        # Day 1 should have Day 2+3's locked as forbidden
        assert "浅草寺" in compiled[0].forbidden_pois
        assert "涩谷Sky" in compiled[0].forbidden_pois
        assert "明治神宫" not in compiled[0].forbidden_pois  # own locked, not forbidden
        # Day 2 should have Day 1+3's locked as forbidden
        assert "明治神宫" in compiled[1].forbidden_pois
        assert "涩谷Sky" in compiled[1].forbidden_pois
        assert "浅草寺" not in compiled[1].forbidden_pois

    def test_date_role_first_last(self):
        plan = _make_plan_with_skeleton()
        plan.skeleton_plans = [{
            "id": "plan_A", "name": "平衡版",
            "days": [
                {"area_cluster": ["A"], "locked_pois": [], "candidate_pois": ["x"]},
                {"area_cluster": ["B"], "locked_pois": [], "candidate_pois": ["y"]},
                {"area_cluster": ["C"], "locked_pois": [], "candidate_pois": ["z"]},
            ],
        }]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        compiled = orch._compile_day_tasks(tasks)
        assert compiled[0].date_role == "arrival_day"
        assert compiled[1].date_role == "full_day"
        assert compiled[2].date_role == "departure_day"

    def test_mobility_envelope_defaults_by_pace(self):
        plan = _make_plan_with_skeleton()
        plan.trip_brief = {"pace": "relaxed"}
        plan.skeleton_plans = [{
            "id": "plan_A", "name": "轻松版",
            "days": [
                {"area_cluster": ["A"], "locked_pois": [], "candidate_pois": ["x"]},
                {"area_cluster": ["B"], "locked_pois": [], "candidate_pois": ["y"]},
                {"area_cluster": ["C"], "locked_pois": [], "candidate_pois": ["z"]},
            ],
        }]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        compiled = orch._compile_day_tasks(tasks)
        assert compiled[0].mobility_envelope["max_cross_area_hops"] == 1
        assert compiled[0].mobility_envelope["max_transit_leg_min"] == 30

    def test_skeleton_provided_envelope_preserved(self):
        plan = _make_plan_with_skeleton()
        plan.skeleton_plans = [{
            "id": "plan_A", "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["A"], "locked_pois": [], "candidate_pois": ["x"],
                    "mobility_envelope": {"max_cross_area_hops": 5, "max_transit_leg_min": 60},
                },
                {"area_cluster": ["B"], "locked_pois": [], "candidate_pois": ["y"]},
                {"area_cluster": ["C"], "locked_pois": [], "candidate_pois": ["z"]},
            ],
        }]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        compiled = orch._compile_day_tasks(tasks)
        assert compiled[0].mobility_envelope["max_cross_area_hops"] == 5
        assert compiled[0].mobility_envelope["max_transit_leg_min"] == 60
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_orchestrator.py::TestCompileDayTasks -v`
Expected: FAIL — `_compile_day_tasks` does not exist

- [ ] **Step 3: Implement `_compile_day_tasks` method**

Add to `Phase5Orchestrator` class in `backend/agent/orchestrator.py`:

```python
def _compile_day_tasks(self, tasks: list[DayTask]) -> list[DayTask]:
    """Enrich DayTasks with cross-day constraints derived from skeleton."""

    # 1. Build global POI ownership map (locked only)
    poi_owner: dict[str, int] = {}
    for t in tasks:
        for poi in t.locked_pois:
            if poi in poi_owner:
                logger.warning(
                    "POI '%s' locked by both Day %d and Day %d",
                    poi, poi_owner[poi], t.day,
                )
            poi_owner[poi] = t.day

    # 2. Derive forbidden_pois for each day
    for t in tasks:
        t.forbidden_pois = [
            poi for poi, owner_day in poi_owner.items()
            if owner_day != t.day
        ]

    # 3. Fill mobility_envelope defaults (only if skeleton didn't provide)
    pace_defaults = {
        "relaxed":   {"max_cross_area_hops": 1, "max_transit_leg_min": 30},
        "balanced":  {"max_cross_area_hops": 2, "max_transit_leg_min": 40},
        "intensive": {"max_cross_area_hops": 3, "max_transit_leg_min": 50},
    }
    for t in tasks:
        if not t.mobility_envelope:
            t.mobility_envelope = dict(
                pace_defaults.get(t.pace, pace_defaults["balanced"])
            )

    # 4. Derive date_role (if skeleton didn't set it)
    if tasks:
        sorted_tasks = sorted(tasks, key=lambda x: x.day)
        if len(sorted_tasks) == 1:
            if sorted_tasks[0].date_role == "full_day":
                sorted_tasks[0].date_role = "arrival_departure_day"
        else:
            if sorted_tasks[0].date_role == "full_day":
                sorted_tasks[0].date_role = "arrival_day"
            if sorted_tasks[-1].date_role == "full_day":
                sorted_tasks[-1].date_role = "departure_day"

    return tasks
```

Update `run()` method to call `_compile_day_tasks` after `_split_tasks`:

In the `run()` method, after `tasks = self._split_tasks()` and before `shared_prefix = build_shared_prefix(self.plan)`, insert:

```python
            tasks = self._compile_day_tasks(tasks)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_orchestrator.py::TestCompileDayTasks -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Implement re-dispatch logic in `run()`**

In `backend/agent/orchestrator.py`, replace the current validation + write block (steps 8-9 in `run()`) with:

```python
            # 8. Sort and validate
            dayplans = sorted(
                [r.dayplan for r in successes if r.dayplan],
                key=lambda dp: dp.get("day", 0),
            )

            yield self._build_progress_chunk(
                worker_statuses, total_days, "正在做最终验证..."
            )
            issues = self._global_validate(dayplans)
            for issue in issues:
                logger.warning("Global validation [%s]: %s", issue.severity, issue.description)

            # 8b. Re-dispatch for error-severity issues (max 1 round)
            error_issues = [i for i in issues if i.severity == "error"]
            if error_issues:
                redispatch_days = set()
                for ei in error_issues:
                    redispatch_days.update(ei.affected_days)

                task_by_day = {t.day: t for t in tasks}
                for rd_day in sorted(redispatch_days):
                    rd_task = task_by_day.get(rd_day)
                    if rd_task is None:
                        continue
                    # Inject repair hints
                    rd_task.repair_hints = [
                        ei.description for ei in error_issues if rd_day in ei.affected_days
                    ]
                    idx = _find_worker_idx(rd_day)
                    worker_statuses[idx].update({
                        "status": "redispatch",
                        "iteration": None,
                        "current_tool": None,
                        "error": None,
                        "error_code": None,
                    })
                    yield self._build_progress_chunk(
                        worker_statuses, total_days,
                        f"校验发现问题，重新规划第 {rd_day} 天...",
                    )
                    # Re-run with updated suffix (includes repair_hints)
                    rd_result = await run_day_worker(
                        llm=self.llm,
                        tool_engine=self.tool_engine,
                        plan=self.plan,
                        task=rd_task,
                        shared_prefix=shared_prefix,
                        max_iterations=self.config.worker_max_iterations,
                        timeout_seconds=self.config.worker_timeout_seconds,
                        on_progress=_make_progress_cb(idx),
                    )
                    if rd_result.success and rd_result.dayplan:
                        # Replace in dayplans list
                        dayplans = [
                            dp for dp in dayplans if dp.get("day") != rd_day
                        ]
                        dayplans.append(rd_result.dayplan)
                        dayplans.sort(key=lambda dp: dp.get("day", 0))
                        worker_statuses[idx]["status"] = "done"
                        worker_statuses[idx]["activity_count"] = len(
                            rd_result.dayplan.get("activities", [])
                        )
                    else:
                        worker_statuses[idx]["status"] = "failed"
                        worker_statuses[idx]["error"] = _format_error(rd_result.error)

                    yield self._build_progress_chunk(
                        worker_statuses, total_days,
                        f"第 {rd_day} 天重新规划{'完成' if rd_result.success else '失败'}",
                    )

                # Re-validate after re-dispatch
                issues = self._global_validate(dayplans)
                unresolved = [i for i in issues if i.severity == "error"]
                if unresolved:
                    for ui in unresolved:
                        logger.warning("Unresolved after re-dispatch: %s", ui.description)

            # 9. Write results
            if dayplans:
                replace_all_daily_plans(self.plan, dayplans)
```

- [ ] **Step 6: Run full test suite**

Run: `cd backend && python -m pytest tests/test_orchestrator.py -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add backend/agent/orchestrator.py backend/tests/test_orchestrator.py
git commit -m "feat(orchestrator): add compile layer + re-dispatch on validation errors

- _compile_day_tasks derives forbidden_pois, mobility_envelope, date_role
- run() now re-dispatches workers for error-severity issues (max 1 round)
- repair_hints injected into re-dispatched worker prompts

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: Phase 3 skeleton schema upgrade + validation

**Files:**
- Modify: `backend/tools/plan_tools/phase3_tools.py:31-50` (schema), `backend/tools/plan_tools/phase3_tools.py:159-228` (tool function)
- Create: `backend/tests/test_plan_tools/test_skeleton_schema.py`

- [ ] **Step 1: Write failing tests for skeleton day schema validation**

Create `backend/tests/test_plan_tools/test_skeleton_schema.py`:

```python
# backend/tests/test_plan_tools/test_skeleton_schema.py
"""Tests for upgraded skeleton day schema validation."""
import pytest
from unittest.mock import patch

from state.models import TravelPlanState
from tools.base import ToolError


def _make_plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="test-schema")
    plan.phase = 3
    return plan


def _make_tool(plan):
    from tools.plan_tools.phase3_tools import make_set_skeleton_plans_tool
    return make_set_skeleton_plans_tool(plan)


@pytest.mark.asyncio
async def test_valid_skeleton_with_new_fields():
    plan = _make_plan()
    tool = _make_tool(plan)
    result = await tool(plans=[{
        "id": "plan_a",
        "name": "平衡版",
        "days": [
            {
                "area_cluster": ["浅草", "上野"],
                "theme": "传统文化",
                "locked_pois": ["浅草寺"],
                "candidate_pois": ["仲见世商店街", "上野公園"],
                "core_activities": ["寺庙参观", "公园散步"],
                "fatigue_level": "medium",
                "budget_level": "medium",
            },
        ],
        "tradeoffs": {"kept": "传统", "dropped": "购物"},
    }])
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_missing_area_cluster_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="area_cluster"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "theme": "传统文化",
                    "locked_pois": ["浅草寺"],
                    "candidate_pois": ["上野公園"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_missing_locked_pois_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="locked_pois"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "candidate_pois": ["上野公園"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_missing_candidate_pois_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="candidate_pois"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "locked_pois": ["浅草寺"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_cross_day_locked_poi_duplicate_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="浅草寺.*locked.*唯一"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "locked_pois": ["浅草寺"],
                    "candidate_pois": ["仲见世"],
                },
                {
                    "area_cluster": ["上野"],
                    "locked_pois": ["浅草寺"],  # duplicate lock!
                    "candidate_pois": ["上野公園"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_empty_locked_pois_is_valid():
    plan = _make_plan()
    tool = _make_tool(plan)
    result = await tool(plans=[{
        "id": "plan_a",
        "name": "平衡版",
        "days": [
            {
                "area_cluster": ["浅草"],
                "locked_pois": [],
                "candidate_pois": ["浅草寺", "仲见世"],
            },
        ],
    }])
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_empty_days_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="days.*不能为空"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [],
        }])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_plan_tools/test_skeleton_schema.py -v`
Expected: FAIL — validation doesn't exist yet, so valid and missing-field tests both behave unexpectedly

- [ ] **Step 3: Implement schema upgrade + validation**

In `backend/tools/plan_tools/phase3_tools.py`, update `_SET_SKELETON_PLANS_PARAMS`:

```python
_SKELETON_DAY_SCHEMA = {
    "type": "object",
    "properties": {
        "area_cluster": {
            "type": "array",
            "items": {"type": "string"},
            "description": "当天主区域列表",
        },
        "theme": {"type": "string", "description": "当天主题"},
        "locked_pois": {
            "type": "array",
            "items": {"type": "string"},
            "description": "该天独占的强锚点（其他天禁止使用）",
        },
        "candidate_pois": {
            "type": "array",
            "items": {"type": "string"},
            "description": "该天允许使用的候选 POI 池",
        },
        "core_activities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "核心活动或体验",
        },
        "fatigue_level": {"type": "string", "description": "疲劳等级"},
        "budget_level": {"type": "string", "description": "预算等级"},
        "excluded_pois": {"type": "array", "items": {"type": "string"}},
        "date_role": {"type": "string", "enum": ["arrival_day", "departure_day", "full_day"]},
        "mobility_envelope": {"type": "object"},
        "fallback_slots": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["area_cluster", "locked_pois", "candidate_pois"],
}

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
                    "days": {"type": "array", "items": _SKELETON_DAY_SCHEMA},
                    "tradeoffs": {"type": "object"},
                },
                "required": ["id", "name"],
            },
            "description": "骨架方案列表",
        },
    },
    "required": ["plans"],
}
```

Add validation function:

```python
def _validate_skeleton_days(plans: list[dict]) -> None:
    for plan_idx, plan in enumerate(plans):
        days = plan.get("days", [])
        if isinstance(days, list) and len(days) == 0:
            raise ToolError(
                f"plans[{plan_idx}].days 不能为空",
                error_code="INVALID_VALUE",
                suggestion="每个骨架方案必须包含至少一天的安排",
            )
        if not isinstance(days, list):
            return  # skip if not list (backward compat)

        all_locked: dict[str, tuple[int, int]] = {}

        for day_idx, day in enumerate(days):
            if not isinstance(day, dict):
                continue
            prefix = f"plans[{plan_idx}].days[{day_idx}]"

            ac = day.get("area_cluster")
            if not ac or not isinstance(ac, list) or not all(isinstance(x, str) for x in ac):
                raise ToolError(
                    f"{prefix}.area_cluster 必须是非空字符串列表",
                    error_code="INVALID_VALUE",
                    suggestion='例如 "area_cluster": ["浅草", "上野"]',
                )

            lp = day.get("locked_pois")
            if lp is None or not isinstance(lp, list):
                raise ToolError(
                    f"{prefix}.locked_pois 必须是字符串列表（可以为空列表）",
                    error_code="INVALID_VALUE",
                    suggestion='例如 "locked_pois": ["浅草寺"] 或 "locked_pois": []',
                )

            cp = day.get("candidate_pois")
            if not cp or not isinstance(cp, list):
                raise ToolError(
                    f"{prefix}.candidate_pois 必须是非空字符串列表",
                    error_code="INVALID_VALUE",
                    suggestion='例如 "candidate_pois": ["仲见世商店街", "上野公園"]',
                )

            for poi in (lp if isinstance(lp, list) else []):
                if not isinstance(poi, str):
                    continue
                if poi in all_locked:
                    prev_p, prev_d = all_locked[poi]
                    raise ToolError(
                        f"'{poi}' 同时被 plans[{prev_p}].days[{prev_d}] "
                        f"和 {prefix} locked，locked_pois 必须跨天唯一",
                        error_code="INVALID_VALUE",
                        suggestion=f"把 '{poi}' 只分配给一天，另一天可放入 candidate_pois",
                    )
                all_locked[poi] = (plan_idx, day_idx)
```

In `set_skeleton_plans` function, after the id/name validation loop and before `write_skeleton_plans`, add:

```python
        _validate_skeleton_days(normalized_plans)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_plan_tools/test_skeleton_schema.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Check for regressions in existing skeleton tests**

Run: `cd backend && python -m pytest tests/test_plan_tools/ -v`
Expected: All pass. Some existing tests may need `area_cluster`/`locked_pois`/`candidate_pois` added to their skeleton days fixtures. Fix any failing tests by adding the required fields to test fixtures.

- [ ] **Step 6: Commit**

```bash
git add backend/tools/plan_tools/phase3_tools.py backend/tests/test_plan_tools/test_skeleton_schema.py
git commit -m "feat(phase3): upgrade skeleton day schema with required POI fields

- days[].area_cluster, locked_pois, candidate_pois now required
- Cross-day locked_pois uniqueness enforced
- Empty days array rejected

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 7: Update Phase 3 skeleton prompt

**Files:**
- Modify: `backend/phase/prompts.py:419-425` (minimum fields), `backend/phase/prompts.py:480-485` (Red Flags)

- [ ] **Step 1: Update minimum structured fields in skeleton prompt**

In `backend/phase/prompts.py`, replace lines 419-424 (the minimum fields section):

```python
每套骨架的 **最小结构化字段**（写入 `skeleton_plans` 时必须包含）：
- `id`：唯一标识符，必须是简短稳定的英文 ID（如 `"plan_A"`、`"plan_B"`），后续选择时作为唯一引用主键
- `name`：方案显示名称（如"轻松版""平衡版""高密度版"），前端卡片标题优先读取此字段
- `days`：list，每天必须包含：
  - `area_cluster`：当天主区域列表（如 `["浅草", "上野"]`）
  - `theme`：当天主题
  - `locked_pois`：该天独占的强锚点列表（如 `["浅草寺"]`）。每个 POI 只能被一天 lock，不允许跨天重复。可以为空列表。
  - `candidate_pois`：该天允许使用的候选 POI 池（如 `["仲见世商店街", "上野公園"]`），不能为空
  - `core_activities`：核心活动
  - `fatigue_level`：疲劳等级（low / medium / high）
  - `budget_level`：预算等级（low / medium / high）
- `tradeoffs`：保留了什么、放弃了什么

可选字段（有则更好，没有时系统自动推导）：
- `excluded_pois`：该天显式排除的 POI
- `date_role`：`"arrival_day"` / `"departure_day"` / `"full_day"`
- `mobility_envelope`：`{ "max_cross_area_hops": 1, "max_transit_leg_min": 35 }`
- `fallback_slots`：`[{ "replace_if_unavailable": "浅草寺", "alternatives": ["今户神社"] }]`
```

- [ ] **Step 2: Update Red Flags section**

In `backend/phase/prompts.py`, replace lines 480-485:

```python
## Red Flags

- **没有搜索攻略就直接生成骨架**——这是最常见的失败模式，会导致方案"逻辑正确但不实用"
- 骨架之间差异太小（仅顺序不同，无实质取舍差异）
- 没有说明取舍（保留了什么、放弃了什么）
- 没有按锚点思考直接生成方案
- **locked_pois 跨天重复**——同一个 POI 被两天同时 lock，会导致 Phase 5 并行 Worker 产生冲突
- **candidate_pois 为空**——Phase 5 Worker 没有候选池就只能凭空创造，容易偏离骨架意图""",
```

- [ ] **Step 3: Run prompt architecture tests**

Run: `cd backend && python -m pytest tests/test_prompt_architecture.py -v`
Expected: PASS (existing tests check for structural sections, new content shouldn't break them)

- [ ] **Step 4: Commit**

```bash
git add backend/phase/prompts.py
git commit -m "feat(prompts): update skeleton prompt with required POI fields + new Red Flags

- Minimum structured fields now include area_cluster, locked_pois,
  candidate_pois with clear semantics
- Added optional fields section (excluded_pois, date_role, etc.)
- New Red Flags for locked_pois cross-day duplication and empty candidate_pois

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 8: NEEDS_PHASE3_REPLAN backtrack protocol

**Files:**
- Modify: `backend/agent/day_worker.py`
- Modify: `backend/agent/orchestrator.py`
- Modify: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: Write failing test for backtrack trigger**

Add to `backend/tests/test_orchestrator.py`:

```python
class TestBacktrackProtocol:
    @pytest.mark.asyncio
    async def test_needs_replan_triggers_backtrack_chunk(self):
        """When a worker returns NEEDS_PHASE3_REPLAN, orchestrator should yield backtrack text."""
        plan = _make_plan_with_skeleton()
        plan.skeleton_plans = [{
            "id": "plan_A", "name": "平衡版",
            "days": [
                {"area_cluster": ["A"], "locked_pois": ["X"], "candidate_pois": ["y"]},
                {"area_cluster": ["B"], "locked_pois": ["Z"], "candidate_pois": ["w"]},
                {"area_cluster": ["C"], "locked_pois": [], "candidate_pois": ["v"]},
            ],
        }]

        replan_result = DayWorkerResult(
            day=1, date="2026-05-01", success=False, dayplan=None,
            error="locked_pois ['X'] 全部不可行", error_code="NEEDS_PHASE3_REPLAN",
        )
        ok_result_2 = DayWorkerResult(
            day=2, date="2026-05-02", success=True,
            dayplan={"day": 2, "date": "2026-05-02", "notes": "", "activities": []},
        )
        ok_result_3 = DayWorkerResult(
            day=3, date="2026-05-03", success=True,
            dayplan={"day": 3, "date": "2026-05-03", "notes": "", "activities": []},
        )

        mock_llm = AsyncMock()
        mock_tool_engine = AsyncMock()

        with patch("agent.orchestrator.run_day_worker") as mock_worker:
            call_count = 0
            async def side_effect(**kwargs):
                nonlocal call_count
                call_count += 1
                day = kwargs["task"].day
                if day == 1:
                    return replan_result
                elif day == 2:
                    return ok_result_2
                else:
                    return ok_result_3
            mock_worker.side_effect = side_effect

            orch = Phase5Orchestrator(
                plan=plan, llm=mock_llm, tool_engine=mock_tool_engine,
                config=Phase5ParallelConfig(max_workers=3, fallback_to_serial=False),
            )
            chunks = []
            async for chunk in orch.run():
                chunks.append(chunk)

            text_chunks = [c for c in chunks if c.type == ChunkType.TEXT_DELTA]
            combined_text = "".join(c.content for c in text_chunks if c.content)
            assert "骨架分配失败" in combined_text or "NEEDS_PHASE3_REPLAN" in combined_text or "回退" in combined_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_orchestrator.py::TestBacktrackProtocol -v`
Expected: FAIL — orchestrator doesn't handle `NEEDS_PHASE3_REPLAN` specially

- [ ] **Step 3: Implement NEEDS_PHASE3_REPLAN handling in orchestrator**

In `backend/agent/orchestrator.py`, in the `run()` method, after the failure retry block (step 7) and before the validation block (step 8), add:

```python
            # 7b. Check for NEEDS_PHASE3_REPLAN
            replan_results = [
                r for r in successes + [
                    DayWorkerResult(day=t.day, date=t.date, success=False, dayplan=None,
                                    error=err, error_code="NEEDS_PHASE3_REPLAN")
                    for t, err in failures
                ]
                if not r.success and r.error_code == "NEEDS_PHASE3_REPLAN"
            ]
```

Actually, to keep it simpler, track replan failures from the initial run + retry. After step 7 (retry loop), replace the simple check:

In the `run()` method, after the retry loop completes, add this block before step 8:

```python
            # 7b. Check for NEEDS_PHASE3_REPLAN from any worker
            all_replan_errors: list[str] = []
            for r in successes:
                if not r.success and r.error_code == "NEEDS_PHASE3_REPLAN":
                    all_replan_errors.append(f"Day {r.day}: {r.error}")
            # Also check retry failures
            for ws in worker_statuses:
                if ws.get("error_code") == "NEEDS_PHASE3_REPLAN":
                    all_replan_errors.append(f"Day {ws['day']}: {ws.get('error', 'unknown')}")

            if all_replan_errors:
                reason = "骨架分配失败，以下天数无法按当前骨架展开:\n" + "\n".join(all_replan_errors)
                yield LLMChunk(
                    type=ChunkType.TEXT_DELTA,
                    content=f"\n\n⚠️ {reason}\n需要回退到 Phase 3 重新调整骨架方案。\n",
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
```

- [ ] **Step 4: Add NEEDS_PHASE3_REPLAN as a recognized error code in day_worker.py**

In `backend/agent/day_worker.py`, add this constant near the top (after `_MAX_POI_RECOVERY`):

```python
ERROR_NEEDS_PHASE3_REPLAN = "NEEDS_PHASE3_REPLAN"
```

No functional changes needed in day_worker.py — the error code is already passed through as a string in `DayWorkerResult.error_code`. The Worker would return this error code when it determines all locked_pois are unfeasible, which is a prompt-driven behavior (the constraint block in Task 4 instructs it to do so).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_orchestrator.py::TestBacktrackProtocol -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `cd backend && python -m pytest tests/test_orchestrator.py tests/test_worker_prompt.py tests/test_plan_tools/test_skeleton_schema.py -v`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add backend/agent/orchestrator.py backend/agent/day_worker.py backend/tests/test_orchestrator.py
git commit -m "feat(orchestrator): add NEEDS_PHASE3_REPLAN backtrack protocol

When any worker returns NEEDS_PHASE3_REPLAN error code, orchestrator
emits backtrack text and stops without writing results.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 9: Fix existing test fixtures + full regression test

**Files:**
- Modify: Various test files that use old skeleton format
- Modify: `backend/tests/test_e2e_golden_path.py` (if needed)

- [ ] **Step 1: Identify all test files with old skeleton format**

Run: `cd backend && grep -rn '"days".*\[' tests/ --include="*.py" | grep -v 'area_cluster'`

Look for test files that create skeleton_plans with days that lack `area_cluster` / `locked_pois` / `candidate_pois`. Update each one to include the required fields.

- [ ] **Step 2: Update fixture skeletons in all affected test files**

For each test file that creates skeleton_plans with days, add the required fields. Example pattern — if a test has:

```python
"days": [{"area": "新宿", "theme": "购物"}]
```

Update to:

```python
"days": [{"area": "新宿", "theme": "购物", "area_cluster": ["新宿"], "locked_pois": [], "candidate_pois": ["竹下通"]}]
```

The key files to check:
- `tests/test_orchestrator.py` (`_make_plan_with_skeleton`)
- `tests/test_worker_prompt.py`
- `tests/test_day_worker.py`
- `tests/test_day_worker_progress_callback.py`
- `tests/test_e2e_golden_path.py`
- `tests/test_loop_phase5_routing.py`
- `tests/test_parallel_phase5_integration.py`
- `tests/test_config_parallel.py`
- `tests/test_context_manager_worker.py`

- [ ] **Step 3: Run full backend test suite**

Run: `cd backend && python -m pytest tests/ -q`
Expected: All tests PASS

- [ ] **Step 4: Commit fixture updates**

```bash
git add backend/tests/
git commit -m "test: update skeleton fixtures with required day fields

All skeleton test fixtures now include area_cluster, locked_pois,
and candidate_pois to match upgraded Phase 3 schema.

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 10: Update PROJECT_OVERVIEW.md + final verification

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Update PROJECT_OVERVIEW.md**

Add to the Phase 5 section (after the "Day Worker loop 保护机制" bullet):

```
- **Orchestrator 全局校验（7 项）**：POI 精确去重（error）、预算检查（warning）、天数覆盖（warning）、时间冲突（error）、首尾日大交通衔接（error）、语义近似 POI 去重（error）、活动数 vs 节奏（warning）。error 级别问题触发 targeted re-dispatch（最多 1 次）。
- **Orchestrator 编译层**：`_compile_day_tasks` 从骨架编译 `DayTask`，推导 `forbidden_pois`（跨天排他）、`mobility_envelope`（默认值按 pace）、`date_role`（首尾日标记），注入 Worker prompt 约束块
- **Worker 约束遵守**：Day Worker prompt 包含硬约束块（locked_pois/candidate_pois/forbidden_pois/mobility_envelope），违反时应返回 `NEEDS_PHASE3_REPLAN` 触发回退
```

Update Phase 3 skeleton description to mention new schema:

```
- **skeleton** → 骨架方案（非逐小时）；日级结构化字段：`area_cluster`（必填）、`locked_pois`（必填，跨天唯一）、`candidate_pois`（必填）、可选 `excluded_pois` / `date_role` / `mobility_envelope` / `fallback_slots`
```

- [ ] **Step 2: Run full backend test suite one last time**

Run: `cd backend && python -m pytest tests/ -q`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: update PROJECT_OVERVIEW with boundary hardening changes

- Phase 5 orchestrator now has 7 validation checks + compile layer
- Phase 3 skeleton schema requires area_cluster/locked_pois/candidate_pois
- Worker prompt includes constraint block + NEEDS_PHASE3_REPLAN protocol

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```
