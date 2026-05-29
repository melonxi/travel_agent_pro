from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ALLOWED_RECALL_DOMAINS = (
    "itinerary",
    "pace",
    "food",
    "hotel",
    "accommodation",
    "flight",
    "train",
    "budget",
    "family",
    "accessibility",
    "planning_style",
    "documents",
    "general",
)

ALLOWED_PROFILE_BUCKETS = (
    "constraints",
    "rejections",
    "stable_preferences",
    "preference_hypotheses",
)

_COMMON_REQUIRED_FIELDS = {"source", "domains", "destination", "keywords", "top_k", "reason"}
_PROFILE_REQUIRED_FIELDS = _COMMON_REQUIRED_FIELDS | {"buckets"}
_HYBRID_REQUIRED_FIELDS = _COMMON_REQUIRED_FIELDS | {"buckets"}
_EPISODE_SLICE_REQUIRED_FIELDS = _COMMON_REQUIRED_FIELDS

_PROFILE_ALLOWED_FIELDS = _PROFILE_REQUIRED_FIELDS
_HYBRID_ALLOWED_FIELDS = _HYBRID_REQUIRED_FIELDS
_EPISODE_SLICE_ALLOWED_FIELDS = _EPISODE_SLICE_REQUIRED_FIELDS


@dataclass
class RecallRetrievalPlan:
    source: str
    buckets: list[str]
    domains: list[str]
    destination: str
    keywords: list[str]
    top_k: int
    reason: str
    fallback_used: str = "none"


@dataclass
class DualRecallPlan:
    need_profile: bool
    need_episode: bool
    profile_buckets: list[str]
    domains: list[str]
    destination: str
    keywords: list[str]
    top_k: int
    reason: str
    fallback_used: str = "none"

    @property
    def needs_recall(self) -> bool:
        return self.need_profile or self.need_episode


