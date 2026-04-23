from dataclasses import dataclass

from memory.recall_query import RecallRetrievalPlan
from memory.recall_reranker import choose_reranker_path
from memory.retrieval_candidates import RecallCandidate
from state.models import TravelPlanState, Travelers


@dataclass(frozen=True)
class DummyRerankerConfig:
    small_candidate_set_threshold: int = 3
    profile_top_n: int = 4
    slice_top_n: int = 3
    hybrid_top_n: int = 4
    hybrid_profile_top_n: int = 2
    hybrid_slice_top_n: int = 2
    recency_half_life_days: int = 180


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
        polarity="prefer",
        created_at="2026-04-01T00:00:00",
    )
    base.update(overrides)
    return RecallCandidate(**base)


def test_choose_reranker_path_skips_scoring_when_candidate_set_is_small():
    candidates = [
        make_candidate(
            item_id="profile_1",
            matched_reason=["exact domain match on hotel"],
        ),
        make_candidate(
            source="episode_slice",
            item_id="slice_1",
            bucket="accommodation_decision",
            score=0.5,
            matched_reason=["exact destination match on 京都"],
            content_summary="上次京都住四条附近的町屋。",
            domains=["hotel"],
            applicability="仅供住宿选择参考。",
            polarity="",
        ),
    ]

    path = choose_reranker_path(
        candidates=candidates,
        user_message="我上次去京都住哪里？",
        plan=TravelPlanState(session_id="s1", trip_id="trip_now", destination="京都"),
        retrieval_plan=RecallRetrievalPlan(
            source="episode_slice",
            buckets=[],
            domains=["hotel"],
            destination="京都",
            keywords=["住宿"],
            top_k=5,
            reason="past_trip_experience_recall -> Kyoto hotel slice lookup",
        ),
        config=DummyRerankerConfig(small_candidate_set_threshold=3),
    )

    assert [candidate.item_id for candidate in path.selected_candidates] == [
        "profile_1",
        "slice_1",
    ]
    assert path.result.fallback_used == "skipped_small_candidate_set"
    assert "small candidate set" in path.result.final_reason
    assert "exact domain match on hotel" in path.result.per_item_reason["profile_1"]


def test_choose_reranker_path_prefers_profile_constraints_for_preference_queries():
    candidates = [
        make_candidate(
            item_id="constraint_avoid_red_eye",
            bucket="constraints",
            polarity="avoid",
            matched_reason=["exact domain match on flight", "keyword match on 红眼"],
            content_summary="flight:avoid_red_eye=true",
            domains=["flight"],
            applicability="适用于所有旅行。",
        ),
        make_candidate(
            item_id="stable_window_pref",
            bucket="stable_preferences",
            polarity="prefer",
            matched_reason=["exact domain match on flight", "keyword match on 靠窗"],
            content_summary="flight:seat_preference=靠窗",
            domains=["flight"],
            applicability="适用于大多数航班选择。",
        ),
        make_candidate(
            source="episode_slice",
            item_id="slice_kyoto_red_eye",
            bucket="transport_choice",
            score=0.4,
            matched_reason=["domain match on flight", "keyword match on 红眼"],
            content_summary="上次京都行程为了省钱选了红眼航班，第二天状态很差。",
            domains=["flight"],
            applicability="仅供交通方式参考；班次和出发条件变化时需重新判断。",
            polarity="",
        ),
    ]

    path = choose_reranker_path(
        candidates=candidates,
        user_message="按我偏好，这次机票别选红眼航班",
        plan=TravelPlanState(session_id="s1", trip_id="trip_now"),
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["constraints", "stable_preferences"],
            domains=["flight"],
            destination="",
            keywords=["机票", "红眼"],
            top_k=5,
            reason="profile_constraint_recall -> flight preference profile",
        ),
        config=DummyRerankerConfig(small_candidate_set_threshold=1, profile_top_n=2),
    )

    assert [candidate.item_id for candidate in path.selected_candidates] == [
        "constraint_avoid_red_eye",
        "stable_window_pref",
    ]
    assert "source-aware weighted rerank" in path.result.final_reason
    assert "bucket=" in path.result.per_item_reason["constraint_avoid_red_eye"]


