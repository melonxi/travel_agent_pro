from __future__ import annotations

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
