from __future__ import annotations

from dataclasses import dataclass

from memory.recall_query import RecallRetrievalPlan
from memory.symbolic_recall import RecallQuery


_CONSERVATIVE_ALLOWED_BUCKETS = ["constraints", "rejections", "stable_preferences"]
_KNOWN_ALLOWED_BUCKETS = _CONSERVATIVE_ALLOWED_BUCKETS + ["preference_hypotheses"]


@dataclass
class LegacyRecallQueryAdapterResult:
    domains: list[str]
    keywords: list[str]
    entities: dict[str, str]
    include_profile: bool
    include_slices: bool
    allowed_buckets: list[str]
    strictness: str
    matched_reason: str


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _normalize_allowed_buckets(values: list[str]) -> list[str]:
    allowed = [bucket for bucket in values if bucket in _KNOWN_ALLOWED_BUCKETS]
    if not allowed:
        return list(_CONSERVATIVE_ALLOWED_BUCKETS)
    return _dedupe(allowed)


def plan_to_legacy_recall_query(plan: RecallRetrievalPlan) -> RecallQuery:
    adapter_result = LegacyRecallQueryAdapterResult(
        domains=list(plan.domains),
        keywords=_dedupe(list(plan.keywords) + list(plan.aliases)),
        entities={},
        include_profile=plan.source == "profile",
        include_slices=False,
        allowed_buckets=_normalize_allowed_buckets(list(plan.buckets)),
        strictness=plan.strictness,
        matched_reason=plan.reason,
    )

    return RecallQuery(
        needs_memory=True,
        domains=adapter_result.domains,
        entities=adapter_result.entities,
        keywords=adapter_result.keywords,
        include_profile=adapter_result.include_profile,
        include_slices=adapter_result.include_slices,
        include_working_memory=False,
        matched_reason=adapter_result.matched_reason,
        allowed_buckets=adapter_result.allowed_buckets,
        strictness=adapter_result.strictness,
    )
