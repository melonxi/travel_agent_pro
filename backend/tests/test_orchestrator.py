# backend/tests/test_orchestrator.py
import pytest
from unittest.mock import AsyncMock

from agent.orchestrator import (
    Phase5Orchestrator,
    GlobalValidationIssue,
    _derive_theme,
    _format_error,
)
from agent.day_worker import DayWorkerResult
from agent.worker_prompt import DayTask
from config import Phase5ParallelConfig
from llm.types import ChunkType
from state.models import (
    TravelPlanState,
    DateRange,
    Travelers,
    Accommodation,
    Budget,
)


def _make_plan_with_skeleton() -> TravelPlanState:
    plan = TravelPlanState(session_id="test-orch")
    plan.phase = 5
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.travelers = Travelers(adults=2)
    plan.trip_brief = {"goal": "文化探索", "pace": "balanced", "departure_city": "上海"}
    plan.accommodation = Accommodation(area="新宿", hotel="新宿华盛顿酒店")
    plan.budget = Budget(total=30000, currency="CNY")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [
        {
            "id": "plan_A",
            "name": "平衡版",
            "days": [
                {
                    "area": "新宿/原宿",
                    "theme": "潮流文化",
                    "core_activities": ["明治神宫", "竹下通"],
                    "fatigue": "低",
                },
                {
                    "area": "浅草/上野",
                    "theme": "传统文化",
                    "core_activities": ["浅草寺", "上野公园"],
                    "fatigue": "中等",
                },
                {
                    "area": "涩谷/银座",
                    "theme": "购物",
                    "core_activities": ["涩谷十字路口", "银座六丁目"],
                    "fatigue": "中等",
                },
            ],
        }
    ]
    return plan


class TestDeriveTheme:
    def test_area_and_theme_both_present(self):
        assert _derive_theme({"area": "浅草", "theme": "传统文化"}) == "浅草 · 传统文化"

    def test_only_area(self):
        assert _derive_theme({"area": "浅草"}) == "浅草"

    def test_only_theme(self):
        assert _derive_theme({"theme": "传统文化"}) == "传统文化"

    def test_neither(self):
        assert _derive_theme({}) is None

    def test_empty_strings_treated_as_missing(self):
        assert _derive_theme({"area": "  ", "theme": ""}) is None


class TestFormatError:
    def test_none_stays_none(self):
        assert _format_error(None) is None

    def test_empty_stays_none(self):
        assert _format_error("") is None

    def test_short_passes_through(self):
        assert _format_error("超时 60s") == "超时 60s"

    def test_long_truncates_with_ellipsis(self):
        raw = "x" * 120
        result = _format_error(raw)
        assert len(result) == 80
        assert result.endswith("...")


class TestSplitTasks:
    def test_split_produces_correct_day_count(self):
        plan = _make_plan_with_skeleton()
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        assert len(tasks) == 3

    def test_split_assigns_correct_dates(self):
        plan = _make_plan_with_skeleton()
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        assert tasks[0].date == "2026-05-01"
        assert tasks[1].date == "2026-05-02"
        assert tasks[2].date == "2026-05-03"

    def test_split_preserves_skeleton_data(self):
        plan = _make_plan_with_skeleton()
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        tasks = orch._split_tasks()
        assert tasks[0].skeleton_slice["area"] == "新宿/原宿"
        assert tasks[1].skeleton_slice["area"] == "浅草/上野"

    def test_split_raises_if_no_skeleton(self):
        plan = _make_plan_with_skeleton()
        plan.selected_skeleton_id = None
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        with pytest.raises(ValueError, match="未找到已选骨架"):
            orch._split_tasks()


