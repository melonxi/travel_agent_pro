from config import Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3_models import RetrievalEvidence, Stage3Telemetry
from memory.recall_stage3_normalizer import (
    _expand_domains,
    _expand_keywords,
    build_query_envelope,
)
from state.models import TravelPlanState


def _plan(**overrides) -> RecallRetrievalPlan:
    values = {
        "source": "profile",
        "buckets": ["stable_preferences"],
        "domains": ["hotel"],
        "destination": "東京",
        "keywords": ["住哪里"],
        "top_k": 5,
        "reason": "test",
    }
    values.update(overrides)
    return RecallRetrievalPlan(**values)


def test_build_query_envelope_preserves_default_profile_source_policy() -> None:
    envelope = build_query_envelope(
        query=_plan(),
        user_message="住宿按我习惯来",
        plan=TravelPlanState(session_id="s1", trip_id="t1"),
        config=Stage3RecallConfig(),
    )

    assert envelope.source_policy.requested_source == "profile"
    assert envelope.source_policy.search_profile is True
    assert envelope.source_policy.search_slices is False
    assert envelope.source_policy.widened is False
    assert envelope.destination == "東京"
    assert envelope.destination_canonical == ""
    assert envelope.expanded_keywords == (
        "住哪里",
        "住宿",
        "酒店",
        "民宿",
        "旅馆",
        "住宿按我习惯来",
    )


def test_build_query_envelope_uses_episode_slice_source_policy() -> None:
    envelope = build_query_envelope(
        query=_plan(source="episode_slice", buckets=[]),
        user_message="上次东京住哪里",
        plan=TravelPlanState(session_id="s1", trip_id="t1"),
        config=Stage3RecallConfig(),
    )

    assert envelope.source_policy.requested_source == "episode_slice"
    assert envelope.source_policy.search_profile is False
    assert envelope.source_policy.search_slices is True
    assert envelope.source_policy.widened is False


def test_build_query_envelope_uses_hybrid_history_source_policy() -> None:
    envelope = build_query_envelope(
        query=_plan(source="hybrid_history"),
        user_message="上次东京住哪里",
        plan=TravelPlanState(session_id="s1", trip_id="t1"),
        config=Stage3RecallConfig(),
    )

    assert envelope.source_policy.requested_source == "hybrid_history"
    assert envelope.source_policy.search_profile is True
    assert envelope.source_policy.search_slices is True
    assert envelope.source_policy.widened is False


def test_build_query_envelope_expands_destination_when_enabled() -> None:
    envelope = build_query_envelope(
        query=_plan(),
        user_message="上次东京住哪里",
        plan=TravelPlanState(session_id="s1", trip_id="t1", destination="东京"),
        config=Stage3RecallConfig(destination_normalization_enabled=True),
    )

    assert envelope.destination == "東京"
    assert envelope.destination_canonical == "东京"
    assert "東京" in envelope.destination_aliases
    assert envelope.destination_region == "关东"


def test_build_query_envelope_expands_hotel_keywords() -> None:
    envelope = build_query_envelope(
        query=_plan(keywords=["住宿"]),
        user_message="我上次住的地方怎么样",
        plan=TravelPlanState(session_id="s1", trip_id="t1"),
        config=Stage3RecallConfig(),
    )

    assert "住宿" in envelope.expanded_keywords
    assert "酒店" in envelope.expanded_keywords
    assert "民宿" in envelope.expanded_keywords


def test_build_query_envelope_expands_keywords_from_user_message() -> None:
    envelope = build_query_envelope(
        query=_plan(keywords=["预算"]),
        user_message="这次想住民宿",
        plan=TravelPlanState(session_id="s1", trip_id="t1"),
        config=Stage3RecallConfig(),
    )

    assert envelope.expanded_keywords == (
        "预算",
        "这次想住民宿",
        "民宿",
        "住宿",
        "酒店",
        "住哪里",
    )


