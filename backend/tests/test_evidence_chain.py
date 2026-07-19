"""阶段 A 证据链最小闭环的验收测试。

覆盖增强方案的验收标准：
- 状态序列化、快照恢复不丢 evidence / visit_info / excluded_candidates
- 旧状态（无新字段）向后兼容加载
- UGC 不被写成官方事实（回归）
- 无可靠来源的 anchor 必须 needs_recheck
- 淘汰记录结构化写入
- Phase 4 交付物自动生成需复核与淘汰章节
"""

from __future__ import annotations

import pytest

from state.models import (
    Activity,
    DateRange,
    DayPlan,
    EvidenceRecord,
    ExcludedCandidate,
    TravelPlanState,
    VisitInfo,
)
from state.plan_writers import write_excluded_candidates
from tools.base import ToolError
from tools.generate_summary import make_generate_summary_tool
from tools.plan_tools.daily_plans import make_save_day_plan_tool
from tools.plan_tools.evidence import make_set_excluded_candidates_tool


def _official_evidence() -> dict:
    return {
        "source_type": "official",
        "title": "官网开放信息",
        "summary": "官网：9:00-17:00 开放，无需预约",
        "claim_type": "fact",
        "confidence": "confirmed",
        "source_url": "https://example.com/official",
        "observed_at": "2026-07",
    }


def _ugc_evidence(**overrides) -> dict:
    record = {
        "source_type": "xiaohongshu",
        "title": "近期笔记",
        "summary": "早上 9 点前人少，排队约 20 分钟",
        "claim_type": "experience",
        "confidence": "unverified",
        "observed_at": "2026-06",
    }
    record.update(overrides)
    return record


def _activity_with_visit_info(visit_info: dict | None) -> dict:
    activity = {
        "name": "浅草寺",
        "location": {"name": "浅草寺", "lat": 35.7148, "lng": 139.7967},
        "start_time": "09:00",
        "end_time": "11:00",
        "category": "shrine",
        "cost": 0,
    }
    if visit_info is not None:
        activity["visit_info"] = visit_info
    return activity


# ---------------------------------------------------------------------------
# 模型：序列化往返与向后兼容
# ---------------------------------------------------------------------------


class TestModels:
    def test_evidence_record_roundtrip(self):
        record = EvidenceRecord.from_dict(_official_evidence())
        assert record.source_type == "official"
        assert record.claim_type == "fact"
        assert record.confidence == "confirmed"
        assert EvidenceRecord.from_dict(record.to_dict()).to_dict() == record.to_dict()

    def test_evidence_record_tolerates_invalid_enums(self):
        """快照恢复容错：非法枚举降级为安全默认值，不抛异常。"""
        record = EvidenceRecord.from_dict(
            {"source_type": "dianping", "claim_type": "queue", "confidence": "likely"}
        )
        assert record.source_type == "web"
        assert record.claim_type == "experience"
        assert record.confidence == "unverified"

    def test_activity_roundtrips_visit_info(self):
        act = Activity.from_dict(
            _activity_with_visit_info(
                {
                    "role": "anchor",
                    "recommendation_reason": "画像必去项",
                    "needs_recheck": False,
                    "evidence": [_official_evidence(), _ugc_evidence()],
                }
            )
        )
        assert act.visit_info is not None
        assert act.visit_info.role == "anchor"
        assert len(act.visit_info.evidence) == 2

        restored = Activity.from_dict(act.to_dict())
        assert restored.visit_info is not None
        assert restored.visit_info.evidence[0].source_type == "official"
        assert restored.visit_info.evidence[1].confidence == "unverified"

    def test_activity_without_visit_info_serializes_without_key(self):
        """无 visit_info 的活动字典结构保持不变（旧快照兼容）。"""
        act = Activity.from_dict(_activity_with_visit_info(None))
        assert act.visit_info is None
        assert "visit_info" not in act.to_dict()

    def test_plan_state_roundtrips_excluded_candidates(self):
        plan = TravelPlanState(
            session_id="sess_evidence",
            excluded_candidates=[
                ExcludedCandidate(
                    name="镰仓一日游",
                    reason="距离过远，占用整天且与慢节奏画像冲突",
                    category="distance",
                    reconsider_when="行程延长到 6 天以上",
                    source_candidate_id="poi_kamakura",
                )
            ],
        )
        data = plan.to_dict()
        assert data["excluded_candidates"][0]["category"] == "distance"

        restored = TravelPlanState.from_dict(data)
        assert len(restored.excluded_candidates) == 1
        assert restored.excluded_candidates[0].name == "镰仓一日游"
        assert restored.excluded_candidates[0].reconsider_when == "行程延长到 6 天以上"

    def test_legacy_state_without_new_fields_loads(self):
        """旧快照没有 excluded_candidates / visit_info 时必须正常加载。"""
        legacy = TravelPlanState(session_id="sess_legacy", phase=3).to_dict()
        legacy.pop("excluded_candidates")
        restored = TravelPlanState.from_dict(legacy)
        assert restored.excluded_candidates == []

    def test_backtrack_to_phase1_clears_exclusions_phase2_keeps(self):
        plan = TravelPlanState(
            session_id="sess_bt",
            phase=3,
            excluded_candidates=[
                ExcludedCandidate(name="X", reason="r", category="preference")
            ],
        )
        plan.clear_downstream(from_phase=2)
        assert len(plan.excluded_candidates) == 1  # 上游研究成果保留
        plan.clear_downstream(from_phase=1)
        assert plan.excluded_candidates == []


