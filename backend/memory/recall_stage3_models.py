from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from memory.recall_query import RecallRetrievalPlan
from memory.retrieval_candidates import RecallCandidate


@dataclass(frozen=True)
class SourcePolicy:
    requested_source: str
    search_profile: bool
    search_slices: bool
    widened: bool = False
    widening_reason: str = ""


@dataclass(frozen=True)
class RecallQueryEnvelope:
    plan: RecallRetrievalPlan
    user_message: str
    source_policy: SourcePolicy
    original_domains: tuple[str, ...]
    expanded_domains: tuple[str, ...]
    original_keywords: tuple[str, ...]
    expanded_keywords: tuple[str, ...]
    destination: str
    destination_canonical: str = ""
    destination_aliases: tuple[str, ...] = ()
    destination_children: tuple[str, ...] = ()
    destination_region: str = ""


@dataclass
class RetrievalEvidence:
    item_id: str
    source: str
    lanes: list[str] = field(default_factory=list)
    lane_scores: dict[str, float] = field(default_factory=dict)
    lane_ranks: dict[str, int] = field(default_factory=dict)
    fused_score: float = 0.0
    matched_domains: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    matched_entities: list[str] = field(default_factory=list)
    destination_match_type: str = "none"
    semantic_score: float | None = None
    lexical_score: float | None = None
    temporal_score: float | None = None
    retrieval_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "source": self.source,
            "lanes": list(self.lanes),
            "lane_scores": dict(self.lane_scores),
            "lane_ranks": dict(self.lane_ranks),
            "fused_score": self.fused_score,
            "matched_domains": list(self.matched_domains),
            "matched_keywords": list(self.matched_keywords),
            "matched_entities": list(self.matched_entities),
            "destination_match_type": self.destination_match_type,
            "semantic_score": self.semantic_score,
            "lexical_score": self.lexical_score,
            "temporal_score": self.temporal_score,
            "retrieval_reason": self.retrieval_reason,
        }


@dataclass(frozen=True)
class Stage3Candidate:
    candidate: RecallCandidate
    evidence: RetrievalEvidence


@dataclass
class Stage3LaneResult:
    lane_name: str
    candidates: list[Stage3Candidate]
    error: str = ""


@dataclass
class Stage3Telemetry:
    lanes_attempted: list[str] = field(default_factory=list)
    lanes_succeeded: list[str] = field(default_factory=list)
    source_policy: dict[str, Any] = field(default_factory=dict)
    query_expansion: dict[str, list[str]] = field(default_factory=dict)
    candidates_by_lane: dict[str, int] = field(default_factory=dict)
    candidates_by_source: dict[str, int] = field(default_factory=dict)
    total_candidates_before_fusion: int = 0
    total_candidates_after_fusion: int = 0
    zero_hit: bool = False
    fallback_used: str = "none"
    lane_errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lanes_attempted": list(self.lanes_attempted),
            "lanes_succeeded": list(self.lanes_succeeded),
            "source_policy": dict(self.source_policy),
            "query_expansion": {
                key: list(value) for key, value in self.query_expansion.items()
            },
            "candidates_by_lane": dict(self.candidates_by_lane),
            "candidates_by_source": dict(self.candidates_by_source),
            "total_candidates_before_fusion": self.total_candidates_before_fusion,
            "total_candidates_after_fusion": self.total_candidates_after_fusion,
            "zero_hit": self.zero_hit,
            "fallback_used": self.fallback_used,
            "lane_errors": dict(self.lane_errors),
        }


@dataclass
class Stage3RecallResult:
    candidates: list[RecallCandidate]
    evidence_by_id: dict[str, RetrievalEvidence]
    telemetry: Stage3Telemetry
