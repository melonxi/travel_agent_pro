"""D3: 结构化再协商 + 共享黑板 单元/注入测试。"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.phase3.orchestrator import Phase3Orchestrator
from agent.phase3.day_worker import DayWorkerResult
from agent.phase3.renegotiation import (
    MAX_RENEGOTIATE_PER_DAY,
    Phase3Blackboard,
    ReplanRequest,
    renegotiate_skeleton,
)
from config import Phase3ParallelConfig
from llm.types import ChunkType
from state.models import (
    Accommodation,
    Budget,
    DateRange,
    TravelPlanState,
    Travelers,
)


def _skeleton_days():
    return [
        {
            "area_cluster": ["A"],
            "locked_pois": ["浅草寺"],
            "candidate_pois": ["仲见世"],
            "date_role": "arrival_day",
        },
        {
            "area_cluster": ["B"],
            "locked_pois": ["明治神宫"],
            "candidate_pois": ["代代木"],
            "date_role": "full_day",
        },
        {
            "area_cluster": ["C"],
            "locked_pois": [],
            "candidate_pois": ["上野公园"],
            "date_role": "departure_day",
        },
    ]


def _plan() -> TravelPlanState:
    plan = TravelPlanState(session_id="test-d3")
    plan.phase = 3
    plan.destination = "东京"
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.travelers = Travelers(adults=2)
    plan.trip_brief = {"goal": "文化", "pace": "balanced", "departure_city": "上海"}
    plan.accommodation = Accommodation(area="新宿")
    plan.budget = Budget(total=30000, currency="CNY")
    plan.selected_skeleton_id = "plan_A"
    plan.skeleton_plans = [
        {"id": "plan_A", "name": "平衡版", "days": _skeleton_days()}
    ]
    return plan


class TestReplanRequest:
    def test_from_tool_arguments_suggest_move(self):
        req = ReplanRequest.from_tool_arguments(
            day=1,
            arguments={
                "kind": "SUGGEST_MOVE",
                "reason": "当日闭馆",
                "move_poi": "浅草寺",
                "to_day": 2,
            },
        )
        assert req.kind == "SUGGEST_MOVE"
        assert req.move_poi == "浅草寺"
        assert req.to_day == 2

    def test_suggest_move_missing_fields_downgrades(self):
        req = ReplanRequest.from_tool_arguments(
            day=1,
            arguments={"kind": "SUGGEST_MOVE", "reason": "想移走"},
        )
        assert req.kind == "INFEASIBLE_DAY"

    def test_unknown_kind_defaults_infeasible(self):
        req = ReplanRequest.from_tool_arguments(
            day=1, arguments={"kind": "??? ", "reason": "x"}
        )
        assert req.kind == "INFEASIBLE_DAY"


class TestRenegotiateSkeleton:
    def test_suggest_move_applies_and_marks_affected(self):
        skeleton = {"id": "plan_A", "days": _skeleton_days()}
        counts: dict[int, int] = {}
        outcome = renegotiate_skeleton(
            skeleton=skeleton,
            requests=[
                ReplanRequest(
                    day=1,
                    kind="SUGGEST_MOVE",
                    reason="闭馆",
                    move_poi="浅草寺",
                    to_day=2,
                )
            ],
            pace="balanced",
            renegotiate_count=counts,
            total_days=3,
        )
        assert outcome.affected_days == {1, 2}
        assert len(outcome.amendments) == 1
        assert not outcome.unresolved
        day1 = outcome.skeleton_copy["days"][0]
        day2 = outcome.skeleton_copy["days"][1]
        assert "浅草寺" not in day1["locked_pois"]
        assert "浅草寺" in day2["locked_pois"]
        assert counts[1] == 1
        assert counts[2] == 1

    def test_suggest_move_rejects_when_target_full(self):
        days = _skeleton_days()
        # balanced max_core=4；塞满 locked
        days[1]["locked_pois"] = ["A", "B", "C", "D"]
        days[1]["candidate_pois"] = []
        skeleton = {"days": days}
        counts: dict[int, int] = {}
        outcome = renegotiate_skeleton(
            skeleton=skeleton,
            requests=[
                ReplanRequest(
                    day=1,
                    kind="SUGGEST_MOVE",
                    reason="满",
                    move_poi="浅草寺",
                    to_day=2,
                )
            ],
            pace="balanced",
            renegotiate_count=counts,
            total_days=3,
        )
        assert not outcome.affected_days
        assert len(outcome.unresolved) == 1
        assert "浅草寺" in skeleton["days"][0]["locked_pois"]

    def test_overloaded_trims_candidates(self):
        days = _skeleton_days()
        days[1]["locked_pois"] = ["L1", "L2", "L3"]
        days[1]["candidate_pois"] = ["C1", "C2", "C3"]  # slots left = 1 for balanced
        skeleton = {"days": days}
        counts: dict[int, int] = {}
        outcome = renegotiate_skeleton(
            skeleton=skeleton,
            requests=[
                ReplanRequest(day=2, kind="OVERLOADED", reason="太多")
            ],
            pace="balanced",
            renegotiate_count=counts,
            total_days=3,
        )
        assert outcome.affected_days == {2}
        assert len(outcome.skeleton_copy["days"][1]["candidate_pois"]) == 1

    def test_infeasible_stays_unresolved(self):
        skeleton = {"days": _skeleton_days()}
        counts: dict[int, int] = {}
        outcome = renegotiate_skeleton(
            skeleton=skeleton,
            requests=[
                ReplanRequest(day=1, kind="INFEASIBLE_DAY", reason="根本不行")
            ],
            pace="balanced",
            renegotiate_count=counts,
            total_days=3,
        )
        assert not outcome.affected_days
        assert len(outcome.unresolved) == 1

    def test_fuse_prevents_second_renegotiate(self):
        skeleton = {"days": _skeleton_days()}
        counts = {1: MAX_RENEGOTIATE_PER_DAY, 2: MAX_RENEGOTIATE_PER_DAY}
        outcome = renegotiate_skeleton(
            skeleton=skeleton,
            requests=[
                ReplanRequest(
                    day=1,
                    kind="SUGGEST_MOVE",
                    reason="再次",
                    move_poi="浅草寺",
                    to_day=2,
                )
            ],
            pace="balanced",
            renegotiate_count=counts,
            total_days=3,
        )
        assert not outcome.affected_days
        assert len(outcome.unresolved) == 1
        assert "上限" in outcome.messages[0]


class TestBlackboard:
    def test_reject_poi_claimed_by_other_day(self):
        bb = Phase3Blackboard()
        bb.poi_registry["浅草寺"] = 1
        ok, reason = bb.try_accept_dayplan(
            2,
            {
                "day": 2,
                "date": "2026-05-02",
                "activities": [
                    {"name": "浅草寺", "cost": 0, "start_time": "10:00", "end_time": "11:00"}
                ],
            },
        )
        assert ok is False
        assert "认领" in (reason or "")

    def test_accept_registers_poi_and_budget(self):
        bb = Phase3Blackboard(total_budget=10000, precommitted_cost=0)
        ok, reason = bb.try_accept_dayplan(
            1,
            {
                "day": 1,
                "date": "2026-05-01",
                "activities": [
                    {"name": "A", "cost": 100, "start_time": "09:00", "end_time": "10:00"},
                    {"name": "B", "cost": 200, "start_time": "11:00", "end_time": "12:00"},
                ],
            },
        )
        assert ok is True
        assert reason is None
        assert bb.poi_registry["A"] == 1
        assert bb.budget_ledger[1] == 300.0

    def test_budget_overrun_rejects(self):
        bb = Phase3Blackboard(total_budget=500, precommitted_cost=400)
        ok, reason = bb.try_accept_dayplan(
            1,
            {
                "day": 1,
                "date": "2026-05-01",
                "activities": [
                    {"name": "A", "cost": 200, "start_time": "09:00", "end_time": "10:00"},
                ],
            },
        )
        assert ok is False
        assert "预算" in (reason or "")

    def test_release_day_clears_claims(self):
        bb = Phase3Blackboard()
        bb.try_accept_dayplan(
            1,
            {
                "day": 1,
                "date": "2026-05-01",
                "activities": [
                    {"name": "A", "cost": 10, "start_time": "09:00", "end_time": "10:00"},
                ],
            },
        )
        bb.release_day(1)
        assert "A" not in bb.poi_registry
        assert 1 not in bb.budget_ledger


class TestOrchestratorRenegotiation:
    @pytest.mark.asyncio
    async def test_suggest_move_redispatches_only_affected_days(self):
        plan = _plan()
        call_days: list[int] = []
        attempt_by_day: dict[int, list[int]] = {}

        async def fake_worker(**kwargs):
            task = kwargs["task"]
            attempt = kwargs["attempt"]
            call_days.append(task.day)
            attempt_by_day.setdefault(task.day, []).append(attempt)
            if task.day == 1 and attempt == 1:
                return DayWorkerResult(
                    day=1,
                    date=task.date,
                    success=False,
                    dayplan=None,
                    error="闭馆",
                    error_code="NEEDS_PHASE3_REPLAN",
                    replan_request=ReplanRequest(
                        day=1,
                        kind="SUGGEST_MOVE",
                        reason="浅草寺闭馆",
                        move_poi="浅草寺",
                        to_day=2,
                    ),
                )
            # 成功路径：覆盖 task.locked_pois，避免全局 locked_poi_missing 再触发 redispatch。
            acts = []
            hour = 10
            for poi in list(task.locked_pois) or [f"活动{task.day}-{attempt}"]:
                acts.append(
                    {
                        "name": poi,
                        "cost": 0,
                        "start_time": f"{hour:02d}:00",
                        "end_time": f"{hour:02d}:30",
                    }
                )
                hour += 1
            dayplan = {
                "day": task.day,
                "date": task.date,
                "notes": "",
                "activities": acts,
            }
            return DayWorkerResult(
                day=task.day,
                date=task.date,
                success=True,
                dayplan=dayplan,
            )

        with patch("agent.phase3.orchestrator.run_day_worker", side_effect=fake_worker):
            orch = Phase3Orchestrator(
                plan=plan,
                llm=AsyncMock(),
                tool_engine=AsyncMock(),
                config=Phase3ParallelConfig(
                    max_workers=3, fallback_to_serial=False
                ),
            )
            chunks = []
            async for chunk in orch.run():
                chunks.append(chunk)

        # 初次：1,2,3；再协商后只重派 1 和 2
        assert 1 in call_days and 2 in call_days and 3 in call_days
        assert attempt_by_day[1][0] == 1
        assert 4 in attempt_by_day[1]  # 再协商重派
        assert attempt_by_day[2][0] == 1
        assert 4 in attempt_by_day[2]
        assert attempt_by_day[3] == [1]  # 第 3 天未重派

        assert orch.final_dayplans
        days_ok = {dp["day"] for dp in orch.final_dayplans}
        assert 3 in days_ok
        # 骨架副本已 MOVE
        assert orch._skeleton_copy is not None
        assert "浅草寺" not in orch._skeleton_copy["days"][0]["locked_pois"]
        assert "浅草寺" in orch._skeleton_copy["days"][1]["locked_pois"]
        assert orch._skeleton_amendments

        text = "".join(
            c.content or "" for c in chunks if c.type == ChunkType.TEXT_DELTA
        )
        assert "MOVE" in text or "移" in text or "浅草寺" in text

    @pytest.mark.asyncio
    async def test_infeasible_keeps_partial_delivery_message(self):
        plan = _plan()

        async def fake_worker(**kwargs):
            task = kwargs["task"]
            if task.day == 1:
                return DayWorkerResult(
                    day=1,
                    date=task.date,
                    success=False,
                    dayplan=None,
                    error="排不下",
                    error_code="NEEDS_PHASE3_REPLAN",
                    replan_request=ReplanRequest(
                        day=1, kind="INFEASIBLE_DAY", reason="区域冲突"
                    ),
                )
            return DayWorkerResult(
                day=task.day,
                date=task.date,
                success=True,
                dayplan={
                    "day": task.day,
                    "date": task.date,
                    "notes": "",
                    "activities": [
                        {
                            "name": f"poi-{task.day}",
                            "cost": 0,
                            "start_time": "10:00",
                            "end_time": "11:00",
                        }
                    ],
                },
            )

        with patch("agent.phase3.orchestrator.run_day_worker", side_effect=fake_worker):
            orch = Phase3Orchestrator(
                plan=plan,
                llm=AsyncMock(),
                tool_engine=AsyncMock(),
                config=Phase3ParallelConfig(
                    max_workers=3, fallback_to_serial=False
                ),
            )
            chunks = []
            async for chunk in orch.run():
                chunks.append(chunk)

        text = "".join(
            c.content or "" for c in chunks if c.type == ChunkType.TEXT_DELTA
        )
        assert "骨架分配失败" in text or "INFEASIBLE" in text or "回退" in text
        # 其余天应部分交付
        days_ok = {dp["day"] for dp in orch.final_dayplans}
        assert 2 in days_ok and 3 in days_ok
        assert 1 not in days_ok

    @pytest.mark.asyncio
    async def test_blackboard_rejects_duplicate_poi_across_days(self):
        plan = _plan()

        async def fake_worker(**kwargs):
            task = kwargs["task"]
            # 两天都提交同名 POI：后完成的应被黑板拒绝
            return DayWorkerResult(
                day=task.day,
                date=task.date,
                success=True,
                dayplan={
                    "day": task.day,
                    "date": task.date,
                    "notes": "",
                    "activities": [
                        {
                            "name": "重复景点",
                            "cost": 0,
                            "start_time": "10:00",
                            "end_time": "11:00",
                        }
                    ],
                },
            )

        with patch("agent.phase3.orchestrator.run_day_worker", side_effect=fake_worker):
            orch = Phase3Orchestrator(
                plan=plan,
                llm=AsyncMock(),
                tool_engine=AsyncMock(),
                config=Phase3ParallelConfig(
                    max_workers=1,  # 串行收集，保证先后顺序
                    fallback_to_serial=False,
                ),
            )
            async for _ in orch.run():
                pass

        # 只有一天能登记成功（其余 BLACKBOARD_REJECT 后可能 retry 仍撞）
        accepted_names = []
        for dp in orch.final_dayplans:
            for a in dp.get("activities", []):
                accepted_names.append(a.get("name"))
        # 最多一天保留「重复景点」；若 retry 换不了名则缺天
        assert accepted_names.count("重复景点") <= 1