def test_choose_reranker_path_drops_conflicting_profile_candidate():
    candidates = [
        make_candidate(
            item_id="constraint_avoid_red_eye",
            bucket="constraints",
            polarity="avoid",
            matched_reason=["exact domain match on flight", "keyword match on 红眼"],
            content_summary="flight:avoid_red_eye=true",
            domains=["flight"],
            applicability="适用于所有旅行。",
        ),
        make_candidate(
            source="episode_slice",
            item_id="slice_recent_red_eye",
            bucket="transport_choice",
            score=0.5,
            matched_reason=["domain match on flight", "keyword match on 红眼"],
            content_summary="上次东京行程坐红眼虽然便宜，但状态很差。",
            domains=["flight"],
            applicability="仅供交通方式参考；班次和出发条件变化时需重新判断。",
            polarity="",
        ),
    ]

    path = choose_reranker_path(
        candidates=candidates,
        user_message="这次为了省预算，可以坐红眼航班",
        plan=TravelPlanState(session_id="s1", trip_id="trip_now"),
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["constraints"],
            domains=["flight"],
            destination="",
            keywords=["红眼", "航班"],
            top_k=5,
            reason="mixed_or_ambiguous -> flight tradeoff",
        ),
        config=DummyRerankerConfig(small_candidate_set_threshold=1, profile_top_n=2),
    )

    assert [candidate.item_id for candidate in path.selected_candidates] == [
        "slice_recent_red_eye"
    ]
    assert "conflict" in path.result.per_item_reason["constraint_avoid_red_eye"]


def test_choose_reranker_path_applies_source_budgets_and_dedupes_profile_candidates():
    candidates = [
        make_candidate(
            item_id="profile_kyoto_area",
            bucket="stable_preferences",
            matched_reason=["exact domain match on hotel", "keyword match on 住宿"],
            content_summary="hotel:preferred_area=京都四条",
            domains=["hotel"],
            applicability="适用于京都住宿选择。",
        ),
        make_candidate(
            item_id="profile_kyoto_area_dup",
            bucket="stable_preferences",
            matched_reason=["exact domain match on hotel", "keyword match on 住哪里"],
            content_summary="hotel:avoid_far_station=false",
            domains=["hotel"],
            applicability="适用于京都住宿选择。",
        ),
        make_candidate(
            source="episode_slice",
            item_id="slice_kyoto_machiya",
            bucket="stay_choice",
            score=0.6,
            matched_reason=["exact destination match on 京都", "keyword match on 住宿"],
            content_summary="上次京都住四条附近的町屋，步行和觅食都方便。",
            domains=["hotel"],
            applicability="仅供住宿选择参考。",
            polarity="",
        ),
        make_candidate(
            source="episode_slice",
            item_id="slice_kyoto_station_hotel",
            bucket="stay_choice",
            score=0.55,
            matched_reason=["exact destination match on 京都", "keyword match on 酒店"],
            content_summary="另一次京都住京都站旁边，交通方便但晚上体验一般。",
            domains=["hotel"],
            applicability="仅供住宿选择参考。",
            polarity="",
        ),
    ]

    path = choose_reranker_path(
        candidates=candidates,
        user_message="推荐这次京都住哪里比较适合带孩子，优先参考我过往偏好",
        plan=TravelPlanState(
            session_id="s1",
            trip_id="trip_now",
            destination="京都",
            travelers=Travelers(adults=2, children=1),
        ),
        retrieval_plan=RecallRetrievalPlan(
            source="hybrid_history",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="京都",
            keywords=["住宿", "带孩子"],
            top_k=5,
            reason="recommend -> Kyoto hotel preference and historical stay",
        ),
        config=DummyRerankerConfig(
            small_candidate_set_threshold=1,
            hybrid_top_n=3,
            hybrid_profile_top_n=1,
            hybrid_slice_top_n=2,
        ),
    )

    assert [candidate.item_id for candidate in path.selected_candidates] == [
        "profile_kyoto_area",
        "slice_kyoto_machiya",
        "slice_kyoto_station_hotel",
    ]
    assert "duplicate group" in path.result.per_item_reason["profile_kyoto_area_dup"]
