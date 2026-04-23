from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import re
from typing import Any

from config import MemoryRerankerConfig
from memory.recall_query import RecallRetrievalPlan
from memory.retrieval_candidates import RecallCandidate
from state.models import TravelPlanState

_NEGATIVE_HINTS = ("不要", "别", "不想", "不住", "不坐", "不订", "避开", "别选", "别住")
_POSITIVE_HINTS = ("可以", "想", "要", "接受", "能", "安排", "优先", "就选")
_GENERIC_APPLICABILITY_HINTS = ("适用于所有", "适用于大多数", "大多数", "仅供", "参考")
_FAMILY_HINTS = ("亲子", "家庭", "带孩子", "儿童")
_TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")

_PROFILE_BUCKET_PRIOR = {
    "constraints": 1.0,
    "rejections": 0.92,
    "stable_preferences": 0.82,
    "preference_hypotheses": 0.66,
}


@dataclass
class RecallRerankResult:
    selected_item_ids: list[str]
    final_reason: str
    per_item_reason: dict[str, str]
    fallback_used: str = "none"


@dataclass
class RecallRerankPath:
    selected_candidates: list[RecallCandidate]
    result: RecallRerankResult


@dataclass(frozen=True)
class _IntentWeights:
    profile_source_prior: float
    slice_source_prior: float
    bucket_weight: float
    domain_weight: float
    keyword_weight: float
    destination_weight: float
    recency_weight: float
    applicability_weight: float
    conflict_weight: float


@dataclass(frozen=True)
class _ScoredCandidate:
    candidate: RecallCandidate
    source_score: float
    normalized_score: float
    final_score: float
    duplicate_group: str
    conflict_score: float
    reason: str


def choose_reranker_path(
    *,
    candidates: list[RecallCandidate],
    user_message: str,
    plan: TravelPlanState,
    retrieval_plan: RecallRetrievalPlan | None,
    config: MemoryRerankerConfig | None = None,
) -> RecallRerankPath:
    reranker_config = config or MemoryRerankerConfig()
    if len(candidates) <= reranker_config.small_candidate_set_threshold:
        return _small_candidate_set_result(candidates)

    weights = _intent_weights(user_message, retrieval_plan)
    grouped: dict[str, list[_ScoredCandidate]] = {"profile": [], "episode_slice": []}
    per_item_reason: dict[str, str] = {}
    duplicate_only_reasons: dict[str, str] = {}
    for candidate in candidates:
        scored = _score_candidate(candidate, user_message, plan, retrieval_plan, weights)
        if scored.conflict_score >= 0.95:
            duplicate_only_reasons[candidate.item_id] = scored.reason
            continue
        grouped[candidate.source].append(scored)
        per_item_reason[candidate.item_id] = scored.reason

    for source in grouped:
        grouped[source] = _normalize_source_scores(grouped[source], source_prior=_source_prior(source, weights))

    deduped_profile, duplicate_profile_reasons = _dedupe_group(grouped["profile"])
    deduped_slices, duplicate_slice_reasons = _dedupe_group(grouped["episode_slice"])
    per_item_reason.update(duplicate_profile_reasons)
    per_item_reason.update(duplicate_slice_reasons)
    per_item_reason.update(duplicate_only_reasons)

    selected = _select_candidates(
        deduped_profile,
        deduped_slices,
        retrieval_plan,
        reranker_config,
    )
    for scored in selected:
        per_item_reason[scored.candidate.item_id] = scored.reason

    selected_candidates = [scored.candidate for scored in selected]
    profile_count = sum(1 for candidate in selected_candidates if candidate.source == "profile")
    slice_count = len(selected_candidates) - profile_count
    final_reason = (
        "source-aware weighted rerank selected "
        f"{len(selected_candidates)} items ({profile_count} profile, {slice_count} slice)"
    )
    result = RecallRerankResult(
        selected_item_ids=[candidate.item_id for candidate in selected_candidates],
        final_reason=final_reason,
        per_item_reason=per_item_reason,
        fallback_used="none",
    )
    return RecallRerankPath(selected_candidates=selected_candidates, result=result)


def _small_candidate_set_result(candidates: list[RecallCandidate]) -> RecallRerankPath:
    per_item_reason = {
        candidate.item_id: _matched_reason_text(candidate)
        for candidate in candidates
    }
    result = RecallRerankResult(
        selected_item_ids=[candidate.item_id for candidate in candidates],
        final_reason="small candidate set; skipped weighted rerank",
        per_item_reason=per_item_reason,
        fallback_used="skipped_small_candidate_set",
    )
    return RecallRerankPath(selected_candidates=list(candidates), result=result)


