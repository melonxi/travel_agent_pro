from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any

from memory.v3_models import EpisodeSlice, MemoryProfileItem, UserMemoryProfile


_CURRENT_TRIP_PHRASES = ("这次", "本次", "当前")
_CURRENT_TRIP_STATE_WORDS = ("预算", "几号", "出发", "骨架", "约束")
_HISTORY_PHRASES = (
    "我是不是说过",
    "按我的习惯",
    "还记得吗",
    "有没有记录",
    "上次",
    "之前",
    "以前",
)
_PROFILE_HINT_WORDS = (
    "说过",
    "习惯",
    "偏好",
    "喜欢",
    "不坐",
    "不吃",
    "不住",
    "不要",
    "避开",
    "拒绝",
)
_SLICES_HINT_WORDS = ("上次", "之前", "以前", "住哪里", "住哪", "住哪家", "哪里住")
_PROFILE_RECALL_DOMAINS = {
    "flight",
    "train",
    "pace",
    "food",
    "hotel",
    "accommodation",
}

_DOMAIN_RULES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    (
        "hotel",
        ("住哪里", "住哪", "住哪家", "哪里住", "住宿", "酒店", "民宿", "住酒店", "住民宿", "青旅", "住青旅"),
        ("住哪里", "住哪", "住哪家", "哪里住", "住宿", "酒店", "民宿", "住酒店", "住民宿", "青旅", "住青旅"),
    ),
    (
        "accommodation",
        ("住哪里", "住哪", "住哪家", "哪里住", "住宿", "酒店", "民宿", "住酒店", "住民宿", "青旅", "住青旅"),
        ("住哪里", "住哪", "住哪家", "哪里住", "住宿", "酒店", "民宿", "住酒店", "住民宿", "青旅", "住青旅"),
    ),
    ("flight", ("航班", "红眼", "飞机"), ("航班", "红眼", "飞机")),
    ("train", ("火车", "高铁"), ("火车", "高铁")),
    ("pace", ("节奏", "累", "慢", "松"), ("节奏", "累", "慢", "松")),
    ("food", ("吃", "辣", "餐厅"), ("吃", "辣", "餐厅")),
]

_KNOWN_DESTINATIONS = (
    "京都",
    "大阪",
    "东京",
    "東京",
    "奈良",
    "名古屋",
    "北海道",
    "冲绳",
    "沖縄",
    "福冈",
    "福岡",
    "札幌",
    "巴黎",
    "伦敦",
    "首尔",
    "台北",
    "香港",
)

_BUCKET_ORDER = {
    "constraints": 0,
    "rejections": 1,
    "stable_preferences": 2,
    "preference_hypotheses": 3,
}
_CONSERVATIVE_PROFILE_BUCKETS = ("constraints", "rejections", "stable_preferences")
_KNOWN_PROFILE_BUCKETS = _CONSERVATIVE_PROFILE_BUCKETS + ("preference_hypotheses",)


@dataclass
class RecallQuery:
    needs_memory: bool
    domains: list[str]
    entities: dict[str, str]
    keywords: list[str]
    include_profile: bool
    include_slices: bool
    include_working_memory: bool
    matched_reason: str
    allowed_buckets: list[str] = field(default_factory=list)
    strictness: str = "soft"


def should_trigger_memory_recall(message: str) -> bool:
    text = _normalize_text(message)
    if not text:
        return False

    has_history_cue = any(phrase in text for phrase in _HISTORY_PHRASES)
    if has_history_cue:
        return True

    domains = _extract_domains(text)
    if _is_direct_profile_recall_query(text, domains):
        return True

    if any(phrase in text for phrase in _CURRENT_TRIP_PHRASES):
        if any(word in text for word in _CURRENT_TRIP_STATE_WORDS):
            return False
        return False

    return False


def build_recall_query(message: str) -> RecallQuery:
    text = _normalize_text(message)
    trigger = should_trigger_memory_recall(text)
    domains = _extract_domains(text)
    entities: dict[str, str] = {}
    destination = _extract_destination(text)
    if destination:
        entities["destination"] = destination

    keywords = _extract_keywords(text)
    include_profile = trigger and _should_include_profile(text, domains)
    include_slices = trigger and _should_include_slices(text, domains, destination)
    include_working_memory = False
    matched_reason = _build_matched_reason(text, trigger, domains, destination, include_profile, include_slices)

    return RecallQuery(
        needs_memory=trigger,
        domains=domains,
        entities=entities,
        keywords=keywords,
        include_profile=include_profile,
        include_slices=include_slices,
        include_working_memory=include_working_memory,
        matched_reason=matched_reason,
    )


