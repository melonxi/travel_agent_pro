# backend/memory/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Rejection:
    item: str
    reason: str
    permanent: bool = False
    context: str = ""  # e.g. destination name for scoped rejections

    def to_dict(self) -> dict:
        return {
            "item": self.item,
            "reason": self.reason,
            "permanent": self.permanent,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Rejection:
        return cls(
            item=d["item"],
            reason=d["reason"],
            permanent=d.get("permanent", False),
            context=d.get("context", ""),
        )


@dataclass
class TripSummary:
    destination: str
    dates: str
    satisfaction: int | None = None
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "destination": self.destination,
            "dates": self.dates,
            "satisfaction": self.satisfaction,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TripSummary:
        return cls(
            destination=d["destination"],
            dates=d["dates"],
            satisfaction=d.get("satisfaction"),
            notes=d.get("notes", ""),
        )


@dataclass
class UserMemory:
    user_id: str
    explicit_preferences: dict[str, Any] = field(default_factory=dict)
    implicit_preferences: dict[str, Any] = field(default_factory=dict)
    trip_history: list[TripSummary] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "explicit_preferences": self.explicit_preferences,
            "implicit_preferences": self.implicit_preferences,
            "trip_history": [t.to_dict() for t in self.trip_history],
            "rejections": [r.to_dict() for r in self.rejections],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UserMemory:
        return cls(
            user_id=d["user_id"],
            explicit_preferences=d.get("explicit_preferences", {}),
            implicit_preferences=d.get("implicit_preferences", {}),
            trip_history=[TripSummary.from_dict(t) for t in d.get("trip_history", [])],
            rejections=[Rejection.from_dict(r) for r in d.get("rejections", [])],
        )
