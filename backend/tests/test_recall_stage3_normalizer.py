from config import Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3_normalizer import build_query_envelope
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
    assert envelope.expanded_keywords == ("住哪里",)


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
