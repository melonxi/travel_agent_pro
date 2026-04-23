from __future__ import annotations

from config import Stage3FusionConfig
from memory.recall_stage3_models import (
    RetrievalEvidence,
    Stage3Candidate,
    Stage3LaneResult,
)


def fuse_lane_results(
    lane_results: list[Stage3LaneResult],
    config: Stage3FusionConfig,
) -> list[Stage3Candidate]:
    lane_weights = dict(config.lane_weights)
    fused_by_id: dict[str, Stage3Candidate] = {}

    for lane_result in lane_results:
        lane_name = lane_result.lane_name
        weight = lane_weights.get(lane_name, 0.0)
        if weight <= 0.0:
            continue

        seen_in_lane: set[str] = set()
        for index, candidate in enumerate(lane_result.candidates):
            rank = index + 1
            contribution = weight / float(config.rrf_k + rank)
            item_id = candidate.candidate.item_id
            if item_id in seen_in_lane:
                continue
            seen_in_lane.add(item_id)

            if item_id not in fused_by_id:
                fused_by_id[item_id] = Stage3Candidate(
                    candidate=candidate.candidate,
                    evidence=_copy_evidence(candidate.evidence),
                )
            else:
                _merge_evidence(fused_by_id[item_id].evidence, candidate.evidence)

            evidence = fused_by_id[item_id].evidence
            if lane_name not in evidence.lanes:
                evidence.lanes.append(lane_name)
            evidence.lane_ranks[lane_name] = rank
            evidence.lane_scores[lane_name] = contribution
            evidence.fused_score += contribution

    ranked = sorted(
        fused_by_id.values(),
        key=lambda candidate: (
            -candidate.evidence.fused_score,
            0 if "symbolic" in candidate.evidence.lanes else 1,
            candidate.candidate.source,
            candidate.candidate.item_id,
        ),
    )
    return _apply_caps(ranked, config)


def _copy_evidence(evidence: RetrievalEvidence) -> RetrievalEvidence:
    return RetrievalEvidence(
        item_id=evidence.item_id,
        source=evidence.source,
        lanes=list(evidence.lanes),
        lane_scores=dict(evidence.lane_scores),
        lane_ranks=dict(evidence.lane_ranks),
        fused_score=0.0,
        matched_domains=list(evidence.matched_domains),
        matched_keywords=list(evidence.matched_keywords),
        matched_entities=list(evidence.matched_entities),
        destination_match_type=evidence.destination_match_type,
        semantic_score=evidence.semantic_score,
        lexical_score=evidence.lexical_score,
        temporal_score=evidence.temporal_score,
        retrieval_reason=evidence.retrieval_reason,
    )


def _merge_evidence(target: RetrievalEvidence, source: RetrievalEvidence) -> None:
    _append_unique(target.matched_domains, source.matched_domains)
    _append_unique(target.matched_keywords, source.matched_keywords)
    _append_unique(target.matched_entities, source.matched_entities)

    target.semantic_score = _max_optional(target.semantic_score, source.semantic_score)
    target.lexical_score = _max_optional(target.lexical_score, source.lexical_score)
    target.temporal_score = _max_optional(target.temporal_score, source.temporal_score)

    if (
        source.destination_match_type
        and source.destination_match_type != "none"
        and target.destination_match_type in ("", "none")
    ):
        target.destination_match_type = source.destination_match_type

    if source.retrieval_reason:
        reasons = [part.strip() for part in target.retrieval_reason.split(";")]
        if source.retrieval_reason not in reasons:
            if target.retrieval_reason:
                target.retrieval_reason = (
                    f"{target.retrieval_reason}; {source.retrieval_reason}"
                )
            else:
                target.retrieval_reason = source.retrieval_reason


def _apply_caps(
    ranked: list[Stage3Candidate],
    config: Stage3FusionConfig,
) -> list[Stage3Candidate]:
    capped: list[Stage3Candidate] = []
    profile_count = 0
    slice_count = 0

    for candidate in ranked:
        if len(capped) >= config.max_candidates:
            break

        if candidate.candidate.source == "profile":
            if profile_count >= config.max_profile_candidates:
                continue
            profile_count += 1
        elif candidate.candidate.source == "episode_slice":
            if slice_count >= config.max_slice_candidates:
                continue
            slice_count += 1

        capped.append(candidate)

    return capped


def _append_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _max_optional(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)
