import pytest

from config import MemoryRetrievalConfig, Stage3RecallConfig, Stage3SemanticConfig
from memory.manager import MemoryManager
from memory.recall_query import DualRecallPlan
from memory.retrieval_candidates import RecallCandidate
from memory.v3_models import UserMemoryProfile
from state.models import TravelPlanState


@pytest.mark.asyncio
async def test_memory_manager_formats_dual_recall_results(monkeypatch, tmp_path):
    manager = MemoryManager(
        data_dir=str(tmp_path),
        retrieval_config=MemoryRetrievalConfig(
            stage3=Stage3RecallConfig(semantic=Stage3SemanticConfig(enabled=False))
        ),
    )

    async def load_profile(user_id):
        return UserMemoryProfile.empty(user_id)

    async def load_working_memory(user_id, session_id, trip_id):
        from memory.v3_models import SessionWorkingMemory

        return SessionWorkingMemory.empty(user_id, session_id, trip_id)

    async def list_episode_slices(user_id, destination=None):
        return []

    monkeypatch.setattr(manager.v3_store, "load_profile", load_profile)
    monkeypatch.setattr(manager.v3_store, "load_working_memory", load_working_memory)
    monkeypatch.setattr(manager.v3_store, "list_episode_slices", list_episode_slices)

    profile_candidate = RecallCandidate(
        source="profile",
        item_id="constraints:hotel:no_smoking",
        bucket="constraints",
        score=1.0,
        retrieval_score=1.0,
        matched_reason=["test"],
        content_summary="hotel:no_smoking=必须无烟房",
        domains=["hotel"],
        applicability="适用于住宿。",
        key="no_smoking",
    )
    episode_candidate = RecallCandidate(
        source="episode_slice",
        item_id="slice_1",
        bucket="stay_choice",
        score=1.0,
        retrieval_score=1.0,
        matched_reason=["test"],
        content_summary="上次京都住在安静旅馆。",
        domains=["hotel"],
        applicability="历史案例。",
    )

    from memory.recall_stage3_models import (
        DualStage3RecallResult,
        SourceStage3RecallResult,
    )

    def fake_retrieve_dual_recall_candidates(**kwargs):
        return DualStage3RecallResult(
            profile=SourceStage3RecallResult(
                source="profile",
                candidates=[profile_candidate],
                evidence_by_id={},
                telemetry={"source": "profile"},
            ),
            episode=SourceStage3RecallResult(
                source="episode_slice",
                candidates=[episode_candidate],
                evidence_by_id={},
                telemetry={"source": "episode_slice"},
            ),
        )

    monkeypatch.setattr(
        "memory.manager.retrieve_dual_recall_candidates",
        fake_retrieve_dual_recall_candidates,
    )

    context, telemetry = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="t1", destination="京都"),
        user_message="上次京都住宿按我的习惯",
        recall_gate=True,
        retrieval_plan=DualRecallPlan(
            need_profile=True,
            need_episode=True,
            profile_buckets=["constraints"],
            domains=["hotel"],
            destination="京都",
            keywords=["住宿"],
            top_k=5,
            reason="test",
        ),
    )

    assert "## User Profile Memory" in context
    assert "## Relevant Past Episodes" in context
    assert telemetry.profile_ids == ["constraints:hotel:no_smoking"]
    assert telemetry.slice_ids == ["slice_1"]
    assert telemetry.profile_reranker_selected_ids == ["constraints:hotel:no_smoking"]
    assert telemetry.episode_reranker_selected_ids == ["slice_1"]
