from __future__ import annotations

import json
from typing import Any

from config import Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3_models import (
    RecallQueryEnvelope,
    RetrievalEvidence,
    Stage3Candidate,
    Stage3LaneResult,
)
from memory.retrieval_candidates import (
    RecallCandidate,
    build_episode_slice_candidates,
    build_profile_candidates,
)
from memory.symbolic_recall import rank_episode_slices, rank_profile_items
from memory.v3_models import EpisodeSlice, MemoryProfileItem, UserMemoryProfile


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
        candidates: list[RecallCandidate] = []
        if envelope.source_policy.search_profile:
            candidates.extend(rank_profile_items(lane_plan, profile)[:symbolic_limit])
        if envelope.source_policy.search_slices:
            candidates.extend(rank_episode_slices(lane_plan, slices)[:symbolic_limit])

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


class LexicalLane:
    lane_name = "lexical"

    def run(
        self,
        envelope: RecallQueryEnvelope,
        profile: UserMemoryProfile,
        slices: list[EpisodeSlice],
        config: Stage3RecallConfig,
    ) -> Stage3LaneResult:
        query_terms = _tokenize_for_lexical(
            " ".join(
                [
                    envelope.user_message,
                    *envelope.expanded_domains,
                    *envelope.expanded_keywords,
                ]
            )
        )
        if not query_terms:
            return Stage3LaneResult(lane_name=self.lane_name, candidates=[])

        ranked: list[tuple[float, RecallCandidate, RetrievalEvidence]] = []
        if envelope.source_policy.search_profile:
            ranked.extend(_rank_profile_lexical(envelope, profile, query_terms))
        if envelope.source_policy.search_slices:
            ranked.extend(_rank_slices_lexical(envelope, slices, query_terms))

        ranked.sort(key=lambda entry: (-entry[0], entry[1].source, entry[1].item_id))
        top_ranked = ranked[: config.lexical.top_k]

        return Stage3LaneResult(
            lane_name=self.lane_name,
            candidates=[
                Stage3Candidate(candidate=candidate, evidence=evidence)
                for _, candidate, evidence in top_ranked
            ],
        )


def _plan_for_source_policy(envelope: RecallQueryEnvelope) -> RecallRetrievalPlan:
    if envelope.source_policy.search_profile and not envelope.source_policy.search_slices:
        source = "profile"
    elif envelope.source_policy.search_slices and not envelope.source_policy.search_profile:
        source = "episode_slice"
    elif envelope.source_policy.search_profile and envelope.source_policy.search_slices:
        source = "hybrid_history"
    else:
        source = envelope.plan.source

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
        matched_domains=_matched_domains_from_reason(candidate),
        matched_keywords=_matched_keywords_from_reason(candidate),
        destination_match_type=_destination_match_type(candidate),
        retrieval_reason="; ".join(candidate.matched_reason),
    )


def _matched_domains_from_reason(candidate: RecallCandidate) -> list[str]:
    return [
        domain
        for domain in candidate.domains
        if any(domain in reason for reason in candidate.matched_reason)
    ]


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


def _rank_profile_lexical(
    envelope: RecallQueryEnvelope,
    profile: UserMemoryProfile,
    query_terms: set[str],
) -> list[tuple[float, RecallCandidate, RetrievalEvidence]]:
    ranked: list[tuple[float, RecallCandidate, RetrievalEvidence]] = []
    for bucket in envelope.plan.buckets:
        for item in getattr(profile, bucket, []):
            item_text = _profile_item_text(bucket, item)
            score = _lexical_score(query_terms, item_text)
            if score <= 0:
                continue

            candidate = build_profile_candidates(
                [(bucket, item, "lexical keyword expansion")]
            )[0]
            candidate.score = score
            evidence = RetrievalEvidence(
                item_id=candidate.item_id,
                source=candidate.source,
                lanes=[LexicalLane.lane_name],
                matched_keywords=sorted(
                    query_terms & _tokenize_for_lexical(item_text)
                ),
                lexical_score=score,
                retrieval_reason="lexical keyword expansion",
            )
            ranked.append((score, candidate, evidence))
    return ranked


def _rank_slices_lexical(
    envelope: RecallQueryEnvelope,
    slices: list[EpisodeSlice],
    query_terms: set[str],
) -> list[tuple[float, RecallCandidate, RetrievalEvidence]]:
    del envelope

    ranked: list[tuple[float, RecallCandidate, RetrievalEvidence]] = []
    for slice_ in slices:
        slice_text = _slice_text(slice_)
        score = _lexical_score(query_terms, slice_text)
        if score <= 0:
            continue

        candidate = build_episode_slice_candidates(
            [(slice_, "lexical keyword expansion")]
        )[0]
        candidate.score = score
        evidence = RetrievalEvidence(
            item_id=candidate.item_id,
            source=candidate.source,
            lanes=[LexicalLane.lane_name],
            matched_keywords=sorted(query_terms & _tokenize_for_lexical(slice_text)),
            lexical_score=score,
            retrieval_reason="lexical keyword expansion",
        )
        ranked.append((score, candidate, evidence))
    return ranked


def _lexical_score(query_terms: set[str], text: str) -> float:
    text_terms = _tokenize_for_lexical(text)
    if not query_terms or not text_terms:
        return 0.0

    overlap = query_terms & text_terms
    if not overlap:
        return 0.0

    precision = len(overlap) / len(text_terms)
    recall = len(overlap) / len(query_terms)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _tokenize_for_lexical(text: str) -> set[str]:
    normalized = "".join(
        char.lower() if char.isalnum() else " " for char in str(text)
    )
    terms: set[str] = set()
    for part in normalized.split():
        if not part:
            continue
        terms.add(part)
        terms.update(part)
        terms.update(part[index : index + 2] for index in range(len(part) - 1))
    return terms


def _profile_item_text(bucket: str, item: MemoryProfileItem) -> str:
    parts = [
        bucket,
        item.id,
        item.domain,
        item.key,
        _stringify_for_stage3(item.value),
        item.polarity,
        item.stability,
        item.status,
        item.applicability,
        _stringify_for_stage3(item.context),
        _stringify_for_stage3(item.recall_hints),
        _stringify_for_stage3(item.source_refs),
    ]
    return " ".join(part for part in parts if part)


def _slice_text(slice_: EpisodeSlice) -> str:
    parts = [
        slice_.id,
        slice_.slice_type,
        " ".join(slice_.domains),
        _stringify_for_stage3(slice_.entities),
        " ".join(slice_.keywords),
        slice_.content,
        slice_.applicability,
    ]
    return " ".join(part for part in parts if part)


def _stringify_for_stage3(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
