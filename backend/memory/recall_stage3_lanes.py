from __future__ import annotations

import json
from typing import Any

from config import Stage3RecallConfig
from memory.embedding_provider import EmbeddingProvider, cosine_similarity
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

_MIN_LEXICAL_SCORE = 0.05


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


class SemanticLane:
    lane_name = "semantic"

    def run(
        self,
        envelope: RecallQueryEnvelope,
        profile: UserMemoryProfile,
        slices: list[EpisodeSlice],
        config: Stage3RecallConfig,
        embedding_provider: EmbeddingProvider | None,
    ) -> Stage3LaneResult:
        if embedding_provider is None:
            return Stage3LaneResult(
                lane_name=self.lane_name,
                candidates=[],
                error="embedding_provider_missing",
            )

        records = _semantic_records(envelope, profile, slices)
        if not records:
            return Stage3LaneResult(lane_name=self.lane_name, candidates=[])

        query_text = " ".join(
            [
                envelope.user_message,
                *envelope.expanded_domains,
                *envelope.expanded_keywords,
            ]
        )
        record_texts = [text for _, _, text in records]
        try:
            vectors = embedding_provider.embed([query_text, *record_texts])
        except Exception as exc:
            return Stage3LaneResult(
                lane_name=self.lane_name,
                candidates=[],
                error=f"embedding_error:{type(exc).__name__}",
            )

        if len(vectors) != len(records) + 1:
            return Stage3LaneResult(
                lane_name=self.lane_name,
                candidates=[],
                error="embedding_count_mismatch",
            )

        query_vector = vectors[0]
        ranked: list[tuple[float, RecallCandidate, RetrievalEvidence]] = []
        for (candidate, evidence, _), vector in zip(records, vectors[1:]):
            score = cosine_similarity(query_vector, vector)
            if score < config.semantic.min_score:
                continue

            candidate.score = score
            evidence.lanes = [self.lane_name]
            evidence.semantic_score = score
            evidence.retrieval_reason = f"semantic cosine score={score:.3f}"
            ranked.append((score, candidate, evidence))

        ranked.sort(key=lambda entry: (-entry[0], entry[1].source, entry[1].item_id))
        return Stage3LaneResult(
            lane_name=self.lane_name,
            candidates=[
                Stage3Candidate(candidate=candidate, evidence=evidence)
                for _, candidate, evidence in ranked[: config.semantic.top_k]
            ],
        )


def _semantic_records(
    envelope: RecallQueryEnvelope,
    profile: UserMemoryProfile,
    slices: list[EpisodeSlice],
) -> list[tuple[RecallCandidate, RetrievalEvidence, str]]:
    records: list[tuple[RecallCandidate, RetrievalEvidence, str]] = []
    if envelope.source_policy.search_profile:
        for bucket in envelope.plan.buckets:
            for item in getattr(profile, bucket, []):
                candidate = build_profile_candidates(
                    [(bucket, item, "semantic embedding match")]
                )[0]
                records.append(
                    (
                        candidate,
                        _evidence_from_candidate(candidate, SemanticLane.lane_name),
                        _profile_item_text(bucket, item),
                    )
                )

    if envelope.source_policy.search_slices:
        for slice_ in slices:
            candidate = build_episode_slice_candidates(
                [(slice_, "semantic embedding match")]
            )[0]
            records.append(
                (
                    candidate,
                    _evidence_from_candidate(candidate, SemanticLane.lane_name),
                    _slice_text(slice_),
                )
            )

    return records


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
                matched_keywords=_matched_lexical_keywords(envelope, item_text),
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
            matched_keywords=_matched_lexical_keywords(envelope, slice_text),
            lexical_score=score,
            retrieval_reason="lexical keyword expansion",
        )
        ranked.append((score, candidate, evidence))
    return ranked


def _lexical_score(query_terms: set[str], text: str) -> float:
    meaningful_query_terms = {
        term for term in query_terms if _is_meaningful_lexical_term(term)
    }
    text_terms = {
        term
        for term in _tokenize_for_lexical(text)
        if _is_meaningful_lexical_term(term)
    }
    if not meaningful_query_terms or not text_terms:
        return 0.0

    overlap = meaningful_query_terms & text_terms
    if not overlap:
        return 0.0

    precision = len(overlap) / len(text_terms)
    recall = len(overlap) / len(meaningful_query_terms)
    if precision + recall == 0:
        return 0.0
    score = 2 * precision * recall / (precision + recall)
    if score < _MIN_LEXICAL_SCORE:
        return 0.0
    return score


def _tokenize_for_lexical(text: str) -> set[str]:
    terms: set[str] = set()
    ascii_buffer: list[str] = []
    cjk_buffer: list[str] = []

    def flush_ascii() -> None:
        if not ascii_buffer:
            return
        token = "".join(ascii_buffer).lower()
        ascii_buffer.clear()
        if len(token) < 2:
            return
        terms.add(token)
        if len(token) >= 4:
            terms.update(token[index : index + 2] for index in range(len(token) - 1))

    def flush_cjk() -> None:
        if not cjk_buffer:
            return
        token = "".join(cjk_buffer)
        cjk_buffer.clear()
        if len(token) >= 2:
            terms.add(token)
        terms.update(token)
        terms.update(token[index : index + 2] for index in range(len(token) - 1))

    for char in str(text):
        if char.isascii() and char.isalnum():
            flush_cjk()
            ascii_buffer.append(char)
        elif _is_cjk(char):
            flush_ascii()
            cjk_buffer.append(char)
        else:
            flush_ascii()
            flush_cjk()
    flush_ascii()
    flush_cjk()
    return terms


def _profile_item_text(bucket: str, item: MemoryProfileItem) -> str:
    del bucket

    parts = [
        _stringify_for_stage3(item.value),
        item.applicability,
        _stringify_for_stage3(item.context),
        _stringify_for_stage3(item.recall_hints),
        _stringify_for_stage3(item.source_refs),
    ]
    return " ".join(part for part in parts if part)


def _slice_text(slice_: EpisodeSlice) -> str:
    parts = [
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


def _matched_lexical_keywords(
    envelope: RecallQueryEnvelope,
    document_text: str,
) -> list[str]:
    document_terms = _tokenize_for_lexical(document_text)
    candidates: list[str] = []
    for value in (
        envelope.user_message,
        *envelope.original_keywords,
        *envelope.expanded_keywords,
    ):
        for term in _tokenize_for_lexical(value):
            if (
                term in document_terms
                and _is_meaningful_lexical_term(term)
                and term not in candidates
            ):
                candidates.append(term)
    return sorted(candidates, key=lambda term: (-len(term), term))


def _is_meaningful_lexical_term(term: str) -> bool:
    if not term:
        return False
    if term.isascii():
        return len(term) >= 2
    if any(_is_cjk(char) for char in term):
        return len(term) >= 2
    return len(term) >= 2


def _is_cjk(char: str) -> bool:
    return any(
        start <= ord(char) <= end
        for start, end in (
            (0x3400, 0x4DBF),
            (0x4E00, 0x9FFF),
            (0xF900, 0xFAFF),
        )
    )