class TestGlobalValidation:
    def _make_dayplan_dict(self, day: int, date: str, activities: list[dict]) -> dict:
        return {"day": day, "date": date, "notes": "", "activities": activities}

    def _make_activity(self, name: str, cost: float = 0) -> dict:
        return {
            "name": name,
            "location": {"name": name, "lat": 35.0, "lng": 139.0},
            "start_time": "09:00",
            "end_time": "10:00",
            "category": "activity",
            "cost": cost,
        }

    def test_no_issues_when_valid(self):
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_activity("A", 5000)]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("B", 5000)]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C", 5000)]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        assert len(issues) == 0

    def test_detects_poi_duplicate(self):
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_activity("浅草寺")]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("浅草寺")]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C")]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        poi_issues = [i for i in issues if i.issue_type == "poi_duplicate"]
        assert len(poi_issues) >= 1

    def test_detects_budget_overrun(self):
        plan = _make_plan_with_skeleton()
        plan.budget = Budget(total=100, currency="CNY")
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_activity("A", 50)]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("B", 50)]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C", 50)]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        budget_issues = [i for i in issues if i.issue_type == "budget_overrun"]
        assert len(budget_issues) >= 1

    def test_detects_coverage_gap(self):
        plan = _make_plan_with_skeleton()
        # Only provide 2 of 3 expected days
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_activity("A", 5000)]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C", 5000)]),
        ]
        orch = Phase5Orchestrator(plan=plan, llm=None, tool_engine=None, config=None)
        issues = orch._global_validate(dayplans)
        gap_issues = [i for i in issues if i.issue_type == "coverage_gap"]
        assert len(gap_issues) == 1
        assert 2 in gap_issues[0].affected_days


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


@pytest.mark.asyncio
async def test_orchestrator_broadcasts_theme_at_init(monkeypatch):
    plan = _make_plan_with_skeleton()
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(enabled=True, max_workers=3),
    )

    async def _fake_worker(**kwargs):
        return DayWorkerResult(
            day=kwargs["task"].day,
            date=kwargs["task"].date,
            success=True,
            dayplan={"day": kwargs["task"].day, "activities": []},
            iterations=1,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)

    chunks = [c async for c in orch.run()]
    progress_chunks = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    first = progress_chunks[0]
    themes = {w["day"]: w["theme"] for w in first.agent_status["workers"]}
    assert themes[1] == "新宿/原宿 · 潮流文化"
    assert themes[2] == "浅草/上野 · 传统文化"
    assert themes[3] == "涩谷/银座 · 购物"


@pytest.mark.asyncio
async def test_orchestrator_broadcasts_current_tool_mid_run(monkeypatch):
    plan = _make_plan_with_skeleton()
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(enabled=True, max_workers=3),
    )

    async def _fake_worker(**kwargs):
        on_progress = kwargs.get("on_progress")
        if on_progress:
            on_progress(kwargs["task"].day, "iter_start", {"iteration": 1, "max": 5})
            on_progress(
                kwargs["task"].day,
                "tool_start",
                {"tool": "get_poi_info", "human_label": "查询 POI"},
            )
        return DayWorkerResult(
            day=kwargs["task"].day,
            date=kwargs["task"].date,
            success=True,
            dayplan={"day": kwargs["task"].day, "activities": []},
            iterations=1,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)

    chunks = [c async for c in orch.run()]
    progress_chunks = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    # At least one mid-run chunk should have a non-null current_tool
    mid_chunks_with_tool = [
        c for c in progress_chunks
        if any(w.get("current_tool") == "查询 POI" for w in c.agent_status["workers"])
    ]
    assert len(mid_chunks_with_tool) >= 1


@pytest.mark.asyncio
async def test_orchestrator_populates_activity_count_on_success(monkeypatch):
    plan = _make_plan_with_skeleton()
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(enabled=True, max_workers=3),
    )

    async def _fake_worker(**kwargs):
        return DayWorkerResult(
            day=kwargs["task"].day,
            date=kwargs["task"].date,
            success=True,
            dayplan={
                "day": kwargs["task"].day,
                "activities": [{"name": "a"}, {"name": "b"}],
            },
            iterations=1,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)

    chunks = [c async for c in orch.run()]
    last_progress = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ][-1]
    for w in last_progress.agent_status["workers"]:
        assert w["activity_count"] == 2