def rank_profile_items(
    query: RecallQuery, profile: UserMemoryProfile
) -> list[tuple[str, MemoryProfileItem, str]]:
    if not query.needs_memory or not query.include_profile:
        return []

    allowed_buckets = _normalized_profile_buckets(query.allowed_buckets)
    ranked: list[tuple[tuple[Any, ...], str, MemoryProfileItem, str]] = []
    for bucket_name in ("constraints", "rejections", "stable_preferences", "preference_hypotheses"):
        if bucket_name not in allowed_buckets:
            continue
        items = getattr(profile, bucket_name, [])
        for item in items:
            score, reason = _score_profile_item(query, bucket_name, item)
            if score is None:
                continue
            ranked.append((score, bucket_name, item, reason))

    ranked.sort(key=lambda entry: entry[0])
    return [(bucket, item, reason) for _, bucket, item, reason in ranked]


def _normalized_profile_buckets(allowed_buckets: list[str]) -> set[str]:
    normalized = {bucket for bucket in allowed_buckets if bucket in _KNOWN_PROFILE_BUCKETS}
    if normalized:
        return normalized
    return set(_CONSERVATIVE_PROFILE_BUCKETS)


def rank_episode_slices(
    query: RecallQuery, slices: list[EpisodeSlice]
) -> list[tuple[EpisodeSlice, str]]:
    if not query.needs_memory or not query.include_slices:
        return []

    ranked: list[tuple[tuple[Any, ...], EpisodeSlice, str]] = []
    for slice_ in slices:
        score, reason = _score_episode_slice(query, slice_)
        if score is None:
            continue
        ranked.append((score, slice_, reason))

    ranked.sort(key=lambda entry: entry[0])
    return [(slice_, reason) for _, slice_, reason in ranked]


def _score_profile_item(
    query: RecallQuery, bucket: str, item: MemoryProfileItem
) -> tuple[tuple[Any, ...] | None, str]:
    item_domains = _profile_item_domains(item)
    matched_domains = [domain for domain in query.domains if domain in item_domains]
    matched_keywords = _matched_keywords(
        query.keywords,
        _profile_item_search_terms(item),
    )
    if not matched_domains and not matched_keywords:
        return None, ""

    bucket_rank = _BUCKET_ORDER.get(bucket, 99)
    exact_domain = 0 if matched_domains else 1
    keyword_rank = 0 if matched_keywords else 1
    recency = -_parse_timestamp(item.updated_at or item.created_at)
    score = (
        bucket_rank,
        exact_domain,
        keyword_rank,
        -len(matched_domains),
        -len(matched_keywords),
        recency,
        item.id,
    )
    reason_parts = []
    if matched_domains:
        reason_parts.append(f"exact domain match on {matched_domains[0]}")
    if matched_keywords:
        reason_parts.append(f"keyword match on {matched_keywords[0]}")
    reason_parts.append(f"bucket={bucket}")
    return score, "; ".join(reason_parts)


def _score_episode_slice(
    query: RecallQuery, slice_: EpisodeSlice
) -> tuple[tuple[Any, ...] | None, str]:
    matched_destination = _match_destination(query, slice_)
    matched_domains = [domain for domain in query.domains if domain in slice_.domains]
    matched_keywords = _matched_keywords(query.keywords, _slice_search_terms(slice_))
    if not matched_destination and not matched_domains and not matched_keywords:
        return None, ""

    score = (
        0 if matched_destination else 1,
        0 if matched_domains else 1,
        0 if matched_keywords else 1,
        -len(matched_domains),
        -len(matched_keywords),
        -_parse_timestamp(slice_.created_at),
        slice_.id,
    )
    reason_parts = []
    if matched_destination:
        reason_parts.append(f"exact destination match on {matched_destination}")
    if matched_domains:
        reason_parts.append(f"domain match on {matched_domains[0]}")
    if matched_keywords:
        reason_parts.append(f"keyword match on {matched_keywords[0]}")
    return score, "; ".join(reason_parts)


def _should_include_profile(text: str, domains: list[str]) -> bool:
    if any(word in text for word in _PROFILE_HINT_WORDS):
        return True
    return any(domain in _PROFILE_RECALL_DOMAINS for domain in domains)


