import pytest

from config import Stage3FusionConfig
from memory.recall_stage3_fusion import fuse_lane_results
from memory.recall_stage3_models import (
    RetrievalEvidence,
    Stage3Candidate,
    Stage3LaneResult,
)
from memory.retrieval_candidates import RecallCandidate


def _candidate(item_id: str, source: str = "profile") -> Stage3Candidate:
    return Stage3Candidate(
        candidate=RecallCandidate(
            source=source,
            item_id=item_id,
            bucket="stable_preferences",
            score=1.0,
            matched_reason=["test"],
            content_summary=f"{item_id} content",
            domains=["hotel"],
            applicability="test",
        ),
        evidence=RetrievalEvidence(
            item_id=item_id,
            source=source,
            lanes=[],
            retrieval_reason="test",
        ),
    )


def test_fuse_lane_results_unions_duplicate_candidates_and_tracks_lanes() -> None:
    lane_results = [
        Stage3LaneResult("symbolic", [_candidate("a"), _candidate("b")]),
        Stage3LaneResult("lexical", [_candidate("b"), _candidate("c")]),
    ]
    config = Stage3FusionConfig(max_candidates=10)

    fused = fuse_lane_results(lane_results, config)

    assert [candidate.candidate.item_id for candidate in fused] == ["b", "a", "c"]
    assert fused[0].evidence.lanes == ["symbolic", "lexical"]
    assert fused[0].evidence.lane_ranks == {"symbolic": 2, "lexical": 1}


def test_fuse_lane_results_applies_source_caps() -> None:
    lane_results = [
        Stage3LaneResult("symbolic", [_candidate("p1"), _candidate("p2")]),
        Stage3LaneResult("semantic", [_candidate("s1", source="episode_slice")]),
    ]
    config = Stage3FusionConfig(
        max_candidates=10,
        max_profile_candidates=1,
        max_slice_candidates=1,
    )

    fused = fuse_lane_results(lane_results, config)

    assert [candidate.candidate.item_id for candidate in fused] == ["p1", "s1"]


def test_fuse_lane_results_ignores_same_lane_duplicate_item_ids() -> None:
    lane_results = [
        Stage3LaneResult(
            "symbolic",
            [
                _candidate("a"),
                _candidate("a"),
                _candidate("b"),
            ],
        ),
    ]
    config = Stage3FusionConfig(max_candidates=10)

    fused = fuse_lane_results(lane_results, config)

    assert [candidate.candidate.item_id for candidate in fused] == ["a", "b"]
    assert fused[0].evidence.lanes == ["symbolic"]
    assert fused[0].evidence.lane_ranks == {"symbolic": 1}
    assert fused[0].evidence.lane_scores == {"symbolic": pytest.approx(1.0 / 61.0)}
    assert fused[0].evidence.fused_score == pytest.approx(1.0 / 61.0)


def test_fuse_lane_results_does_not_leak_input_fused_score() -> None:
    candidate = _candidate("a")
    candidate.evidence.fused_score = 99.0
    lane_results = [Stage3LaneResult("symbolic", [candidate])]
    config = Stage3FusionConfig(max_candidates=10)

    fused = fuse_lane_results(lane_results, config)

    assert fused[0].evidence.fused_score == pytest.approx(1.0 / 61.0)


def test_fuse_lane_results_ignores_zero_weight_lane() -> None:
    lane_results = [
        Stage3LaneResult("symbolic", [_candidate("a")]),
        Stage3LaneResult("lexical", [_candidate("b")]),
    ]
    config = Stage3FusionConfig(
        max_candidates=10,
        lane_weights=(("symbolic", 1.0), ("lexical", 0.0)),
    )

    fused = fuse_lane_results(lane_results, config)

    assert [candidate.candidate.item_id for candidate in fused] == ["a"]


def test_fuse_lane_results_applies_global_max_candidates_cap() -> None:
    lane_results = [
        Stage3LaneResult(
            "symbolic",
            [
                _candidate("a"),
                _candidate("b"),
                _candidate("c"),
            ],
        ),
    ]
    config = Stage3FusionConfig(max_candidates=2)

    fused = fuse_lane_results(lane_results, config)

    assert [candidate.candidate.item_id for candidate in fused] == ["a", "b"]


def test_fuse_lane_results_deep_copies_mutable_evidence_fields() -> None:
    candidate = _candidate("a")
    candidate.evidence.lanes.append("preexisting")
    candidate.evidence.lane_scores["preexisting"] = 0.5
    candidate.evidence.lane_ranks["preexisting"] = 9
    candidate.evidence.matched_domains.append("hotel")
    candidate.evidence.matched_keywords.append("pool")
    candidate.evidence.matched_entities.append("tokyo")
    lane_results = [Stage3LaneResult("symbolic", [candidate])]
    config = Stage3FusionConfig(max_candidates=10)

    fused = fuse_lane_results(lane_results, config)
    evidence = fused[0].evidence
    candidate.evidence.lanes.append("mutated")
    candidate.evidence.lane_scores["mutated"] = 1.0
    candidate.evidence.lane_ranks["mutated"] = 1
    candidate.evidence.matched_domains.append("mutated")
    candidate.evidence.matched_keywords.append("mutated")
    candidate.evidence.matched_entities.append("mutated")

    assert evidence.lanes == ["preexisting", "symbolic"]
    assert evidence.lane_scores == {
        "preexisting": 0.5,
        "symbolic": pytest.approx(1.0 / 61.0),
    }
    assert evidence.lane_ranks == {"preexisting": 9, "symbolic": 1}
    assert evidence.matched_domains == ["hotel"]
    assert evidence.matched_keywords == ["pool"]
    assert evidence.matched_entities == ["tokyo"]