def test_build_query_envelope_requires_keyword_arguments() -> None:
    try:
        build_query_envelope(
            _plan(),
            "住宿按我习惯来",
            TravelPlanState(session_id="s1", trip_id="t1"),
            Stage3RecallConfig(),
        )
    except TypeError:
        return

    raise AssertionError("build_query_envelope should require keyword arguments")


def test_expansion_helpers_return_lists() -> None:
    assert _expand_domains(["hotel"]) == ["hotel", "accommodation"]
    assert _expand_keywords(["住哪里"]) == ["住哪里", "住宿", "酒店", "民宿", "旅馆"]


def test_expand_keywords_preserves_original_non_empty_values() -> None:
    assert _expand_keywords([" 住哪里 ", "   ", " 住宿按我习惯来 "]) == [
        " 住哪里 ",
        "住哪里",
        "住宿",
        "酒店",
        "民宿",
        "旅馆",
        " 住宿按我习惯来 ",
    ]


def test_retrieval_evidence_to_dict_returns_shape_and_copies() -> None:
    evidence = RetrievalEvidence(
        item_id="i1",
        source="profile",
        lanes=["symbolic"],
        lane_scores={"symbolic": 0.9},
        lane_ranks={"symbolic": 1},
        fused_score=0.8,
        matched_domains=["hotel"],
        matched_keywords=["住宿"],
        matched_entities=["东京"],
        destination_match_type="alias",
        semantic_score=0.7,
        lexical_score=0.6,
        temporal_score=0.5,
        retrieval_reason="matched hotel preference",
    )

    data = evidence.to_dict()
    assert data == {
        "item_id": "i1",
        "source": "profile",
        "lanes": ["symbolic"],
        "lane_scores": {"symbolic": 0.9},
        "lane_ranks": {"symbolic": 1},
        "fused_score": 0.8,
        "matched_domains": ["hotel"],
        "matched_keywords": ["住宿"],
        "matched_entities": ["东京"],
        "destination_match_type": "alias",
        "semantic_score": 0.7,
        "lexical_score": 0.6,
        "temporal_score": 0.5,
        "retrieval_reason": "matched hotel preference",
    }

    data["lanes"].append("lexical")
    data["lane_scores"]["lexical"] = 0.4
    assert evidence.lanes == ["symbolic"]
    assert evidence.lane_scores == {"symbolic": 0.9}


def test_stage3_telemetry_to_dict_returns_shape_and_copies() -> None:
    telemetry = Stage3Telemetry(
        lanes_attempted=["symbolic"],
        lanes_succeeded=["symbolic"],
        source_policy={"requested_source": "profile"},
        query_expansion={"keywords": ["住宿"], "domains": ["hotel"]},
        candidates_by_lane={"symbolic": 2},
        candidates_by_source={"profile": 2},
        total_candidates_before_fusion=2,
        total_candidates_after_fusion=1,
        zero_hit=False,
        fallback_used="none",
        lane_errors={"semantic": "disabled"},
    )

    data = telemetry.to_dict()
    assert data == {
        "lanes_attempted": ["symbolic"],
        "lanes_succeeded": ["symbolic"],
        "source_policy": {"requested_source": "profile"},
        "query_expansion": {"keywords": ["住宿"], "domains": ["hotel"]},
        "candidates_by_lane": {"symbolic": 2},
        "candidates_by_source": {"profile": 2},
        "total_candidates_before_fusion": 2,
        "total_candidates_after_fusion": 1,
        "zero_hit": False,
        "fallback_used": "none",
        "lane_errors": {"semantic": "disabled"},
    }

    data["lanes_attempted"].append("lexical")
    data["query_expansion"]["keywords"].append("酒店")
    assert telemetry.lanes_attempted == ["symbolic"]
    assert telemetry.query_expansion == {"keywords": ["住宿"], "domains": ["hotel"]}
