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
