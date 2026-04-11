# backend/memory/models.py
from __future__ import annotations

import hashlib
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


def _normalize_memory_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "|".join(sorted(_normalize_memory_value(item) for item in value))
    if isinstance(value, dict):
        return "|".join(
            f"{key}:{_normalize_memory_value(value[key])}" for key in sorted(value)
        )
    return str(value).strip()


def generate_memory_id(
    user_id: str,
    type: str,
    domain: str,
    key: str,
    scope: str,
    trip_id: str | None = None,
    value: Any = None,
) -> str:
    if type == "rejection":
        raw = (
            f"{user_id}:{type}:{domain}:{key}:{_normalize_memory_value(value)}:"
            f"{scope}:{trip_id or ''}"
        )
    else:
        raw = f"{user_id}:{type}:{domain}:{key}:{scope}:{trip_id or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class MemorySource:
    kind: str
    session_id: str
    message_id: str | None = None
    tool_call_id: str | None = None
    quote: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "tool_call_id": self.tool_call_id,
            "quote": self.quote,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemorySource":
        return cls(
            kind=str(data.get("kind", "")),
            session_id=str(data.get("session_id", "")),
            message_id=data.get("message_id"),
            tool_call_id=data.get("tool_call_id"),
            quote=data.get("quote"),
        )


@dataclass
class MemoryItem:
    id: str
    user_id: str
    type: str
    domain: str
    key: str
    value: Any
    scope: str
    polarity: str
    confidence: float
    status: str
    source: MemorySource
    created_at: str
    updated_at: str
    expires_at: str | None = None
    destination: str | None = None
    session_id: str | None = None
    trip_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "type": self.type,
            "domain": self.domain,
            "key": self.key,
            "value": self.value,
            "scope": self.scope,
            "polarity": self.polarity,
            "confidence": self.confidence,
            "status": self.status,
            "source": self.source.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "destination": self.destination,
            "session_id": self.session_id,
            "trip_id": self.trip_id,
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryItem":
        source = data.get("source", {})
        if isinstance(source, MemorySource):
            source_obj = source
        else:
            source_obj = MemorySource.from_dict(source)
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            type=str(data["type"]),
            domain=str(data["domain"]),
            key=str(data["key"]),
            value=data.get("value"),
            scope=str(data["scope"]),
            polarity=str(data.get("polarity", "neutral")),
            confidence=float(data.get("confidence", 0.0)),
            status=str(data.get("status", "active")),
            source=source_obj,
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            expires_at=data.get("expires_at"),
            destination=data.get("destination"),
            session_id=data.get("session_id"),
            trip_id=data.get("trip_id"),
            attributes=dict(data.get("attributes", {})),
        )


@dataclass
class MemoryCandidate:
    type: str
    domain: str
    key: str
    value: Any
    scope: str
    polarity: str
    confidence: float
    risk: str
    evidence: str
    reason: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "domain": self.domain,
            "key": self.key,
            "value": self.value,
            "scope": self.scope,
            "polarity": self.polarity,
            "confidence": self.confidence,
            "risk": self.risk,
            "evidence": self.evidence,
            "reason": self.reason,
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryCandidate":
        return cls(
            type=str(data.get("type", "preference")),
            domain=str(data.get("domain", "general")),
            key=str(data.get("key", "general")),
            value=data.get("value"),
            scope=str(data.get("scope", "trip")),
            polarity=str(data.get("polarity", "neutral")),
            confidence=float(data.get("confidence", 0.0)),
            risk=str(data.get("risk", "low")),
            evidence=str(data.get("evidence", "")),
            reason=str(data.get("reason", "")),
            attributes=dict(data.get("attributes", {})),
        )


@dataclass
class MemoryEvent:
    id: str
    user_id: str
    session_id: str
    event_type: str
    object_type: str
    object_payload: dict[str, Any]
    reason_text: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "event_type": self.event_type,
            "object_type": self.object_type,
            "object_payload": self.object_payload,
            "reason_text": self.reason_text,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEvent":
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            session_id=str(data["session_id"]),
            event_type=str(data["event_type"]),
            object_type=str(data["object_type"]),
            object_payload=dict(data.get("object_payload", {})),
            reason_text=data.get("reason_text"),
            created_at=str(data["created_at"]),
        )


@dataclass
class TripEpisode:
    id: str
    user_id: str
    session_id: str
    trip_id: str | None
    destination: str | None
    dates: str | None
    travelers: dict[str, Any] | None
    budget: dict[str, Any] | None
    selected_skeleton: dict[str, Any] | None
    final_plan_summary: str
    accepted_items: list[dict[str, Any]]
    rejected_items: list[dict[str, Any]]
    lessons: list[str]
    satisfaction: int | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "trip_id": self.trip_id,
            "destination": self.destination,
            "dates": self.dates,
            "travelers": self.travelers,
            "budget": self.budget,
            "selected_skeleton": self.selected_skeleton,
            "final_plan_summary": self.final_plan_summary,
            "accepted_items": self.accepted_items,
            "rejected_items": self.rejected_items,
            "lessons": self.lessons,
            "satisfaction": self.satisfaction,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TripEpisode":
        return cls(
            id=str(data["id"]),
            user_id=str(data["user_id"]),
            session_id=str(data["session_id"]),
            trip_id=data.get("trip_id"),
            destination=data.get("destination"),
            dates=data.get("dates"),
            travelers=data.get("travelers"),
            budget=data.get("budget"),
            selected_skeleton=data.get("selected_skeleton"),
            final_plan_summary=str(data.get("final_plan_summary", "")),
            accepted_items=list(data.get("accepted_items", [])),
            rejected_items=list(data.get("rejected_items", [])),
            lessons=list(data.get("lessons", [])),
            satisfaction=data.get("satisfaction"),
            created_at=str(data.get("created_at", "")),
        )