def _intent_weights(
    user_message: str,
    retrieval_plan: RecallRetrievalPlan | None,
) -> _IntentWeights:
    text = user_message or ""
    reason = (retrieval_plan.reason if retrieval_plan is not None else "").lower()
    source = retrieval_plan.source if retrieval_plan is not None else ""
    if source == "profile" or "profile_" in reason:
        return _IntentWeights(1.0, 0.62, 0.34, 0.24, 0.18, 0.08, 0.06, 0.10, 1.4)
    if source == "episode_slice" or "past_trip" in reason:
        return _IntentWeights(0.62, 1.0, 0.16, 0.22, 0.18, 0.24, 0.14, 0.08, 1.0)
    if any(word in text for word in ("推荐", "比较好", "适合我", "怎么安排")):
        return _IntentWeights(0.9, 0.9, 0.22, 0.22, 0.20, 0.18, 0.10, 0.14, 1.2)
    return _IntentWeights(0.84, 0.84, 0.24, 0.22, 0.18, 0.14, 0.08, 0.12, 1.2)


def _score_candidate(
    candidate: RecallCandidate,
    user_message: str,
    plan: TravelPlanState,
    retrieval_plan: RecallRetrievalPlan | None,
    weights: _IntentWeights,
) -> _ScoredCandidate:
    bucket_score = _bucket_prior(candidate)
    domain_score = _jaccard(
        set(retrieval_plan.domains if retrieval_plan is not None else []),
        set(candidate.domains),
    )
    keyword_score = _keyword_overlap(candidate, retrieval_plan)
    destination_score = _destination_match(candidate, plan, retrieval_plan)
    recency_score = _recency_score(candidate, 180)
    applicability_score = _applicability_score(candidate, plan)
    conflict_score = _conflict_score(candidate, user_message)
    duplicate_group = _duplicate_group(candidate)
    raw_score = (
        weights.bucket_weight * bucket_score
        + weights.domain_weight * domain_score
        + weights.keyword_weight * keyword_score
        + weights.destination_weight * destination_score
        + weights.recency_weight * recency_score
        + weights.applicability_weight * applicability_score
        - weights.conflict_weight * conflict_score
    )
    reason = (
        f"{_matched_reason_text(candidate)} | bucket={bucket_score:.2f} "
        f"domain={domain_score:.2f} keyword={keyword_score:.2f} "
        f"destination={destination_score:.2f} recency={recency_score:.2f} "
        f"applicability={applicability_score:.2f} conflict={conflict_score:.2f}"
    )
    return _ScoredCandidate(
        candidate=candidate,
        source_score=raw_score,
        normalized_score=raw_score,
        final_score=raw_score,
        duplicate_group=duplicate_group,
        conflict_score=conflict_score,
        reason=reason,
    )


def _normalize_source_scores(
    scored_candidates: list[_ScoredCandidate],
    *,
    source_prior: float,
) -> list[_ScoredCandidate]:
    if not scored_candidates:
        return []
    values = [candidate.source_score for candidate in scored_candidates]
    max_score = max(values)
    min_score = min(values)
    normalized: list[_ScoredCandidate] = []
    for scored in scored_candidates:
        if math.isclose(max_score, min_score):
            norm = 1.0
        else:
            norm = (scored.source_score - min_score) / (max_score - min_score)
        normalized.append(
            _ScoredCandidate(
                candidate=scored.candidate,
                source_score=scored.source_score,
                normalized_score=norm,
                final_score=source_prior + norm,
                duplicate_group=scored.duplicate_group,
                conflict_score=scored.conflict_score,
                reason=scored.reason,
            )
        )
    normalized.sort(key=lambda item: (-item.final_score, item.candidate.item_id))
    return normalized


def _select_candidates(
    profile_candidates: list[_ScoredCandidate],
    slice_candidates: list[_ScoredCandidate],
    retrieval_plan: RecallRetrievalPlan | None,
    config: MemoryRerankerConfig,
) -> list[_ScoredCandidate]:
    source = retrieval_plan.source if retrieval_plan is not None else "hybrid_history"
    if source == "profile":
        selected = list(profile_candidates[: config.profile_top_n])
        if len(selected) < config.profile_top_n:
            remaining = config.profile_top_n - len(selected)
            selected.extend(slice_candidates[:remaining])
        selected.sort(key=lambda item: (-item.final_score, item.candidate.item_id))
        return selected[: config.profile_top_n]
    if source == "episode_slice":
        selected = list(slice_candidates[: config.slice_top_n])
        if len(selected) < config.slice_top_n:
            remaining = config.slice_top_n - len(selected)
            selected.extend(profile_candidates[:remaining])
        selected.sort(key=lambda item: (-item.final_score, item.candidate.item_id))
        return selected[: config.slice_top_n]

    selected: list[_ScoredCandidate] = []
    selected.extend(profile_candidates[: config.hybrid_profile_top_n])
    selected.extend(slice_candidates[: config.hybrid_slice_top_n])
    selected.sort(key=lambda item: (-item.final_score, item.candidate.item_id))
    return selected[: config.hybrid_top_n]


