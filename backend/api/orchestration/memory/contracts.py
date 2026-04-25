from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from memory.async_jobs import MemoryJobScheduler
from memory.recall_query import RecallRetrievalPlan


@dataclass
class MemoryExtractionOutcome:
    status: str
    message: str
    item_ids: list[str]
    saved_profile_count: int = 0
    saved_working_count: int = 0
    reason: str | None = None
    error: str | None = None

    @property
    def saved_total(self) -> int:
        return self.saved_profile_count + self.saved_working_count

    def to_result(self) -> dict[str, Any]:
        result = {
            "item_ids": list(self.item_ids),
            "count": len(self.item_ids),
            "saved_profile_count": self.saved_profile_count,
            "saved_working_count": self.saved_working_count,
            "saved_total": self.saved_total,
        }
        if self.reason:
            result["reason"] = self.reason
        return result


@dataclass
class MemoryExtractionProgress:
    saved_profile_count: int = 0
    saved_working_count: int = 0
    pending_ids: list[str] = field(default_factory=list)

    @property
    def saved_total(self) -> int:
        return self.saved_profile_count + self.saved_working_count


@dataclass
class MemoryRouteSaveProgress:
    saved_count: int = 0
    pending_ids: list[str] = field(default_factory=list)


@dataclass
class MemoryExtractionGateDecision:
    should_extract: bool
    reason: str
    message: str
    routes: dict[str, bool] = field(default_factory=dict)
    error: str | None = None

    @property
    def status(self) -> str:
        if self.reason in {"timeout", "error", "no_tool_result"}:
            return "warning"
        if self.should_extract:
            return "success"
        return "skipped"

    def to_result(self) -> dict[str, Any]:
        result = {
            "should_extract": self.should_extract,
            "reason": self.reason,
            "routes": dict(self.routes),
        }
        if self.error:
            result["error"] = self.error
        return result


@dataclass
class MemorySchedulerRuntime:
    scheduler: MemoryJobScheduler
    last_consumed_user_count: int = 0


@dataclass
class MemoryRecallDecision:
    needs_recall: bool
    stage0_decision: str
    stage0_reason: str
    stage0_matched_rule: str = ""
    stage0_signals: dict[str, list[str]] = field(default_factory=dict)
    intent_type: str = ""
    reason: str = ""
    confidence: float | None = None
    fallback_used: str = "none"
    recall_skip_source: str = ""
    gate_user_window: list[str] = field(default_factory=list)


@dataclass
class RecallQueryPlanResult:
    plan: RecallRetrievalPlan | None
    query_plan_source: str
    query_plan_fallback: str = "none"
