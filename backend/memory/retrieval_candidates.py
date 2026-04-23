from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memory.v3_models import EpisodeSlice, MemoryProfileItem

_MAX_SLICE_SUMMARY_LEN = 78


@dataclass
class RecallCandidate:
    source: str
    item_id: str
    bucket: str
    score: float
    matched_reason: list[str]
    content_summary: str
    domains: list[str]
    applicability: str
    polarity: str = ""
    created_at: str = ""
    key: str = ""


def build_profile_candidates(
    ranked_items: list[tuple[str, MemoryProfileItem, str]],
) -> list[RecallCandidate]:
    total = len(ranked_items)
    candidates: list[RecallCandidate] = []
    for index, (bucket, item, reason) in enumerate(ranked_items):
        candidates.append(
            RecallCandidate(
                source="profile",
                item_id=item.id,
                bucket=bucket,
                score=_ordinal_normalized_rank(index, total),
                matched_reason=_split_reason(reason),
                content_summary=_profile_summary(item),
                domains=[item.domain],
                applicability=item.applicability,
                polarity=item.polarity,
                created_at=item.updated_at or item.created_at,
                key=item.key,
            )
        )
    return candidates


def build_episode_slice_candidates(
    ranked_slices: list[tuple[EpisodeSlice, str]],
) -> list[RecallCandidate]:
    total = len(ranked_slices)
    candidates: list[RecallCandidate] = []
    for index, (slice_, reason) in enumerate(ranked_slices):
        candidates.append(
            RecallCandidate(
                source="episode_slice",
                item_id=slice_.id,
                bucket=slice_.slice_type,
                score=_ordinal_normalized_rank(index, total),
                matched_reason=_split_reason(reason),
                content_summary=_slice_summary(slice_.content),
                domains=list(slice_.domains),
                applicability=slice_.applicability,
                created_at=slice_.created_at,
            )
        )
    return candidates


def _ordinal_normalized_rank(index: int, total: int) -> float:
    # This is only an ordinal score normalized within the current ranked list.
    # It preserves relative order for downstream consumers, not cross-query comparability.
    if total <= 0:
        return 0.0
    return float(total - index) / float(total)


def _split_reason(reason: str) -> list[str]:
    return [part.strip() for part in reason.split(";") if part.strip()]


def _profile_summary(item: MemoryProfileItem) -> str:
    value = _render_value(item.value)
    if value:
        return f"{item.domain}:{item.key}={value}"
    return f"{item.domain}:{item.key}"


def _slice_summary(content: str) -> str:
    summary = " ".join(content.split())
    if len(summary) <= _MAX_SLICE_SUMMARY_LEN:
        return summary
    return f"{summary[:_MAX_SLICE_SUMMARY_LEN]}..."


def _render_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "、".join(_render_value(item) for item in value if _render_value(item))
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            rendered = _render_value(value[key])
            if rendered:
                parts.append(f"{key}={rendered}")
        return "；".join(parts)
    return str(value).strip()
