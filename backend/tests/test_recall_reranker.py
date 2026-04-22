from memory.recall_reranker import (
    RecallRerankResult,
    choose_reranker_path,
    parse_recall_reranker_arguments,
)
from memory.retrieval_candidates import RecallCandidate


def make_candidate(**overrides) -> RecallCandidate:
    base = dict(
        source="profile",
        item_id="profile_1",
        bucket="stable_preferences",
        score=1.0,
        matched_reason=["domain=hotel"],
        content_summary="hotel:preferred_area=京都四条",
        domains=["hotel"],
        applicability="适用于大多数住宿选择。",
    )
    base.update(overrides)
    return RecallCandidate(**base)


def test_parse_recall_reranker_arguments_returns_selected_ids_and_reasons():
    result = parse_recall_reranker_arguments(
        {
            "selected_item_ids": ["profile_1", "slice_2"],
            "final_reason": "these two items directly answer the user's lodging question",
            "per_item_reason": {
                "profile_1": "long-term lodging preference still applies",
                "slice_2": "past Kyoto lodging experience is directly relevant",
            },
        }
    )

    assert result == RecallRerankResult(
        selected_item_ids=["profile_1", "slice_2"],
        final_reason="these two items directly answer the user's lodging question",
        per_item_reason={
            "profile_1": "long-term lodging preference still applies",
            "slice_2": "past Kyoto lodging experience is directly relevant",
        },
        fallback_used="none",
    )


def test_parse_recall_reranker_arguments_falls_back_for_invalid_payload():
    result = parse_recall_reranker_arguments(
        {
            "selected_item_ids": ["profile_1"],
            "final_reason": 123,
            "per_item_reason": {"profile_1": "still applies"},
        }
    )

    assert result == RecallRerankResult(
        selected_item_ids=[],
        final_reason="invalid_reranker_payload",
        per_item_reason={},
        fallback_used="invalid_reranker_payload",
    )


def test_choose_reranker_path_skips_llm_when_candidate_count_is_small():
    candidates = [
        make_candidate(item_id="profile_1"),
        make_candidate(
            source="episode_slice",
            item_id="slice_1",
            bucket="accommodation_decision",
            score=0.5,
            matched_reason=["destination=京都"],
            content_summary="上次京都住四条附近的町屋。",
        ),
    ]

    path = choose_reranker_path(candidates, rerank_threshold=3, fallback_top_n=3)

    assert path.should_call_llm is False
    assert [candidate.item_id for candidate in path.selected_candidates] == [
        "profile_1",
        "slice_1",
    ]
    assert path.fallback_used == "skipped_small_candidate_set"


def test_choose_reranker_path_prefers_top_n_when_llm_path_is_needed():
    candidates = [
        make_candidate(item_id="profile_1", score=1.0),
        make_candidate(item_id="profile_2", score=0.75),
        make_candidate(item_id="profile_3", score=0.5),
        make_candidate(item_id="profile_4", score=0.25),
    ]

    path = choose_reranker_path(candidates, rerank_threshold=3, fallback_top_n=2)

    assert path.should_call_llm is True
    assert [candidate.item_id for candidate in path.selected_candidates] == [
        "profile_1",
        "profile_2",
    ]
    assert path.fallback_used == "none"
