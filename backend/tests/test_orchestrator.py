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

    def _make_activity(self, name: str, cost: float = 0, lat: float = 35.0, lng: float = 139.0) -> dict:
        return {
            "name": name,
            "location": {"name": name, "lat": lat, "lng": lng},
            "start_time": "09:00",
            "end_time": "10:00",
            "category": "activity",
            "cost": cost,
        }

    def test_no_issues_when_valid(self):
        plan = _make_plan_with_skeleton()
        dayplans = [
            self._make_dayplan_dict(1, "2026-05-01", [self._make_activity("A", 5000, lat=35.7, lng=139.7)]),
            self._make_dayplan_dict(2, "2026-05-02", [self._make_activity("B", 5000, lat=35.6, lng=139.6)]),
            self._make_dayplan_dict(3, "2026-05-03", [self._make_activity("C", 5000, lat=35.5, lng=139.5)]),
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
                self._make_timed_activity("A", "11:30", "13:00"),  # 11:00 arrival + 120min = 13:00, 11:30 < 13:00
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
