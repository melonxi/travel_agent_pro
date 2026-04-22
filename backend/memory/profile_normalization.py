from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from memory.v3_models import MemoryProfileItem

_CANONICAL_KEYS = {
    ("food", "dislike_spicy_food"): "avoid_spicy",
    ("food", "no_spicy"): "avoid_spicy",
    ("food", "avoid_spicy"): "avoid_spicy",
    ("flight", "avoid_red_eye"): "avoid_red_eye",
}

_DEFAULT_ALIASES = {
    ("food", "avoid_spicy"): ["不吃辣", "不能吃辣", "避开辣味"],
    ("flight", "avoid_red_eye"): ["红眼航班", "夜间航班"],
}

_DEFAULT_APPLICABILITY = {
    "constraints": "适用于所有旅行，除非用户明确临时允许。",
    "rejections": "适用于同类决策，除非用户明确改变主意。",
    "stable_preferences": "适用于大多数旅行。",
    "preference_hypotheses": "仅作为暂时偏好假设，需更多观察确认。",
}


def _dedupe(values: list[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _coerce_hint_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _context_observation_count(context: Any) -> int:
    if not isinstance(context, dict):
        return 0
    try:
        return max(int(context.get("observation_count", 0) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _merge_source_refs(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in [*left, *right]:
        if not isinstance(ref, dict):
            continue
        marker = json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(ref)
    return merged


def _same_profile_identity(
    bucket: str, left: MemoryProfileItem, right: MemoryProfileItem
) -> bool:
    if bucket in {"constraints", "stable_preferences"}:
        return left.domain == right.domain and left.key == right.key
    if bucket == "rejections":
        return (
            left.domain == right.domain
            and left.key == right.key
            and left.value == right.value
        )
    return (
        left.domain == right.domain
        and left.key == right.key
        and left.value == right.value
    )


def merge_profile_item_with_existing(
    bucket: str,
    incoming: MemoryProfileItem,
    existing_items: list[MemoryProfileItem],
) -> tuple[str, MemoryProfileItem]:
    matching = next(
        (
            item
            for item in existing_items
            if _same_profile_identity(bucket, item, incoming)
        ),
        None,
    )

    incoming_context = dict(incoming.context) if isinstance(incoming.context, dict) else {}
    incoming_context.pop("observation_count", None)
    incoming_source_refs = list(incoming.source_refs) if isinstance(incoming.source_refs, list) else []

    if matching is None:
        incoming_context["observation_count"] = 1
        return bucket, replace(
            incoming,
            context=incoming_context,
            source_refs=_merge_source_refs([], incoming_source_refs),
        )

    matching_context = dict(matching.context) if isinstance(matching.context, dict) else {}
    observation_count = max(_context_observation_count(matching_context), 1) + 1
    merged_context = dict(matching_context)
    merged_context.update(incoming_context)
    merged_context["observation_count"] = observation_count

    merged_bucket = bucket
    merged_stability = incoming.stability
    if bucket == "preference_hypotheses" and observation_count >= 2:
        merged_bucket = "stable_preferences"
        merged_stability = "pattern_observed"

    merged_source_refs = _merge_source_refs(
        list(matching.source_refs) if isinstance(matching.source_refs, list) else [],
        incoming_source_refs,
    )

    return merged_bucket, replace(
        incoming,
        stability=merged_stability,
        confidence=max(matching.confidence, incoming.confidence),
        context=merged_context,
        source_refs=merged_source_refs,
    )


def normalize_profile_item(bucket: str, item: MemoryProfileItem) -> MemoryProfileItem:
    canonical_key = _CANONICAL_KEYS.get((item.domain, item.key), item.key)
    hints = item.recall_hints if isinstance(item.recall_hints, dict) else {}
    domains = _dedupe([item.domain, *_coerce_hint_values(hints.get("domains"))])
    keywords = _dedupe(_coerce_hint_values(hints.get("keywords")))
    aliases = _dedupe(
        [
            *_coerce_hint_values(hints.get("aliases")),
            *_DEFAULT_ALIASES.get((item.domain, canonical_key), []),
        ]
    )
    applicability = item.applicability.strip() or _DEFAULT_APPLICABILITY.get(
        bucket, "适用于当前已知相似旅行。"
    )
    return replace(
        item,
        key=canonical_key,
        applicability=applicability,
        recall_hints={
            "domains": domains,
            "keywords": [value for value in keywords if value],
            "aliases": [value for value in aliases if value],
        },
    )