# ---------------------------------------------------------------------------
# 写入层
# ---------------------------------------------------------------------------


def test_write_excluded_candidates_replaces_wholesale():
    plan = TravelPlanState(session_id="sess_writer")
    write_excluded_candidates(
        plan, [{"name": "A", "reason": "r1", "category": "duplicate"}]
    )
    write_excluded_candidates(
        plan, [{"name": "B", "reason": "r2", "category": "schedule"}]
    )
    assert [c.name for c in plan.excluded_candidates] == ["B"]
    assert plan.excluded_candidates[0].category == "schedule"


# ---------------------------------------------------------------------------
# 工具：set_excluded_candidates
# ---------------------------------------------------------------------------


class TestSetExcludedCandidates:
    def _make_plan(self) -> TravelPlanState:
        plan = TravelPlanState(session_id="sess_excl")
        plan.phase = 2
        return plan

    def test_metadata(self):
        tool_fn = make_set_excluded_candidates_tool(self._make_plan())
        assert tool_fn.name == "set_excluded_candidates"
        assert tool_fn.side_effect == "write"
        assert tool_fn.phases == [2, 3]
        assert tool_fn.parameters["required"] == ["items"]

    @pytest.mark.asyncio
    async def test_happy_path(self):
        plan = self._make_plan()
        tool_fn = make_set_excluded_candidates_tool(plan)
        result = await tool_fn(
            items=[
                {
                    "name": "镰仓一日游",
                    "reason": "单程 1.5 小时，与慢节奏画像冲突",
                    "category": "distance",
                    "reconsider_when": "行程延长到 6 天以上",
                }
            ]
        )
        assert result == {
            "updated_field": "excluded_candidates",
            "count": 1,
            "previous_count": 0,
        }
        assert plan.excluded_candidates[0].category == "distance"

    @pytest.mark.asyncio
    async def test_rejects_invalid_category(self):
        tool_fn = make_set_excluded_candidates_tool(self._make_plan())
        with pytest.raises(ToolError, match="category"):
            await tool_fn(
                items=[{"name": "X", "reason": "r", "category": "vibes"}]
            )

    @pytest.mark.asyncio
    async def test_rejects_missing_reason(self):
        tool_fn = make_set_excluded_candidates_tool(self._make_plan())
        with pytest.raises(ToolError, match="reason"):
            await tool_fn(items=[{"name": "X", "category": "distance"}])


# ---------------------------------------------------------------------------
# 工具：save_day_plan + visit_info（含两条硬规则回归）
# ---------------------------------------------------------------------------


