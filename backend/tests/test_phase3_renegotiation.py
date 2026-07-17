"""D3: 结构化再协商 + 共享黑板 单元/注入测试。"""

from __future__ import annotations

import json
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
    plan.skeleton_plans = [{"id": "plan_A", "name": "平衡版", "days": _skeleton_days()}]
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

    def test_zero_string_target_downgrades_without_assertion(self):
        req = ReplanRequest.from_tool_arguments(
            day=1,
            arguments={
                "kind": "SUGGEST_MOVE",
                "reason": "bad target",
                "move_poi": "浅草寺",
                "to_day": "0",
            },
        )
        assert req.kind == "INFEASIBLE_DAY"
        assert req.to_day is None


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

    def test_suggest_move_does_not_count_candidate_pool_as_committed_capacity(self):
        days = _skeleton_days()
        days[1]["locked_pois"] = ["L1"]
        days[1]["candidate_pois"] = ["A", "B", "C"]
        outcome = renegotiate_skeleton(
            skeleton={"days": days},
            requests=[
                ReplanRequest(
                    day=1,
                    kind="SUGGEST_MOVE",
                    reason="target full",
                    move_poi="浅草寺",
                    to_day=2,
                )
            ],
            pace="balanced",
            renegotiate_count={},
            total_days=3,
        )
        assert outcome.affected_days == {1, 2}
        assert not outcome.unresolved
        assert "浅草寺" in outcome.skeleton_copy["days"][1]["locked_pois"]

    def test_two_day_move_keeps_source_and_target_atomic(self):
        skeleton = {
            "days": [
                {"locked_pois": ["A"], "candidate_pois": []},
                {"locked_pois": [], "candidate_pois": []},
            ]
        }
        outcome = renegotiate_skeleton(
            skeleton=skeleton,
            requests=[
                ReplanRequest(
                    day=1,
                    kind="SUGGEST_MOVE",
                    reason="move",
                    move_poi="A",
                    to_day=2,
                )
            ],
            pace="balanced",
            renegotiate_count={},
            total_days=2,
        )
        assert outcome.affected_days == {1, 2}
        assert "A" in outcome.skeleton_copy["days"][1]["locked_pois"]

    def test_overloaded_trims_candidates(self):
        days = _skeleton_days()
        days[1]["locked_pois"] = ["L1", "L2", "L3"]
        days[1]["candidate_pois"] = ["C1", "C2", "C3"]  # slots left = 1 for balanced
        skeleton = {"days": days}
        counts: dict[int, int] = {}
        outcome = renegotiate_skeleton(
            skeleton=skeleton,
            requests=[ReplanRequest(day=2, kind="OVERLOADED", reason="太多")],
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
            requests=[ReplanRequest(day=1, kind="INFEASIBLE_DAY", reason="根本不行")],
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
                    {
                        "name": "浅草寺",
                        "cost": 0,
                        "start_time": "10:00",
                        "end_time": "11:00",
                    }
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
                    {
                        "name": "A",
                        "cost": 100,
                        "start_time": "09:00",
                        "end_time": "10:00",
                    },
                    {
                        "name": "B",
                        "cost": 200,
                        "start_time": "11:00",
                        "end_time": "12:00",
                    },
                ],
            },
        )
        assert ok is True
        assert reason is None
        assert bb.poi_registry["A"] == 1
        assert bb.budget_ledger[1] == 300.0

    def test_locked_poi_matches_worker_activity_alias(self):
        bb = Phase3Blackboard()
        bb.seed_from_locked({1: ["浅草寺"]})
        ok, reason = bb.try_accept_dayplan(
            2,
            {
                "activities": [
                    {
                        "name": "参观浅草寺",
                        "location": {"name": "浅草寺"},
                        "cost": 0,
                    }
                ]
            },
        )
        assert ok is False
        assert "浅草寺" in (reason or "")

    def test_same_generic_activity_name_does_not_merge_distinct_locations(self):
        bb = Phase3Blackboard()
        first, _ = bb.try_accept_dayplan(
            1,
            {
                "activities": [
                    {
                        "name": "博物馆参观",
                        "location": {"name": "东京国立博物馆"},
                    }
                ]
            },
        )
        second, reason = bb.try_accept_dayplan(
            2,
            {
                "activities": [
                    {
                        "name": "博物馆参观",
                        "location": {"name": "森美术馆"},
                    }
                ]
            },
        )
        assert first is True
        assert second is True
        assert reason is None

    def test_same_location_still_conflicts_when_activity_names_differ(self):
        bb = Phase3Blackboard()
        first, _ = bb.try_accept_dayplan(
            1,
            {
                "activities": [
                    {
                        "name": "上午看展",
                        "location": {"name": "森美术馆"},
                    }
                ]
            },
        )
        second, reason = bb.try_accept_dayplan(
            2,
            {
                "activities": [
                    {
                        "name": "夜间展览",
                        "location": {"name": "森美术馆"},
                    }
                ]
            },
        )
        assert first is True
        assert second is False
        assert "森美术馆" in (reason or "")

    def test_generic_activity_names_do_not_conflict(self):
        bb = Phase3Blackboard()
        first, _ = bb.try_accept_dayplan(1, {"activities": [{"name": "午餐"}]})
        second, _ = bb.try_accept_dayplan(2, {"activities": [{"name": "午餐"}]})
        assert first is True
        assert second is True

    def test_generic_location_name_falls_back_to_activity_name_for_dedup(self):
        """C2：location.name 泛化（产不出 key）时须回退 activity.name 防重复。"""
        bb = Phase3Blackboard()
        first, _ = bb.try_accept_dayplan(
            1,
            {"activities": [{"name": "浅草寺", "location": {"name": "自由活动"}}]},
        )
        second, reason = bb.try_accept_dayplan(
            2,
            {"activities": [{"name": "浅草寺", "location": {"name": "自由活动"}}]},
        )
        assert first is True
        assert second is False
        assert "浅草寺" in (reason or "")

    def test_invalid_day_boundary_is_rejected(self):
        bb = Phase3Blackboard()
        ok, reason = bb.try_accept_dayplan(
            1,
            {"activities": [{"name": "A", "start_time": "18:00", "end_time": "17:00"}]},
        )
        assert ok is False
        assert "边界" in (reason or "")

    def test_out_of_order_activities_are_not_rejected(self):
        """C3：边界校验不得依赖 activities 已按时间排序。"""
        bb = Phase3Blackboard()
        ok, reason = bb.try_accept_dayplan(
            1,
            {
                "activities": [
                    {"name": "夜景", "start_time": "20:00", "end_time": "22:00"},
                    {"name": "早市", "start_time": "09:00", "end_time": "10:00"},
                ]
            },
        )
        assert ok is True
        assert reason is None

    def test_cross_midnight_end_time_is_not_rejected(self):
        """C3：末活动跨午夜（end 落在凌晨）是合法夜间行程。"""
        bb = Phase3Blackboard()
        ok, reason = bb.try_accept_dayplan(
            1,
            {
                "activities": [
                    {"name": "白天活动", "start_time": "09:00", "end_time": "18:00"},
                    {"name": "深夜食堂", "start_time": "22:00", "end_time": "01:00"},
                ]
            },
        )
        assert ok is True
        assert reason is None

    def test_budget_overrun_rejects(self):
        bb = Phase3Blackboard(total_budget=500, precommitted_cost=400)
        ok, reason = bb.try_accept_dayplan(
            1,
            {
                "day": 1,
                "date": "2026-05-01",
                "activities": [
                    {
                        "name": "A",
                        "cost": 200,
                        "start_time": "09:00",
                        "end_time": "10:00",
                    },
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
                    {
                        "name": "A",
                        "cost": 10,
                        "start_time": "09:00",
                        "end_time": "10:00",
                    },
                ],
            },
        )
        bb.release_day(1)
        assert "A" not in bb.poi_registry
        assert 1 not in bb.budget_ledger


