from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import re
from typing import Any

from config import EpisodeRerankConfig, ProfileRerankConfig
from memory.retrieval_candidates import RecallCandidate

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
    per_item_scores: dict[str, Any] = field(default_factory=dict)
    intent_label: str = ""
    selection_metrics: dict[str, float | None] = field(default_factory=dict)


def selection_metrics_placeholder() -> dict[str, float | None]:
    return {
        "selected_pairwise_similarity_max": None,
        "selected_pairwise_similarity_avg": None,
    }


def empty_rerank_result() -> RecallRerankResult:
    return RecallRerankResult(
        selected_item_ids=[],
        final_reason="",
        per_item_reason={},
        fallback_used="none",
        per_item_scores={},
        intent_label="",
        selection_metrics=selection_metrics_placeholder(),
    )


def rerank_profile_candidates(
    *,
    candidates: list[RecallCandidate],
    user_message: str,
    destination: str,
    domains: list[str],
    keywords: list[str],
    config: ProfileRerankConfig,
) -> RecallRerankResult:
    scored: list[tuple[float, str, RecallCandidate]] = []
    per_item_reason: dict[str, str] = {}
    per_item_scores: dict[str, dict[str, float | str | None]] = {}

    for candidate in candidates:
        if candidate.source != "profile":
            continue
        score_detail = _profile_score(candidate, destination, domains, keywords, config)
        per_item_scores[candidate.item_id] = score_detail
        if _profile_conflicts(candidate, user_message):
            per_item_reason[candidate.item_id] = "filtered: conflict"
            continue
        scored.append(
            (
                float(score_detail["final_score"] or 0.0),
                _profile_duplicate_group(candidate),
                candidate,
            )
        )
        per_item_reason[candidate.item_id] = _score_reason(score_detail)

    selected = _dedupe_source_scored(scored)[: config.profile_top_n]
    return RecallRerankResult(
        selected_item_ids=[candidate.item_id for _, _, candidate in selected],
        final_reason=f"profile rerank selected {len(selected)} items",
        per_item_reason=per_item_reason,
        fallback_used="none",
        per_item_scores=per_item_scores,
        intent_label="profile",
        selection_metrics=selection_metrics_placeholder(),
    )


def rerank_episode_candidates(
    *,
    candidates: list[RecallCandidate],
    user_message: str,
    destination: str,
    domains: list[str],
    keywords: list[str],
    config: EpisodeRerankConfig,
) -> RecallRerankResult:
    scored: list[tuple[float, str, RecallCandidate]] = []
    per_item_reason: dict[str, str] = {}
    per_item_scores: dict[str, dict[str, float | str | None]] = {}

    for candidate in candidates:
        if candidate.source != "episode_slice":
            continue
        score_detail = _episode_score(candidate, destination, domains, keywords, config)
        per_item_scores[candidate.item_id] = score_detail
        if _episode_conflicts(candidate, user_message):
            per_item_reason[candidate.item_id] = "filtered: conflict"
            continue
        scored.append(
            (
                float(score_detail["final_score"] or 0.0),
                _episode_duplicate_group(candidate),
                candidate,
            )
        )
        per_item_reason[candidate.item_id] = _score_reason(score_detail)

    selected = _dedupe_source_scored(scored)[: config.episode_top_n]
    return RecallRerankResult(
        selected_item_ids=[candidate.item_id for _, _, candidate in selected],
        final_reason=f"episode rerank selected {len(selected)} items",
        per_item_reason=per_item_reason,
        fallback_used="none",
        per_item_scores=per_item_scores,
        intent_label="episode_slice",
        selection_metrics=selection_metrics_placeholder(),
    )


