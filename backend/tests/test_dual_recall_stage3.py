from dataclasses import replace

from config import Stage3LaneConfig, Stage3RecallConfig, Stage3SemanticConfig
from memory.recall_query import DualRecallPlan
from memory.recall_stage3 import retrieve_dual_recall_candidates
from memory.v3_models import EpisodeSlice, MemoryProfileItem, UserMemoryProfile
from state.models import TravelPlanState


def _config() -> Stage3RecallConfig:
    return replace(
        Stage3RecallConfig(semantic=Stage3SemanticConfig(enabled=False)),
        lexical=Stage3LaneConfig(enabled=True, top_k=20, timeout_ms=20),
    )


def _profile() -> UserMemoryProfile:
    return UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        stable_preferences=[
            MemoryProfileItem(
                id="stable_preferences:hotel:quiet",
                domain="hotel",
                key="quiet",
                value="喜欢安静住宿",
                polarity="prefer",
                stability="stable",
                confidence=0.9,
                status="active",
                applicability="适用于住宿选择。",
                recall_hints={"keywords": ["安静", "住宿"]},
                created_at="2026-04-01T00:00:00",
                updated_at="2026-04-02T00:00:00",
            )
        ],
    )


def _slices() -> list[EpisodeSlice]:
    return [
        EpisodeSlice(
            id="slice_kyoto_hotel",
            user_id="u1",
            source_episode_id="ep1",
            source_trip_id="trip_old",
            slice_type="stay_choice",
            domains=["hotel"],
            entities={"destination": "京都"},
            keywords=["住宿", "安静"],
            content="上次京都住在安静的旅馆。",
            applicability="适用于京都住宿参考。",
            created_at="2026-04-03T00:00:00",
        )
    ]


def _dual(*, need_profile: bool, need_episode: bool) -> DualRecallPlan:
    return DualRecallPlan(
        need_profile=need_profile,
        need_episode=need_episode,
        profile_buckets=["stable_preferences"],
        domains=["hotel"],
        destination="京都",
        keywords=["住宿", "安静"],
        top_k=5,
        reason="test",
    )


def test_dual_stage3_keeps_profile_and_episode_results_separate():
    result = retrieve_dual_recall_candidates(
        query=_dual(need_profile=True, need_episode=True),
        profile=_profile(),
        slices=_slices(),
        user_message="上次京都住宿按我习惯",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=_config(),
    )

    assert [candidate.source for candidate in result.profile.candidates] == ["profile"]
    assert [candidate.source for candidate in result.episode.candidates] == [
        "episode_slice"
    ]
    assert result.profile.telemetry["source"] == "profile"
    assert result.episode.telemetry["source"] == "episode_slice"


def test_dual_stage3_assigns_retrieval_score_for_single_source_candidates():
    result = retrieve_dual_recall_candidates(
        query=_dual(need_profile=False, need_episode=True),
        profile=_profile(),
        slices=_slices(),
        user_message="上次京都住宿",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=_config(),
    )

    assert result.profile.candidates == []
    assert result.episode.candidates
    assert result.episode.candidates[0].retrieval_score > 0.0