@pytest.mark.asyncio
async def test_orchestrator_populates_error_on_failure(monkeypatch):
    plan = _make_plan_with_skeleton()
    # Disable retry by making fallback kick in
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(
            enabled=True, max_workers=3, fallback_to_serial=True
        ),
    )

    async def _fake_worker(**kwargs):
        return DayWorkerResult(
            day=kwargs["task"].day,
            date=kwargs["task"].date,
            success=False,
            dayplan=None,
            error="Worker 超时 (60s)",
            iterations=5,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)

    chunks = [c async for c in orch.run()]
    progress = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    # At least one chunk should have error populated for all failed workers
    has_error = any(
        all(w["error"] == "Worker 超时 (60s)" for w in c.agent_status["workers"])
        for c in progress
    )
    assert has_error


@pytest.mark.asyncio
async def test_orchestrator_retry_resets_dynamic_fields(monkeypatch):
    plan = _make_plan_with_skeleton()
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(
            enabled=True, max_workers=3, fallback_to_serial=False
        ),
    )

    call_count = {1: 0, 2: 0, 3: 0}

    async def _fake_worker(**kwargs):
        day = kwargs["task"].day
        call_count[day] += 1
        on_progress = kwargs.get("on_progress")
        if on_progress:
            on_progress(day, "iter_start", {"iteration": 1, "max": 5})
            on_progress(
                day, "tool_start",
                {"tool": "get_poi_info", "human_label": "查询 POI"},
            )
        # First call to day 1 fails, second (retry) succeeds
        if day == 1 and call_count[1] == 1:
            return DayWorkerResult(
                day=day, date=kwargs["task"].date,
                success=False, dayplan=None,
                error="first try failed", iterations=5,
            )
        return DayWorkerResult(
            day=day, date=kwargs["task"].date,
            success=True,
            dayplan={"day": day, "activities": []}, iterations=1,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)

    chunks = [c async for c in orch.run()]
    progress = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    # Find the "retrying" transition chunk
    retry_chunks = [
        c for c in progress
        if any(
            w["day"] == 1 and w["status"] == "retrying"
            for w in c.agent_status["workers"]
        )
    ]
    assert retry_chunks, "expected at least one retrying chunk for day 1"
    retry_worker = next(
        w for w in retry_chunks[0].agent_status["workers"] if w["day"] == 1
    )
    assert retry_worker["iteration"] is None
    assert retry_worker["current_tool"] is None
    assert retry_worker["theme"] is not None  # theme preserved


@pytest.mark.asyncio
async def test_orchestrator_long_error_truncated_to_80(monkeypatch):
    plan = _make_plan_with_skeleton()
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(
            enabled=True, max_workers=3, fallback_to_serial=True
        ),
    )

    async def _fake_worker(**kwargs):
        return DayWorkerResult(
            day=kwargs["task"].day, date=kwargs["task"].date,
            success=False, dayplan=None,
            error="x" * 200, iterations=5,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)
    chunks = [c async for c in orch.run()]
    progress = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    for c in progress:
        for w in c.agent_status["workers"]:
            if w.get("error"):
                assert len(w["error"]) == 80
                assert w["error"].endswith("...")


@pytest.mark.asyncio
async def test_orchestrator_error_code_propagated_on_failure(monkeypatch):
    plan = _make_plan_with_skeleton()
    orch = Phase5Orchestrator(
        plan=plan,
        llm=AsyncMock(),
        tool_engine=AsyncMock(),
        config=Phase5ParallelConfig(
            enabled=True, max_workers=3, fallback_to_serial=True
        ),
    )

    async def _fake_worker(**kwargs):
        return DayWorkerResult(
            day=kwargs["task"].day,
            date=kwargs["task"].date,
            success=False,
            dayplan=None,
            error="Worker 耗尽迭代",
            error_code="REPEATED_QUERY_LOOP",
            iterations=5,
        )

    monkeypatch.setattr("agent.orchestrator.run_day_worker", _fake_worker)
    chunks = [c async for c in orch.run()]
    progress = [
        c for c in chunks
        if c.type == ChunkType.AGENT_STATUS
        and c.agent_status.get("stage") == "parallel_progress"
    ]
    has_error_code = any(
        any(
            w.get("error_code") == "REPEATED_QUERY_LOOP"
            for w in c.agent_status["workers"]
        )
        for c in progress
    )
    assert has_error_code