def _profile_score(
    candidate: RecallCandidate,
    destination: str,
    domains: list[str],
    keywords: list[str],
    config: ProfileRerankConfig,
) -> dict[str, float | str | None]:
    bucket_score = _PROFILE_BUCKET_PRIOR.get(candidate.bucket, 0.5)
    retrieval_score = max(0.0, min(candidate.retrieval_score or candidate.score, 1.0))
    match_score = max(
        _jaccard(set(domains), set(candidate.domains)),
        1.0
        if destination
        and destination in f"{candidate.content_summary} {candidate.applicability}"
        else 0.0,
        _jaccard(set(keywords), set(_candidate_terms(candidate))),
    )
    recency_score = _recency_score(candidate, config.recency_half_life_days)
    final_score = (
        config.w_bucket * bucket_score
        + config.w_conf * retrieval_score
        + config.w_match * match_score
        + config.w_rec * recency_score
    )
    return {
        "bucket_score": bucket_score,
        "retrieval_score": retrieval_score,
        "match_score": match_score,
        "recency_score": recency_score,
        "final_score": final_score,
        "source": "profile",
    }


def _episode_score(
    candidate: RecallCandidate,
    destination: str,
    domains: list[str],
    keywords: list[str],
    config: EpisodeRerankConfig,
) -> dict[str, float | str | None]:
    retrieval_score = max(0.0, min(candidate.retrieval_score or candidate.score, 1.0))
    match_score = max(
        _jaccard(set(domains), set(candidate.domains)),
        1.0
        if destination
        and destination in f"{candidate.content_summary} {candidate.applicability}"
        else 0.0,
        _jaccard(set(keywords), set(_candidate_terms(candidate))),
    )
    type_score = _episode_type_score(candidate)
    recency_score = _recency_score(candidate, config.recency_half_life_days)
    final_score = (
        config.w_rel * retrieval_score
        + config.w_match * match_score
        + config.w_type * type_score
        + config.w_rec * recency_score
    )
    return {
        "retrieval_score": retrieval_score,
        "match_score": match_score,
        "type_score": type_score,
        "recency_score": recency_score,
        "final_score": final_score,
        "source": "episode_slice",
    }


def _episode_type_score(candidate: RecallCandidate) -> float:
    if candidate.bucket in {"stay_choice", "transport_choice", "itinerary_pattern"}:
        return 1.0
    if candidate.bucket in {"rejected_option", "pitfall"}:
        return 0.88
    return 0.62


def _profile_conflicts(candidate: RecallCandidate, user_message: str) -> bool:
    return _conflict_score(candidate, user_message) >= 0.95


def _episode_conflicts(candidate: RecallCandidate, user_message: str) -> bool:
    return _conflict_score(candidate, user_message) >= 0.95


def _profile_duplicate_group(candidate: RecallCandidate) -> str:
    return _duplicate_group(candidate)


def _episode_duplicate_group(candidate: RecallCandidate) -> str:
    return _duplicate_group(candidate)


def _score_reason(score_detail: dict[str, float | str | None]) -> str:
    return (
        f"retrieval={float(score_detail.get('retrieval_score') or 0.0):.2f} "
        f"match={float(score_detail.get('match_score') or 0.0):.2f} "
        f"recency={float(score_detail.get('recency_score') or 0.0):.2f} "
        f"final={float(score_detail.get('final_score') or 0.0):.2f}"
    )


def _dedupe_source_scored(
    scored_candidates: list[tuple[float, str, RecallCandidate]],
) -> list[tuple[float, str, RecallCandidate]]:
    deduped: list[tuple[float, str, RecallCandidate]] = []
    seen_groups: set[str] = set()
    for score, group, candidate in sorted(
        scored_candidates,
        key=lambda item: (-item[0], item[2].item_id),
    ):
        if group in seen_groups:
            continue
        seen_groups.add(group)
        deduped.append((score, group, candidate))
    return deduped


def _duplicate_group(candidate: RecallCandidate) -> str:
    if candidate.source == "episode_slice":
        summary = _duplicate_text_key(candidate.content_summary)
        if summary:
            primary_domain = candidate.domains[0] if candidate.domains else candidate.bucket
            return f"{candidate.source}:{candidate.bucket}:{primary_domain}:{summary}"
        return f"{candidate.source}:{candidate.item_id}"
    primary_domain = candidate.domains[0] if candidate.domains else candidate.bucket
    key = candidate.key or "no_key"
    polarity = candidate.polarity or "neutral"
    return f"{candidate.source}:{primary_domain}:{key}:{polarity}"


