from __future__ import annotations

from config import Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3_models import (
    RecallQueryEnvelope,
    RetrievalEvidence,
    Stage3Candidate,
    Stage3LaneResult,
)
from memory.retrieval_candidates import RecallCandidate
from memory.symbolic_recall import rank_episode_slices, rank_profile_items
from memory.v3_models import EpisodeSlice, UserMemoryProfile


class SymbolicLane:
    lane_name = "symbolic"

    def run(
        self,
        envelope: RecallQueryEnvelope,
        profile: UserMemoryProfile,
        slices: list[EpisodeSlice],
        config: Stage3RecallConfig,
    ) -> Stage3LaneResult:
        lane_plan = _plan_for_source_policy(envelope)
        symbolic_limit = min(config.symbolic.top_k, envelope.plan.top_k)
        candidates = [
            *rank_profile_items(lane_plan, profile)[:symbolic_limit],
            *rank_episode_slices(lane_plan, slices)[:symbolic_limit],
        ]

        return Stage3LaneResult(
            lane_name=self.lane_name,
            candidates=[
                Stage3Candidate(
                    candidate=candidate,
                    evidence=_evidence_from_candidate(candidate, self.lane_name),
                )
                for candidate in candidates
            ],
        )


def _plan_for_source_policy(envelope: RecallQueryEnvelope) -> RecallRetrievalPlan:
    source = "hybrid_history"
    if envelope.source_policy.search_profile and not envelope.source_policy.search_slices:
        source = "profile"
    elif envelope.source_policy.search_slices and not envelope.source_policy.search_profile:
        source = "episode_slice"

    return RecallRetrievalPlan(
        source=source,
        buckets=list(envelope.plan.buckets),
        domains=list(envelope.plan.domains),
        destination=envelope.plan.destination,
        keywords=list(envelope.plan.keywords),
        top_k=envelope.plan.top_k,
        reason=envelope.plan.reason,
        fallback_used=envelope.plan.fallback_used,
    )


def _evidence_from_candidate(
    candidate: RecallCandidate,
    lane_name: str,
) -> RetrievalEvidence:
    return RetrievalEvidence(
        item_id=candidate.item_id,
        source=candidate.source,
        lanes=[lane_name],
        matched_domains=list(candidate.domains),
        matched_keywords=_matched_keywords_from_reason(candidate),
        destination_match_type=_destination_match_type(candidate),
        retrieval_reason="; ".join(candidate.matched_reason),
    )


def _matched_keywords_from_reason(candidate: RecallCandidate) -> list[str]:
    matched_keywords: list[str] = []
    for reason in candidate.matched_reason:
        if "keyword match on " not in reason:
            continue
        keyword = reason.rsplit("keyword match on ", 1)[-1].strip()
        if keyword and keyword not in matched_keywords:
            matched_keywords.append(keyword)
    return matched_keywords


def _destination_match_type(candidate: RecallCandidate) -> str:
    if any("exact destination match" in reason for reason in candidate.matched_reason):
        return "exact"
    return "none"