_DEFAULT_PROFILE_BUCKETS = ["constraints", "rejections", "stable_preferences"]
_EPISODE_QUERY_HINTS = ("上次", "之前", "以前", "住哪里", "住哪", "订哪家", "怎么安排")
_PROFILE_QUERY_HINTS = ("习惯", "偏好", "喜欢", "不喜欢", "不住", "不坐", "不要", "避开", "说过")
_KNOWN_DESTINATION_HINTS = (
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


def dual_plan_from_retrieval_plan(plan: RecallRetrievalPlan) -> DualRecallPlan:
    source = plan.source
    need_profile = source in {"profile", "hybrid_history"}
    need_episode = source in {"episode_slice", "hybrid_history"}
    profile_buckets = list(plan.buckets) if need_profile else []
    return DualRecallPlan(
        need_profile=need_profile,
        need_episode=need_episode,
        profile_buckets=profile_buckets,
        domains=list(plan.domains),
        destination=plan.destination,
        keywords=list(plan.keywords),
        top_k=plan.top_k,
        reason=plan.reason,
        fallback_used=plan.fallback_used,
    )


def dual_plan_from_gate(
    *,
    intent_type: str,
    user_message: str,
    stage0_reason: str,
    stage0_signals: dict[str, list[str] | tuple[str, ...]] | None,
) -> DualRecallPlan:
    need_profile = False
    need_episode = False
    text = str(user_message or "")
    normalized_signals = _normalize_stage0_signal_dict(stage0_signals)

    if intent_type in {"profile_preference_recall", "profile_constraint_recall"}:
        need_profile = True
    elif intent_type == "past_trip_experience_recall":
        need_episode = True
    elif intent_type == "mixed_or_ambiguous":
        need_profile = True
        need_episode = True
    elif stage0_reason == "explicit_profile_history_query":
        has_style = bool(normalized_signals.get("style"))
        has_history = bool(normalized_signals.get("history"))
        has_episode_text = _has_episode_query_text(text)
        has_profile_text = _has_profile_query_text(text)
        if has_style and not has_episode_text:
            need_profile = True
        if has_history and has_episode_text:
            need_episode = True
        if has_history and has_profile_text:
            need_profile = True
        if not need_profile and not need_episode:
            need_profile = True
            need_episode = True

    return DualRecallPlan(
        need_profile=need_profile,
        need_episode=need_episode,
        profile_buckets=list(_DEFAULT_PROFILE_BUCKETS) if need_profile else [],
        domains=[],
        destination=_extract_destination_hint(text),
        keywords=_extract_keyword_hints(text),
        top_k=5,
        reason=stage0_reason or intent_type or "dual_recall_plan_from_gate",
    )


def _normalize_stage0_signal_dict(
    stage0_signals: dict[str, list[str] | tuple[str, ...]] | None,
) -> dict[str, list[str]]:
    if not isinstance(stage0_signals, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for key, values in stage0_signals.items():
        if isinstance(key, str) and isinstance(values, (list, tuple)):
            normalized[key] = [value for value in values if isinstance(value, str)]
    return normalized


def _has_episode_query_text(text: str) -> bool:
    return any(hint in text for hint in _EPISODE_QUERY_HINTS) or bool(
        _extract_destination_hint(text)
    )


def _has_profile_query_text(text: str) -> bool:
    return any(hint in text for hint in _PROFILE_QUERY_HINTS)


def _extract_destination_hint(text: str) -> str:
    for destination in _KNOWN_DESTINATION_HINTS:
        if destination in text:
            return destination
    return ""


def _extract_keyword_hints(text: str) -> list[str]:
    keywords: list[str] = []
    for hint in (*_EPISODE_QUERY_HINTS, *_PROFILE_QUERY_HINTS):
        if hint in text and hint not in keywords:
            keywords.append(hint)
    return keywords


def fallback_retrieval_plan() -> RecallRetrievalPlan:
    return RecallRetrievalPlan(
        source="hybrid_history",
        buckets=["constraints", "rejections", "stable_preferences"],
        domains=[],
        destination="",
        keywords=[],
        top_k=5,
        reason="fallback_default_plan",
        fallback_used="fallback_default_plan",
    )


def _invalid_query_plan() -> RecallRetrievalPlan:
    fallback_plan = fallback_retrieval_plan()
    fallback_plan.reason = "invalid_query_plan"
    fallback_plan.fallback_used = "invalid_query_plan"
    return fallback_plan


def _parse_domains(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    if any(item not in ALLOWED_RECALL_DOMAINS for item in value):
        return None
    return value


def _parse_keywords(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _parse_destination(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()


def _parse_buckets(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    if any(item not in ALLOWED_PROFILE_BUCKETS for item in value):
        return None
    return value


def _parse_top_k(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0:
        return None
    return min(value, 10)


def _parse_reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    reason = value.strip()
    if not reason:
        return None
    return reason[:160]


def _allowed_and_required_fields(source: str) -> tuple[set[str], set[str]] | None:
    if source == "profile":
        return _PROFILE_ALLOWED_FIELDS, _PROFILE_REQUIRED_FIELDS
    if source == "episode_slice":
        return _EPISODE_SLICE_ALLOWED_FIELDS, _EPISODE_SLICE_REQUIRED_FIELDS
    if source == "hybrid_history":
        return _HYBRID_ALLOWED_FIELDS, _HYBRID_REQUIRED_FIELDS
    return None


def parse_recall_query_tool_arguments(payload: dict[str, Any] | None) -> RecallRetrievalPlan:
    if not isinstance(payload, dict):
        return fallback_retrieval_plan()

    source = payload.get("source")
    if source not in {"profile", "episode_slice", "hybrid_history"}:
        return _invalid_query_plan()

    field_contract = _allowed_and_required_fields(source)
    if field_contract is None:
        return _invalid_query_plan()
    allowed_fields, required_fields = field_contract
    payload_keys = set(payload.keys())
    if not required_fields.issubset(payload_keys):
        return _invalid_query_plan()
    if payload_keys - allowed_fields:
        return _invalid_query_plan()

    domains = _parse_domains(payload.get("domains"))
    destination = _parse_destination(payload.get("destination"))
    keywords = _parse_keywords(payload.get("keywords"))
    top_k = _parse_top_k(payload.get("top_k"))
    reason = _parse_reason(payload.get("reason"))
    if None in (domains, destination, keywords, top_k, reason):
        return _invalid_query_plan()

    buckets: list[str] = []
    if source in {"profile", "hybrid_history"}:
        buckets = _parse_buckets(payload.get("buckets")) or []
        if not buckets:
            return _invalid_query_plan()

    return RecallRetrievalPlan(
        source=source,
        buckets=buckets,
        domains=domains,
        destination=destination,
        keywords=keywords,
        top_k=top_k,
        reason=reason,
    )
