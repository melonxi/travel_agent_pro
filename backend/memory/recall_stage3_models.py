from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
        return asdict(self)


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
    query_expansion: dict[str, Any] = field(default_factory=dict)
    candidates_by_lane: dict[str, int] = field(default_factory=dict)
    candidates_by_source: dict[str, int] = field(default_factory=dict)
    total_candidates_before_fusion: int = 0
    total_candidates_after_fusion: int = 0
    zero_hit: bool = False
    fallback_used: str = "none"
    lane_errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Stage3RecallResult:
    candidates: list[Stage3Candidate]
    evidence_by_id: dict[str, RetrievalEvidence]
    telemetry: Stage3Telemetry
