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