def _duplicate_text_key(text: str) -> str:
    tokens = _tokenize(text)
    if not tokens:
        return ""
    return "|".join(tokens[:12])


def _recency_score(candidate: RecallCandidate, half_life_days: int) -> float:
    if not candidate.created_at:
        return max(candidate.score, 0.0)
    raw = candidate.created_at
    # Accept trailing `Z` as UTC shorthand.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        created_at = datetime.fromisoformat(raw)
    except ValueError:
        return max(candidate.score, 0.0)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age_days = max((now - created_at).days, 0)
    if half_life_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_days / float(half_life_days))


def _conflict_score(candidate: RecallCandidate, user_message: str) -> float:
    text = user_message or ""
    if not _overlaps_with_message(candidate, text):
        return 0.0
    polarity = (candidate.polarity or "").lower()
    # Episode slices rarely carry an explicit polarity, but their bucket
    # taxonomy is already oriented: rejected_option / pitfall imply "avoid".
    if candidate.source == "episode_slice":
        if candidate.bucket in {"rejected_option", "pitfall"} and _has_candidate_specific_positive(
            candidate,
            text,
        ):
            return 1.0
        return 0.0

    if polarity in {"avoid", "reject", "dislike"} and _has_candidate_specific_positive(
        candidate,
        text,
    ):
        return 1.0
    if polarity in {"prefer", "like", "must"} and _has_candidate_specific_negative(
        candidate,
        text,
    ):
        return 1.0
    return 0.0


def _has_candidate_specific_negative(candidate: RecallCandidate, text: str) -> bool:
    candidate_terms = {
        term
        for term in _candidate_terms(candidate)
        if len(term) >= 2 and term in text
    }
    if not candidate_terms:
        return False
    negative_prefixes = ("不要", "别", "不想", "不住", "不坐", "不订", "避开", "别选", "别住", "不要再", "别再")
    negative_suffixes = ("就算了", "算了")
    for term in candidate_terms:
        index = text.find(term)
        while index >= 0:
            before = text[max(0, index - 8):index]
            after = text[index:index + len(term) + 8]
            if any(token in before for token in negative_prefixes):
                return True
            if any(token in after for token in negative_suffixes):
                return True
            index = text.find(term, index + len(term))
    return False


def _has_candidate_specific_positive(candidate: RecallCandidate, text: str) -> bool:
    candidate_terms = {
        term
        for term in _candidate_terms(candidate)
        if len(term) >= 2 and term in text
    }
    if not candidate_terms:
        return False
    positive_prefixes = (
        "可以",
        "想",
        "接受",
        "能",
        "安排",
        "优先",
        "就选",
        "试试",
        "换个",
        "宁可",
    )
    for term in candidate_terms:
        index = text.find(term)
        while index >= 0:
            before = text[max(0, index - 8):index]
            if any(token in before for token in positive_prefixes):
                return True
            if "要" in before and "不要" not in before:
                return True
            index = text.find(term, index + len(term))
    return False


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
    if any(
        phrase in text
        for phrase in (
            "红眼",
            "靠窗",
            "带孩子",
            "京都",
            "慢悠悠",
            "走路少",
            "少走路",
            "太累",
            "别太累",
            "爸妈",
            "安静",
            "热闹",
            "避世",
            "青旅",
        )
    ):
        if "红眼" in text:
            tokens.append("红眼")
        if "靠窗" in text:
            tokens.append("靠窗")
        if "带孩子" in text:
            tokens.append("带孩子")
        if "京都" in text:
            tokens.append("京都")
        if "慢悠悠" in text:
            tokens.append("慢悠悠")
        if "走路少" in text:
            tokens.append("走路少")
        if "少走路" in text:
            tokens.append("少走路")
        if "太累" in text:
            tokens.append("太累")
        if "别太累" in text:
            tokens.append("别太累")
        if "爸妈" in text:
            tokens.append("爸妈")
        if "安静" in text:
            tokens.append("安静")
        if "热闹" in text:
            tokens.append("热闹")
        if "避世" in text:
            tokens.append("避世")
        if "青旅" in text:
            tokens.append("青旅")
    return tokens


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return float(len(left & right)) / float(len(union))
