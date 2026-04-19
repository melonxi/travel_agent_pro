from __future__ import annotations

from dataclasses import dataclass
from typing import Any


VALID_INTERNAL_TASK_STATUSES = {"pending", "success", "warning", "error", "skipped"}
VALID_INTERNAL_TASK_SCOPES = {"turn", "background", "session"}


@dataclass
class InternalTask:
    id: str
    kind: str
    label: str
    status: str
    message: str | None = None
    blocking: bool = True
    scope: str = "turn"
    related_tool_call_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float | None = None
    ended_at: float | None = None

    def __post_init__(self) -> None:
        if self.status not in VALID_INTERNAL_TASK_STATUSES:
            raise ValueError(f"Invalid internal task status: {self.status!r}")
        if self.scope not in VALID_INTERNAL_TASK_SCOPES:
            raise ValueError(f"Invalid internal task scope: {self.scope!r}")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "blocking": self.blocking,
            "scope": self.scope,
        }
        if self.message is not None:
            d["message"] = self.message
        if self.related_tool_call_id is not None:
            d["related_tool_call_id"] = self.related_tool_call_id
        if self.result is not None:
            d["result"] = self.result
        if self.error is not None:
            d["error"] = self.error
        if self.started_at is not None:
            d["started_at"] = self.started_at
        if self.ended_at is not None:
            d["ended_at"] = self.ended_at
        return d