def _should_include_slices(text: str, domains: list[str], destination: str | None) -> bool:
    if any(word in text for word in _SLICES_HINT_WORDS):
        if "上次" in text:
            return True
        return bool(destination) or any(
            domain in {"hotel", "accommodation", "train"} for domain in domains
        )
    return bool(destination) or any(domain in {"hotel", "accommodation", "train"} for domain in domains)


def _build_matched_reason(
    text: str,
    trigger: bool,
    domains: list[str],
    destination: str | None,
    include_profile: bool,
    include_slices: bool,
) -> str:
    if not trigger:
        if any(phrase in text for phrase in _CURRENT_TRIP_PHRASES):
            return "current-trip question"
        return "no historical recall cue"

    parts = []
    history_hits = [phrase for phrase in _HISTORY_PHRASES if phrase in text]
    if history_hits:
        parts.append(f"history cue: {history_hits[0]}")
    elif _is_direct_profile_recall_query(text, domains):
        parts.append("profile cue")
    if destination:
        parts.append(f"destination={destination}")
    if domains:
        parts.append(f"domains={','.join(domains)}")
    if include_profile:
        parts.append("profile recall enabled")
    if include_slices:
        parts.append("slice recall enabled")
    return "; ".join(parts) or "historical recall cue"


def _is_direct_profile_recall_query(text: str, domains: list[str]) -> bool:
    return any(domain in _PROFILE_RECALL_DOMAINS for domain in domains) and any(
        word in text for word in _PROFILE_HINT_WORDS
    )


def _extract_domains(text: str) -> list[str]:
    domains: list[str] = []
    for domain, trigger_words, _ in _DOMAIN_RULES:
        if any(word in text for word in trigger_words) and domain not in domains:
            domains.append(domain)
    return domains


def _extract_keywords(text: str) -> list[str]:
    keywords: list[str] = []
    for _, trigger_words, keywords_for_domain in _DOMAIN_RULES:
        for word in (*trigger_words, *keywords_for_domain):
            if word in text and word not in keywords:
                keywords.append(word)
    for phrase in _HISTORY_PHRASES:
        if phrase in text and phrase not in keywords:
            keywords.append(phrase)
    return keywords


def _extract_destination(text: str) -> str | None:
    for destination in _KNOWN_DESTINATIONS:
        if destination in text:
            return destination
    return None


def _profile_item_domains(item: MemoryProfileItem) -> set[str]:
    domains = {item.domain}
    recall_domains = item.recall_hints.get("domains")
    if isinstance(recall_domains, list):
        for domain in recall_domains:
            if isinstance(domain, str) and domain:
                domains.add(domain)
    return domains


def _profile_item_search_terms(item: MemoryProfileItem) -> list[str]:
    terms: list[str] = [item.domain, item.key, _stringify(item.value), item.applicability]
    for key, value in item.context.items():
        terms.append(str(key))
        terms.append(_stringify(value))

    recall_hints = item.recall_hints
    for field_name in ("keywords", "aliases", "domains"):
        values = recall_hints.get(field_name)
        if isinstance(values, list):
            terms.extend(_stringify(value) for value in values)
    for source_ref in item.source_refs:
        if isinstance(source_ref, dict):
            terms.extend(_stringify(value) for value in source_ref.values())
    return [term for term in terms if term]


def _slice_search_terms(slice_: EpisodeSlice) -> list[str]:
    terms: list[str] = [
        slice_.slice_type,
        _stringify(slice_.content),
        _stringify(slice_.applicability),
        _stringify(slice_.entities.get("destination")),
    ]
    for key, value in slice_.entities.items():
        terms.append(str(key))
        terms.append(_stringify(value))
    terms.extend(slice_.keywords)
    return [term for term in terms if term]


def _match_destination(query: RecallQuery, slice_: EpisodeSlice) -> str | None:
    destination = query.entities.get("destination")
    if destination and _stringify(slice_.entities.get("destination")) == destination:
        return destination
    return None


def _matched_keywords(haystack_terms: list[str], search_terms: list[str]) -> list[str]:
    matches: list[str] = []
    joined_text = "\n".join(search_terms)
    for keyword in haystack_terms:
        if keyword and keyword in joined_text and keyword not in matches:
            matches.append(keyword)
    return matches


def _normalize_text(message: str) -> str:
    return " ".join(str(message).split())


def _parse_timestamp(value: str) -> float:
    if not value:
        return 0.0
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
