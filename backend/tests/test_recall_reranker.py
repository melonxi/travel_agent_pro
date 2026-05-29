from datetime import datetime, timedelta, timezone

import pytest

from config import EpisodeRerankConfig, ProfileRerankConfig
from memory.recall_reranker import (
    _conflict_score,
    _duplicate_group,
    _jaccard,
    _recency_score,
    empty_rerank_result,
    rerank_episode_candidates,
    rerank_profile_candidates,
)
from memory.retrieval_candidates import RecallCandidate


def make_candidate(**overrides) -> RecallCandidate:
    base = dict(
        source="profile",
        item_id="profile_1",
        bucket="stable_preferences",
        score=1.0,
        retrieval_score=1.0,
        matched_reason=["domain=hotel"],
        content_summary="hotel:preferred_area=京都四条",
        domains=["hotel"],
        applicability="适用于大多数住宿选择。",
        polarity="prefer",
        key="preferred_area",
        created_at="2026-04-01T00:00:00",
    )
    base.update(overrides)
    return RecallCandidate(**base)


def test_empty_rerank_result_keeps_selection_metrics_placeholder():
    result = empty_rerank_result()

    assert result.selection_metrics == {
        "selected_pairwise_similarity_max": None,
        "selected_pairwise_similarity_avg": None,
    }


def test_rerank_profile_prefers_constraints_and_confidence():
    candidates = [
        make_candidate(
            item_id="stable_preferences:hotel:quiet",
            bucket="stable_preferences",
            content_summary="hotel:quiet=喜欢安静住宿",
            key="quiet",
            polarity="prefer",
            score=0.7,
            retrieval_score=0.7,
        ),
        make_candidate(
            item_id="constraints:hotel:no_smoking",
            bucket="constraints",
            content_summary="hotel:no_smoking=必须无烟房",
            key="no_smoking",
            polarity="must",
            score=0.6,
            retrieval_score=0.6,
        ),
    ]

    result = rerank_profile_candidates(
        candidates=candidates,
        user_message="住宿按我的要求",
        destination="",
        domains=["hotel"],
        keywords=["住宿"],
        config=ProfileRerankConfig(profile_top_n=2),
    )

    assert result.selected_item_ids[0] == "constraints:hotel:no_smoking"
    assert "profile rerank selected 2 items" in result.final_reason
    assert result.intent_label == "profile"


def test_rerank_profile_ignores_episode_candidates():
    result = rerank_profile_candidates(
        candidates=[
            make_candidate(
                source="episode_slice",
                item_id="slice_1",
                bucket="stay_choice",
                content_summary="上次京都住在安静旅馆。",
            )
        ],
        user_message="住宿按我的要求",
        destination="京都",
        domains=["hotel"],
        keywords=["住宿"],
        config=ProfileRerankConfig(profile_top_n=2),
    )

    assert result.selected_item_ids == []
    assert result.per_item_scores == {}


def test_rerank_episode_keeps_semantic_only_retrieval_score():
    candidates = [
        make_candidate(
            item_id="slice_semantic",
            source="episode_slice",
            bucket="stay_choice",
            domains=[],
            content_summary="以前住过一个安静小旅馆，体验很好。",
            score=0.92,
            retrieval_score=0.92,
        )
    ]

    result = rerank_episode_candidates(
        candidates=candidates,
        user_message="找个安静住宿",
        destination="",
        domains=[],
        keywords=[],
        config=EpisodeRerankConfig(episode_top_n=1),
    )

    assert result.selected_item_ids == ["slice_semantic"]
    assert result.per_item_scores["slice_semantic"]["retrieval_score"] == 0.92


def test_rerank_episode_filters_rejected_option_when_user_asks_positive_specific_option():
    candidates = [
        make_candidate(
            item_id="slice_rejected",
            source="episode_slice",
            bucket="rejected_option",
            content_summary="上次明确拒绝住青旅。",
            score=0.9,
            retrieval_score=0.9,
        )
    ]

    result = rerank_episode_candidates(
        candidates=candidates,
        user_message="这次想住青旅",
        destination="",
        domains=["hotel"],
        keywords=["青旅"],
        config=EpisodeRerankConfig(episode_top_n=1),
    )

    assert result.selected_item_ids == []
    assert "filtered: conflict" in result.per_item_reason["slice_rejected"]


def test_rerank_episode_dedupes_redundant_slices():
    candidates = [
        make_candidate(
            item_id="slice_a",
            source="episode_slice",
            bucket="stay_choice",
            content_summary="上次京都住在安静旅馆。",
            retrieval_score=0.8,
        ),
        make_candidate(
            item_id="slice_b",
            source="episode_slice",
            bucket="stay_choice",
            content_summary="上次京都住在安静旅馆。",
            retrieval_score=0.7,
        ),
    ]

    result = rerank_episode_candidates(
        candidates=candidates,
        user_message="上次京都住宿",
        destination="京都",
        domains=["hotel"],
        keywords=["住宿"],
        config=EpisodeRerankConfig(episode_top_n=2),
    )

    assert result.selected_item_ids == ["slice_a"]


def test_conflict_score_drops_profile_avoid_when_user_asks_positive_specific_option():
    candidate = make_candidate(
        item_id="rejections:hotel:hostel",
        bucket="rejections",
        content_summary="hotel:hostel=不住青旅",
        polarity="avoid",
        key="hostel",
    )

    assert _conflict_score(candidate, "这次想住青旅") == 1.0


def test_duplicate_group_preserves_profile_items_with_different_polarity():
    prefer = make_candidate(
        item_id="stable_preferences:hotel:quiet",
        key="quiet",
        polarity="prefer",
    )
    avoid = make_candidate(
        item_id="rejections:hotel:quiet",
        bucket="rejections",
        key="quiet",
        polarity="avoid",
    )

    assert _duplicate_group(prefer) != _duplicate_group(avoid)


def test_recency_score_handles_tz_aware_created_at():
    recent = datetime.now(timezone.utc) - timedelta(days=10)
    candidate = make_candidate(created_at=recent.isoformat())

    assert _recency_score(candidate, half_life_days=30) == pytest.approx(
        0.7937005259,
        rel=0.2,
    )


def test_jaccard_scores_overlap():
    assert _jaccard({"hotel", "quiet"}, {"hotel", "food"}) == pytest.approx(1 / 3)
