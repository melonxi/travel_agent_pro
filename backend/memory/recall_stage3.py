from __future__ import annotations

from collections import Counter
from typing import Any

from config import Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3_fusion import fuse_lane_results
from memory.recall_stage3_lanes import SymbolicLane
from memory.recall_stage3_models import (
    Stage3LaneResult,
    Stage3RecallResult,
    Stage3Telemetry,
)
from memory.recall_stage3_normalizer import build_query_envelope
from memory.v3_models import EpisodeSlice, UserMemoryProfile
from state.models import TravelPlanState


def retrieve_recall_candidates(
    *,
    query: RecallRetrievalPlan,
    profile: UserMemoryProfile,
    slices: list[EpisodeSlice],
    user_message: str,
    plan: TravelPlanState,
    config: Stage3RecallConfig,
    embedding_provider: Any = None,
) -> Stage3RecallResult:
    del embedding_provider

    envelope = build_query_envelope(
        query=query,
        user_message=user_message,
        plan=plan,
        config=config,
    )
    telemetry = Stage3Telemetry(
        source_policy={
            "requested_source": envelope.source_policy.requested_source,
            "search_profile": envelope.source_policy.search_profile,
            "search_slices": envelope.source_policy.search_slices,
            "widened": envelope.source_policy.widened,
            "widening_reason": envelope.source_policy.widening_reason,
        },
        query_expansion={
            "original_domains": list(envelope.original_domains),
            "expanded_domains": list(envelope.expanded_domains),
            "original_keywords": list(envelope.original_keywords),
            "expanded_keywords": list(envelope.expanded_keywords),
            "destination_aliases": list(envelope.destination_aliases),
            "destination_children": list(envelope.destination_children),
        },
        fallback_used=query.fallback_used,
    )

    lane_results: list[Stage3LaneResult] = []
    if config.symbolic.enabled:
        lane_name = SymbolicLane.lane_name
        telemetry.lanes_attempted.append(lane_name)
        try:
            lane_result = SymbolicLane().run(envelope, profile, slices, config)
        except Exception as exc:
            telemetry.lane_errors[lane_name] = str(exc)
        else:
            lane_results.append(lane_result)
            telemetry.candidates_by_lane[lane_name] = len(lane_result.candidates)
            telemetry.lanes_succeeded.append(lane_name)

    telemetry.total_candidates_before_fusion = sum(
        len(lane_result.candidates) for lane_result in lane_results
    )
    fused = fuse_lane_results(lane_results, config.fusion)
    telemetry.total_candidates_after_fusion = len(fused)
    telemetry.zero_hit = len(fused) == 0
    telemetry.candidates_by_source = dict(
        Counter(candidate.candidate.source for candidate in fused)
    )

    return Stage3RecallResult(
        candidates=[stage3_candidate.candidate for stage3_candidate in fused],
        evidence_by_id={
            stage3_candidate.candidate.item_id: stage3_candidate.evidence
            for stage3_candidate in fused
        },
        telemetry=telemetry,
    )