def _dedupe_group(
    scored_candidates: list[_ScoredCandidate],
) -> tuple[list[_ScoredCandidate], dict[str, str]]:
    seen: dict[str, _ScoredCandidate] = {}
    deduped: list[_ScoredCandidate] = []
    reasons: dict[str, str] = {}
    for scored in scored_candidates:
        existing = seen.get(scored.duplicate_group)
        if existing is None:
            seen[scored.duplicate_group] = scored
            deduped.append(scored)
            continue
        reasons[scored.candidate.item_id] = (
            f"{scored.reason} | duplicate group={scored.duplicate_group}"
        )
    return deduped, reasons


def _duplicate_group(candidate: RecallCandidate) -> str:
    if candidate.source == "episode_slice":
        return f"{candidate.source}:{candidate.item_id}"
    primary_domain = candidate.domains[0] if candidate.domains else candidate.bucket
    polarity = candidate.polarity or "neutral"
    return f"{candidate.source}:{primary_domain}:{polarity}"


def _bucket_prior(candidate: RecallCandidate) -> float:
    if candidate.source == "profile":
        return _PROFILE_BUCKET_PRIOR.get(candidate.bucket, 0.5)
    if candidate.bucket in {"rejected_option", "pitfall"}:
        return 0.88
    if candidate.bucket in {"stay_choice", "transport_choice", "itinerary_pattern"}:
        return 0.76
    return 0.62


def _keyword_overlap(
    candidate: RecallCandidate,
    retrieval_plan: RecallRetrievalPlan | None,
) -> float:
    query_tokens = set(retrieval_plan.keywords if retrieval_plan is not None else [])
    candidate_tokens = set(_candidate_terms(candidate))
    return _jaccard(query_tokens, candidate_tokens)


def _destination_match(
    candidate: RecallCandidate,
    plan: TravelPlanState,
    retrieval_plan: RecallRetrievalPlan | None,
) -> float:
    destinations = [
        value
        for value in (
            retrieval_plan.destination if retrieval_plan is not None else "",
            plan.destination or "",
        )
        if value
    ]
    haystack = " ".join(
        part for part in (candidate.content_summary, candidate.applicability) if part
    )
    for destination in destinations:
        if destination and destination in haystack:
            return 1.0
    return 0.0


def _recency_score(candidate: RecallCandidate, half_life_days: int) -> float:
    if not candidate.created_at:
        return max(candidate.score, 0.0)
    try:
        created_at = datetime.fromisoformat(candidate.created_at)
    except ValueError:
        return max(candidate.score, 0.0)
    age_days = max((datetime.now() - created_at).days, 0)
    if half_life_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_days / float(half_life_days))


def _applicability_score(candidate: RecallCandidate, plan: TravelPlanState) -> float:
    text = " ".join(part for part in (candidate.applicability, candidate.content_summary) if part)
    score = 0.0
    if any(hint in text for hint in _GENERIC_APPLICABILITY_HINTS):
        score += 0.35
    if plan.destination and plan.destination in text:
        score += 0.65
    if plan.travelers and plan.travelers.children > 0 and any(hint in text for hint in _FAMILY_HINTS):
        score += 0.45
    return min(score, 1.0)


def _conflict_score(candidate: RecallCandidate, user_message: str) -> float:
    if candidate.source != "profile":
        return 0.0
    text = user_message or ""
    if not _overlaps_with_message(candidate, text):
        return 0.0
    polarity = (candidate.polarity or "").lower()
    has_negative = any(token in text for token in _NEGATIVE_HINTS)
    has_positive = any(token in text for token in _POSITIVE_HINTS)
    if polarity in {"avoid", "reject", "dislike"} and has_positive:
        return 1.0
    if polarity in {"prefer", "like", "must"} and has_negative:
        return 1.0
    return 0.0


def _overlaps_with_message(candidate: RecallCandidate, user_message: str) -> bool:
    message_terms = set(_tokenize(user_message))
    candidate_terms = set(_candidate_terms(candidate))
    if not message_terms or not candidate_terms:
        return False
    if message_terms & candidate_terms:
        return True
    return any(domain in user_message for domain in candidate.domains)


def _candidate_terms(candidate: RecallCandidate) -> list[str]:
    terms: list[str] = []
    for part in (
        candidate.content_summary,
        candidate.applicability,
        " ".join(candidate.matched_reason),
        " ".join(candidate.domains),
    ):
        terms.extend(_tokenize(part))
    return terms


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    raw_tokens = _TOKEN_SPLIT_RE.split(text)
    tokens = [token.strip().lower() for token in raw_tokens if token and len(token.strip()) > 1]
    if any(phrase in text for phrase in ("红眼", "靠窗", "带孩子", "京都")):
        if "红眼" in text:
            tokens.append("红眼")
        if "靠窗" in text:
            tokens.append("靠窗")
        if "带孩子" in text:
            tokens.append("带孩子")
        if "京都" in text:
            tokens.append("京都")
    return tokens


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return float(len(left & right)) / float(len(union))


def _matched_reason_text(candidate: RecallCandidate) -> str:
    return " | ".join(candidate.matched_reason) if candidate.matched_reason else "matched candidate"


def _source_prior(source: str, weights: _IntentWeights) -> float:
    if source == "profile":
        return weights.profile_source_prior
    return weights.slice_source_prior