class TestOrchestratorRenegotiation:
    def test_blackboard_precommitted_budget_reuses_authoritative_shape(self):
        plan = _plan()
        plan.selected_transport = {"segments": [{"price": 3000}]}
        plan.accommodation = Accommodation(area="新宿", hotel="A")
        plan.accommodation_options = [{"name": "A", "price_per_night": 750}]
        orch = Phase3Orchestrator(
            plan=plan,
            llm=AsyncMock(),
            tool_engine=AsyncMock(),
            config=Phase3ParallelConfig(),
        )
        orch._init_blackboard([])
        assert orch._blackboard.precommitted_cost == 5250

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
                config=Phase3ParallelConfig(max_workers=3, fallback_to_serial=False),
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
    async def test_failed_renegotiation_redispatch_restores_previous_version(
        self, tmp_path
    ):
        """C1：重派前乐观作废旧候选后重派失败 → 回滚旧版本，不得静默丢天。"""
        plan = _plan()
        plan.skeleton_plans[0]["days"][2]["locked_pois"] = ["上野公园"]
        plan.skeleton_plans[0]["days"][2]["candidate_pois"] = []

        async def fake_worker(**kwargs):
            task = kwargs["task"]
            attempt = kwargs["attempt"]
            if task.day == 2 and attempt == 1:
                return DayWorkerResult(
                    day=2,
                    date=task.date,
                    success=False,
                    dayplan=None,
                    error="move P2",
                    error_code="NEEDS_PHASE3_REPLAN",
                    replan_request=ReplanRequest(
                        day=2,
                        kind="SUGGEST_MOVE",
                        reason="P2 should move",
                        move_poi="明治神宫",
                        to_day=1,
                    ),
                )

            if task.day == 1 and attempt >= 3:
                # 再协商重派（attempt=4）与 8b 修复（attempt=3）都失败
                return DayWorkerResult(
                    day=1,
                    date=task.date,
                    success=False,
                    dayplan=None,
                    error="redispatch failed before submission",
                    error_code="LLM_ERROR",
                )
            elif task.locked_pois:
                name = task.locked_pois[0]
            else:
                name = f"replacement-{task.day}-{attempt}"
            dayplan = {
                "day": task.day,
                "date": task.date,
                "activities": [
                    {
                        "name": name,
                        "location": {"name": name},
                        "cost": 0,
                        "start_time": "10:00",
                        "end_time": "11:00",
                    }
                ],
            }
            kwargs["candidate_store"].submit_candidate(
                plan.session_id,
                kwargs["run_id"],
                f"day_{task.day}_attempt_{attempt}",
                task.day,
                attempt,
                dayplan,
            )
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
                    max_workers=1,
                    fallback_to_serial=False,
                    artifact_root=str(tmp_path),
                ),
            )
            async for _ in orch.run():
                pass

        # 第 1 天重派失败后必须回滚交付旧版本（浅草寺），而非缺席
        assert {dayplan["day"] for dayplan in orch.final_dayplans} == {1, 2, 3}
        day1 = next(dp for dp in orch.final_dayplans if dp["day"] == 1)
        assert [a["name"] for a in day1["activities"]] == ["浅草寺"]
        # 磁盘口径一致：旧候选被重新标 accepted 并成为最新版本
        artifact_path = next(
            (tmp_path / plan.session_id).glob("*/day_1_attempt_1.json")
        )
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert payload["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_failed_8b_repair_keeps_delivery_and_blackboard_claim(
        self, tmp_path
    ):
        """C1：8b 修复重派失败 → 该天仍交付旧计划，且黑板认领被恢复。"""
        plan = _plan()
        attempts_by_day: dict[int, list[int]] = {}

        async def fake_worker(**kwargs):
            task = kwargs["task"]
            attempt = kwargs["attempt"]
            attempts_by_day.setdefault(task.day, []).append(attempt)
            if task.day == 1 and attempt >= 2:
                return DayWorkerResult(
                    day=1,
                    date=task.date,
                    success=False,
                    dayplan=None,
                    error="repair failed",
                    error_code="LLM_ERROR",
                )
            if task.day == 1:
                # 缺 locked POI 浅草寺 → 触发 8b locked_poi_missing 修复
                name = "错位活动"
            else:
                name = (task.locked_pois or [f"poi-{task.day}"])[0]
            dayplan = {
                "day": task.day,
                "date": task.date,
                "activities": [
                    {
                        "name": name,
                        "location": {"name": name},
                        "cost": 0,
                        "start_time": "10:00",
                        "end_time": "11:00",
                    }
                ],
            }
            kwargs["candidate_store"].submit_candidate(
                plan.session_id,
                kwargs["run_id"],
                f"day_{task.day}_attempt_{attempt}",
                task.day,
                attempt,
                dayplan,
            )
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
                    max_workers=1,
                    fallback_to_serial=False,
                    artifact_root=str(tmp_path),
                ),
            )
            async for _ in orch.run():
                pass

        # 8b 修复触发且失败
        assert 3 in attempts_by_day.get(1, [])
        # 交付保留旧计划
        assert {dayplan["day"] for dayplan in orch.final_dayplans} == {1, 2, 3}
        day1 = next(dp for dp in orch.final_dayplans if dp["day"] == 1)
        assert [a["name"] for a in day1["activities"]] == ["错位活动"]
        # 黑板认领被恢复：后续跨天修复不能抢占该 POI
        assert orch._blackboard.poi_registry.get("错位活动") == 1

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
                config=Phase3ParallelConfig(max_workers=3, fallback_to_serial=False),
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

    @pytest.mark.asyncio
    async def test_rejected_disk_candidate_is_not_delivered(self, tmp_path):
        plan = _plan()

        async def fake_worker(**kwargs):
            task = kwargs["task"]
            attempt = kwargs["attempt"]
            dayplan = {
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
            }
            kwargs["candidate_store"].submit_candidate(
                plan.session_id,
                kwargs["run_id"],
                f"day_{task.day}_attempt_{attempt}",
                task.day,
                attempt,
                dayplan,
            )
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
                    max_workers=1,
                    fallback_to_serial=False,
                    artifact_root=str(tmp_path),
                ),
            )
            async for _ in orch.run():
                pass

        delivered = [
            activity["name"]
            for dayplan in orch.final_dayplans
            for activity in dayplan.get("activities", [])
        ]
        assert delivered.count("重复景点") == 1

    @pytest.mark.asyncio
    async def test_blackboard_reject_reason_reaches_retry_hint(self, tmp_path):
        plan = _plan()
        calls: list[tuple[int, int, list[str]]] = []

        async def fake_worker(**kwargs):
            task = kwargs["task"]
            attempt = kwargs["attempt"]
            calls.append((task.day, attempt, list(task.repair_hints or [])))
            name = "共享候选" if attempt == 1 or task.day == 1 else f"替代-{task.day}"
            return DayWorkerResult(
                day=task.day,
                date=task.date,
                success=True,
                dayplan={
                    "day": task.day,
                    "date": task.date,
                    "activities": [{"name": name, "cost": 0}],
                },
            )

        with patch("agent.phase3.orchestrator.run_day_worker", side_effect=fake_worker):
            orch = Phase3Orchestrator(
                plan=plan,
                llm=AsyncMock(),
                tool_engine=AsyncMock(),
                config=Phase3ParallelConfig(
                    max_workers=1,
                    fallback_to_serial=False,
                    artifact_root=str(tmp_path),
                ),
            )
            async for _ in orch.run():
                pass

        retry_hints = [hints for _, attempt, hints in calls if attempt == 2]
        assert retry_hints
        assert any("认领" in hint for hints in retry_hints for hint in hints)
