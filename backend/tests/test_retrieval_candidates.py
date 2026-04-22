from memory.retrieval_candidates import (
    RecallCandidate,
    build_episode_slice_candidates,
    build_profile_candidates,
)
from memory.v3_models import EpisodeSlice, MemoryProfileItem


def test_build_profile_candidates_exposes_ordinal_normalized_rank_within_current_list():
    item = MemoryProfileItem(
        id="constraints:flight:avoid_red_eye",
        domain="flight",
        key="avoid_red_eye",
        value=True,
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.95,
        status="active",
        context={},
        applicability="适用于所有旅行。",
        recall_hints={"domains": ["flight"], "keywords": ["红眼航班"]},
        source_refs=[],
        created_at="2026-04-19T00:00:00",
        updated_at="2026-04-19T00:00:00",
    )
    fallback_item = MemoryProfileItem(
        id="stable_preferences:flight:seat_type",
        domain="flight",
        key="seat_type",
        value="aisle",
        polarity="prefer",
        stability="stable",
        confidence=0.7,
        status="active",
        context={},
        applicability="长途航班优先。",
        recall_hints={"domains": ["flight"], "keywords": ["靠过道"]},
        source_refs=[],
        created_at="2026-04-18T00:00:00",
        updated_at="2026-04-18T00:00:00",
    )

    candidates = build_profile_candidates(
        [
            (
                "constraints",
                item,
                "exact domain match on flight; keyword match on 红眼航班; bucket=constraints",
            ),
            (
                "stable_preferences",
                fallback_item,
                "exact domain match on flight; bucket=stable_preferences",
            ),
        ]
    )

    assert len(candidates) == 2
    candidate = candidates[0]
    assert isinstance(candidate, RecallCandidate)
    assert candidate.source == "profile"
    assert candidate.item_id == item.id
    assert candidate.bucket == "constraints"
    assert candidate.domains == ["flight"]
    assert candidate.applicability == "适用于所有旅行。"
    assert candidate.content_summary == "flight:avoid_red_eye=true"
    assert candidate.score == 1.0
    assert candidate.matched_reason == [
        "exact domain match on flight",
        "keyword match on 红眼航班",
        "bucket=constraints",
    ]
    assert candidates[1].score == 0.5


def test_build_episode_slice_candidates_normalizes_slice_tuple_output():
    slice_ = EpisodeSlice(
        id="slice_ep_kyoto_01",
        user_id="u1",
        source_episode_id="ep_kyoto",
        source_trip_id="trip_1",
        slice_type="accommodation_decision",
        domains=["hotel", "accommodation"],
        entities={"destination": "京都"},
        keywords=["住宿", "酒店"],
        content="上次京都住四条附近的町屋。",
        applicability="仅供住宿选择参考。",
        created_at="2026-04-19T00:00:00",
    )

    candidates = build_episode_slice_candidates(
        [(slice_, "exact destination match on 京都; domain match on hotel")]
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, RecallCandidate)
    assert candidate.source == "episode_slice"
    assert candidate.item_id == slice_.id
    assert candidate.bucket == "accommodation_decision"
    assert candidate.domains == ["hotel", "accommodation"]
    assert candidate.applicability == "仅供住宿选择参考。"
    assert candidate.score == 1.0
    assert candidate.matched_reason == [
        "exact destination match on 京都",
        "domain match on hotel",
    ]
    assert candidate.content_summary == "上次京都住四条附近的町屋。"


def test_build_episode_slice_candidates_summarizes_multiline_and_long_content():
    slice_ = EpisodeSlice(
        id="slice_ep_kyoto_long",
        user_id="u1",
        source_episode_id="ep_kyoto",
        source_trip_id="trip_1",
        slice_type="accommodation_decision",
        domains=["hotel"],
        entities={"destination": "京都"},
        keywords=["住宿"],
        content=(
            "上次京都住四条附近的町屋。\n\n"
            "步行去锦市场很方便，晚上也比较安静。"
            "这段补充说明用于验证摘要不会把过长原文直接透传给后续 formatter 和 manager。"
        ),
        applicability="仅供住宿选择参考。",
        created_at="2026-04-19T00:00:00",
    )

    candidates = build_episode_slice_candidates(
        [(slice_, "exact destination match on 京都")]
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert "\n" not in candidate.content_summary
    assert candidate.content_summary == (
        "上次京都住四条附近的町屋。 步行去锦市场很方便，晚上也比较安静。这段补充说明用于验证摘要不会把过长原文直接透传给后续 formatter 和 manager..."
    )
