from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


TraceEventType = Literal[
    "llm_call",
    "tool_call",
    "memory_recall",
    "memory_hit",
    "phase_transition",
    "internal_task",
    "context_compression",
]

RubricStatus = Literal["pass", "fail", "skip"]


@dataclass
class TraceEvent:
    event_id: str
    run_id: str
    sequence: int
    event_type: TraceEventType
    phase: int | None
    phase2_step: str | None
    iteration: int | None
    tool_name: str | None
    llm_provider: str | None
    llm_model: str | None
    status: str | None
    duration_ms: float | None
    cost_usd: float | None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass
class RubricResult:
    rubric_id: str
    status: RubricStatus
    score: int
    reason: str
    evidence_event_ids: list[str] = field(default_factory=list)