class TestDayPlanVisitInfo:
    def _make_plan(self) -> TravelPlanState:
        plan = TravelPlanState(session_id="sess_visit")
        plan.phase = 3
        plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
        return plan

    @pytest.mark.asyncio
    async def test_valid_visit_info_persists(self):
        plan = self._make_plan()
        tool_fn = make_save_day_plan_tool(plan)
        await tool_fn(
            mode="create",
            day=1,
            date="2026-05-01",
            activities=[
                _activity_with_visit_info(
                    {
                        "role": "anchor",
                        "recommendation_reason": "画像必去项，官方确认开放",
                        "needs_recheck": False,
                        "evidence": [_official_evidence(), _ugc_evidence()],
                    }
                )
            ],
        )
        saved = plan.daily_plans[0].activities[0]
        assert saved.visit_info is not None
        assert saved.visit_info.role == "anchor"
        assert saved.visit_info.evidence[0].confidence == "confirmed"
        # 快照往返不丢证据
        restored = TravelPlanState.from_dict(plan.to_dict())
        assert (
            restored.daily_plans[0].activities[0].visit_info.evidence[1].source_type
            == "xiaohongshu"
        )

    @pytest.mark.asyncio
    async def test_regression_ugc_cannot_be_confirmed_fact(self):
        """回归：小红书证据不允许被写成 confidence=confirmed 的 fact。"""
        tool_fn = make_save_day_plan_tool(self._make_plan())
        with pytest.raises(ToolError, match="UGC_FACT_NOT_CONFIRMABLE|不允许标"):
            await tool_fn(
                mode="create",
                day=1,
                date="2026-05-01",
                activities=[
                    _activity_with_visit_info(
                        {
                            "role": "normal",
                            "recommendation_reason": "笔记说好玩",
                            "evidence": [
                                _ugc_evidence(
                                    claim_type="fact", confidence="confirmed"
                                )
                            ],
                        }
                    )
                ],
            )

    @pytest.mark.asyncio
    async def test_anchor_without_reliable_source_requires_recheck(self):
        """无可靠来源允许推荐，但 anchor 必须显式标 needs_recheck=true。"""
        plan = self._make_plan()
        tool_fn = make_save_day_plan_tool(plan)
        anchor_no_source = {
            "role": "anchor",
            "recommendation_reason": "口碑很好但未查到官方信息",
            "needs_recheck": False,
            "evidence": [_ugc_evidence()],
        }
        with pytest.raises(ToolError, match="anchor"):
            await tool_fn(
                mode="create",
                day=1,
                date="2026-05-01",
                activities=[_activity_with_visit_info(anchor_no_source)],
            )

        anchor_no_source["needs_recheck"] = True
        result = await tool_fn(
            mode="create",
            day=1,
            date="2026-05-01",
            activities=[_activity_with_visit_info(anchor_no_source)],
        )
        assert result["day"] == 1
        assert plan.daily_plans[0].activities[0].visit_info.needs_recheck is True

    @pytest.mark.asyncio
    async def test_activity_without_visit_info_still_accepted(self):
        """最小证据策略：普通活动不强制 visit_info。"""
        plan = self._make_plan()
        tool_fn = make_save_day_plan_tool(plan)
        result = await tool_fn(
            mode="create",
            day=1,
            date="2026-05-01",
            activities=[_activity_with_visit_info(None)],
        )
        assert result["activity_count"] == 1


# ---------------------------------------------------------------------------
# Phase 4 交付物：需复核与淘汰章节
# ---------------------------------------------------------------------------


class TestSummarySections:
    def _make_plan(self) -> TravelPlanState:
        plan = TravelPlanState(session_id="sess_summary", phase=4)
        plan.daily_plans = [
            DayPlan(
                day=1,
                date="2026-05-01",
                activities=[
                    Activity.from_dict(
                        _activity_with_visit_info(
                            {
                                "role": "anchor",
                                "recommendation_reason": "口碑推荐但未查到官方信息",
                                "needs_recheck": True,
                                "evidence": [_ugc_evidence()],
                            }
                        )
                    )
                ],
            )
        ]
        plan.excluded_candidates = [
            ExcludedCandidate(
                name="镰仓一日游",
                reason="距离过远",
                category="distance",
                reconsider_when="行程延长到 6 天以上",
            )
        ]
        return plan

    @pytest.mark.asyncio
    async def test_summary_renders_recheck_and_excluded_sections(self):
        tool_fn = make_generate_summary_tool(self._make_plan())
        result = await tool_fn(
            plan_data={"destination": "东京"},
            title="东京3日旅行计划",
            daily_sections=[{"day": 1, "content": "- 上午浅草寺"}],
            checklist_title="出发前清单",
            checklist_categories=[
                {"category": "证件", "items": ["护照", "签证", "机票单"]}
            ],
        )
        md = result["travel_plan_markdown"]
        assert "## 出发前需复核" in md
        assert "浅草寺" in md
        assert "## 已排除 / 暂缓项目" in md
        assert "镰仓一日游" in md
        assert "行程延长到 6 天以上" in md

    @pytest.mark.asyncio
    async def test_summary_omits_sections_when_no_data(self):
        plan = TravelPlanState(session_id="sess_clean", phase=4)
        tool_fn = make_generate_summary_tool(plan)
        result = await tool_fn(
            plan_data={"destination": "东京"},
            title="东京3日旅行计划",
            daily_sections=[{"day": 1, "content": "- 上午浅草寺"}],
            checklist_title="出发前清单",
            checklist_categories=[
                {"category": "证件", "items": ["护照", "签证", "机票单"]}
            ],
        )
        md = result["travel_plan_markdown"]
        assert "## 出发前需复核" not in md
        assert "## 已排除 / 暂缓项目" not in md
