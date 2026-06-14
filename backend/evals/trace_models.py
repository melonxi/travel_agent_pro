from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


TraceEventType = Literal[
    "run_start",
    "run_end",
    "llm_call",
    "llm_output",
    "tool_call",
    "tool_result",
    "state_snapshot",
    "state_diff",
    "phase_gate",
    "quality_gate",
    "soft_judge",
    "validation",
    "memory_recall",
    "memory_hit",
    "context_build",
    "context_rebuild",
    "phase_transition",
    "internal_task",
    "context_compression",
    "phase3_orchestrator",
    "phase3_worker",
    "deliverable_draft",
    "deliverable_finalize",
    "error",
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
    session_id: str | None = None
    trip_id: str | None = None
    context_epoch: int | None = None
    parent_event_id: str | None = None
    root_event_id: str | None = None
    correlation_id: str | None = None
    actor: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    payload_schema_version: int | None = None


@dataclass
class RubricResult:
    rubric_id: str
    status: RubricStatus
    score: int
    reason: str
    evidence_event_ids: list[str] = field(default_factory=list)
