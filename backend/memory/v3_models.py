from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return "|".join(
            f"{key}:{_normalize_value(value[key])}" for key in sorted(value)
        )
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if isinstance(value, set):
            items = sorted(items, key=_normalize_value)
        return "|".join(_normalize_value(item) for item in items)
    return str(value).strip()


@dataclass
class MemoryProfileItem:
    id: str
    domain: str
    key: str
    value: Any
    polarity: str
    stability: str
    confidence: float
    status: str
    context: dict[str, Any] = field(default_factory=dict)
    applicability: str = ""
    recall_hints: dict[str, Any] = field(default_factory=dict)
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "domain": self.domain,
            "key": self.key,
            "value": self.value,
            "polarity": self.polarity,
            "stability": self.stability,
            "confidence": self.confidence,
            "status": self.status,
            "context": self.context,
            "applicability": self.applicability,
            "recall_hints": self.recall_hints,
            "source_refs": self.source_refs,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryProfileItem":
        return cls(
            id=str(data.get("id", "")),
            domain=str(data.get("domain", "")),
            key=str(data.get("key", "")),
            value=data.get("value"),
            polarity=str(data.get("polarity", "")),
            stability=str(data.get("stability", "")),
            confidence=float(data.get("confidence", 0.0)),
            status=str(data.get("status", "")),
            context=dict(data.get("context", {})),
            applicability=str(data.get("applicability", "")),
            recall_hints=dict(data.get("recall_hints", {})),
            source_refs=list(data.get("source_refs", [])),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


def generate_profile_item_id(bucket: str, item: MemoryProfileItem) -> str:
    if bucket == "rejections":
        return f"{bucket}:{item.domain}:{item.key}:{_normalize_value(item.value)}"
    if bucket == "preference_hypotheses":
        return f"{bucket}:{item.domain}:{item.key}:{_normalize_value(item.context)}"
    return f"{bucket}:{item.domain}:{item.key}"


@dataclass
class UserMemoryProfile:
    schema_version: int
    user_id: str
    constraints: list[MemoryProfileItem] = field(default_factory=list)
    rejections: list[MemoryProfileItem] = field(default_factory=list)
    stable_preferences: list[MemoryProfileItem] = field(default_factory=list)
    preference_hypotheses: list[MemoryProfileItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "user_id": self.user_id,
            "constraints": [item.to_dict() for item in self.constraints],
            "rejections": [item.to_dict() for item in self.rejections],
            "stable_preferences": [item.to_dict() for item in self.stable_preferences],
            "preference_hypotheses": [
                item.to_dict() for item in self.preference_hypotheses
            ],
        }

    @classmethod
    def empty(cls, user_id: str) -> "UserMemoryProfile":
        return cls(schema_version=3, user_id=user_id)

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], user_id: str | None = None
    ) -> "UserMemoryProfile":
        return cls(
            schema_version=int(data.get("schema_version", 3)),
            user_id=str(data.get("user_id", user_id or "")),
            constraints=[
                MemoryProfileItem.from_dict(item) for item in data.get("constraints", [])
            ],
            rejections=[
                MemoryProfileItem.from_dict(item) for item in data.get("rejections", [])
            ],
            stable_preferences=[
                MemoryProfileItem.from_dict(item)
                for item in data.get("stable_preferences", [])
            ],
            preference_hypotheses=[
                MemoryProfileItem.from_dict(item)
                for item in data.get("preference_hypotheses", [])
            ],
        )


@dataclass
class WorkingMemoryItem:
    id: str
    phase: int
    kind: str
    domains: list[str]
    content: str
    reason: str
    status: str
    expires: dict[str, bool]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase": self.phase,
            "kind": self.kind,
            "domains": self.domains,
            "content": self.content,
            "reason": self.reason,
            "status": self.status,
            "expires": self.expires,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkingMemoryItem":
        return cls(
            id=str(data.get("id", "")),
            phase=int(data.get("phase", 0)),
            kind=str(data.get("kind", "note")),
            domains=list(data.get("domains", [])),
            content=str(data.get("content", "")),
            reason=str(data.get("reason", "")),
            status=str(data.get("status", "active")),
            expires=dict(data.get("expires", {})),
            created_at=str(data.get("created_at", "")),
        )


@dataclass
class SessionWorkingMemory:
    schema_version: int
    user_id: str
    session_id: str
    trip_id: str | None
    items: list[WorkingMemoryItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "trip_id": self.trip_id,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def empty(
        cls, user_id: str, session_id: str, trip_id: str | None
    ) -> "SessionWorkingMemory":
        return cls(schema_version=1, user_id=user_id, session_id=session_id, trip_id=trip_id)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionWorkingMemory":
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            user_id=str(data.get("user_id", "")),
            session_id=str(data.get("session_id", "")),
            trip_id=data.get("trip_id"),
            items=[WorkingMemoryItem.from_dict(item) for item in data.get("items", [])],
        )


@dataclass
class EpisodeSlice:
    id: str
    user_id: str
    source_episode_id: str
    source_trip_id: str | None
    slice_type: str
    domains: list[str]
    entities: dict[str, Any]
    keywords: list[str]
    content: str
    applicability: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "source_episode_id": self.source_episode_id,
            "source_trip_id": self.source_trip_id,
            "slice_type": self.slice_type,
            "domains": self.domains,
            "entities": self.entities,
            "keywords": self.keywords,
            "content": self.content,
            "applicability": self.applicability,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EpisodeSlice":
        return cls(
            id=str(data.get("id", "")),
            user_id=str(data.get("user_id", "")),
            source_episode_id=str(data.get("source_episode_id", "")),
            source_trip_id=data.get("source_trip_id"),
            slice_type=str(data.get("slice_type", "general")),
            domains=list(data.get("domains", [])),
            entities=dict(data.get("entities", {})),
            keywords=list(data.get("keywords", [])),
            content=str(data.get("content", "")),
            applicability=str(data.get("applicability", "")),
            created_at=str(data.get("created_at", "")),
        )
