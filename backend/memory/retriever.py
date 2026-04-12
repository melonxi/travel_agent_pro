from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from memory.models import MemoryItem


_CORE_PRIORITY_TYPES = {"constraint", "rejection"}
_PHASE_DOMAINS: dict[int, set[str]] = {
    1: {"destination", "pace", "budget", "family", "planning_style"},
    3: {
        "destination",
        "pace",
        "budget",
        "family",
        "hotel",
        "flight",
        "train",
        "accessibility",
    },
    5: {"pace", "food", "accessibility", "family", "budget"},
    7: {"documents", "flight", "train", "food", "accessibility"},
}


@dataclass
class MemoryRetriever:
    def retrieve_core_profile(
        self, items: list[MemoryItem], limit: int = 10
    ) -> list[MemoryItem]:
        filtered = [
            item
            for item in items
            if item.status == "active" and item.scope == "global"
        ]
        return self._rank(filtered)[:limit]

    def retrieve_trip_memory(
        self, items: list[MemoryItem], plan: Any
    ) -> list[MemoryItem]:
        trip_id = getattr(plan, "trip_id", None)
        if not trip_id:
            return []
        filtered = [
            item
            for item in items
            if item.status == "active"
            and item.scope == "trip"
            and item.trip_id == trip_id
        ]
        return self._rank(filtered)

    def retrieve_phase_relevant(
        self, items: list[MemoryItem], plan: Any, phase: int, limit: int = 8
    ) -> list[MemoryItem]:
        allowed_domains = _PHASE_DOMAINS.get(phase, set())
        if not allowed_domains:
            return []

        trip_id = getattr(plan, "trip_id", None)
        filtered: list[MemoryItem] = []
        for item in items:
            if item.status != "active":
                continue
            if item.domain not in allowed_domains:
                continue
            if item.scope == "trip" and item.trip_id != trip_id:
                continue
            if item.scope == "trip" and not trip_id:
                continue
            if item.scope not in {"global", "trip"}:
                continue
            filtered.append(item)
        return self._rank(filtered)[:limit]

    def _rank(self, items: list[MemoryItem]) -> list[MemoryItem]:
        return sorted(items, key=self._sort_key)

    def _sort_key(self, item: MemoryItem) -> tuple[Any, ...]:
        return (
            0 if item.type in _CORE_PRIORITY_TYPES else 1,
            -float(item.confidence),
            -self._parse_timestamp(item.updated_at),
            item.id,
        )

    def _parse_timestamp(self, value: str) -> float:
        if not value:
            return 0.0
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            return 0.0
