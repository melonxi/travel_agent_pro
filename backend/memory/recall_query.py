from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RecallRetrievalPlan:
    source: str
    buckets: list[str]
    domains: list[str]
    keywords: list[str]
    aliases: list[str]
    strictness: str
    top_k: int
    reason: str
    fallback_used: str = "none"


def fallback_retrieval_plan() -> RecallRetrievalPlan:
    return RecallRetrievalPlan(
        source="profile",
        buckets=["constraints", "rejections", "stable_preferences"],
        domains=[],
        keywords=[],
        aliases=[],
        strictness="soft",
        top_k=5,
        reason="fallback_default_plan",
        fallback_used="fallback_default_plan",
    )


def _parse_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    if not all(isinstance(item, str) for item in value):
        return []

    return value


def _parse_top_k(value: Any) -> int:
    if isinstance(value, bool):
        return 5

    if isinstance(value, int):
        return min(value, 10) if value > 0 else 5

    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return 5

        return min(parsed, 10) if parsed > 0 else 5

    return 5


def _parse_string(value: Any, default: str) -> str:
    if isinstance(value, str):
        return value

    return default


def _parse_strictness(value: Any) -> str:
    if value in {"soft", "strict"}:
        return value

    return "soft"


def parse_recall_query_tool_arguments(payload: dict[str, Any] | None) -> RecallRetrievalPlan:
    if not isinstance(payload, dict):
        return fallback_retrieval_plan()

    source = payload.get("source")
    if source != "profile":
        fallback_plan = fallback_retrieval_plan()
        fallback_plan.reason = "invalid_query_plan"
        fallback_plan.fallback_used = "invalid_query_plan"
        return fallback_plan

    return RecallRetrievalPlan(
        source="profile",
        buckets=_parse_string_list(payload.get("buckets")),
        domains=_parse_string_list(payload.get("domains")),
        keywords=_parse_string_list(payload.get("keywords")),
        aliases=_parse_string_list(payload.get("aliases")),
        strictness=_parse_strictness(payload.get("strictness")),
        top_k=_parse_top_k(payload.get("top_k")),
        reason=_parse_string(payload.get("reason"), ""),
    )
