from __future__ import annotations

from collections import Counter
from typing import Any

from config import Stage3FusionConfig, Stage3RecallConfig
from memory.recall_query import DualRecallPlan, RecallRetrievalPlan
from memory.recall_stage3_fusion import fuse_lane_results
from memory.recall_stage3_lanes import LexicalLane, SemanticLane, SymbolicLane
from memory.recall_stage3_models import (
    DualStage3RecallResult,
    RetrievalEvidence,
    SourceStage3RecallResult,
    Stage3LaneResult,
    Stage3RecallResult,
    Stage3Telemetry,
)
from memory.retrieval_candidates import RecallCandidate
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
    sidecar_store: Any = None,
    user_id: str = "",
) -> Stage3RecallResult:
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
        lane_result = SymbolicLane().run(envelope, profile, slices, config)
        telemetry.candidates_by_lane[lane_name] = len(lane_result.candidates)
        if lane_result.error:
            telemetry.lane_errors[lane_name] = lane_result.error
        else:
            lane_results.append(lane_result)
            telemetry.lanes_succeeded.append(lane_name)
    if config.lexical.enabled:
        lane_name = LexicalLane.lane_name
        telemetry.lanes_attempted.append(lane_name)
        lane_result = LexicalLane().run(envelope, profile, slices, config)
        telemetry.candidates_by_lane[lane_name] = len(lane_result.candidates)
        if lane_result.error:
            telemetry.lane_errors[lane_name] = lane_result.error
        else:
            lane_results.append(lane_result)
            telemetry.lanes_succeeded.append(lane_name)
    if config.semantic.enabled:
        lane_name = SemanticLane.lane_name
        telemetry.lanes_attempted.append(lane_name)
        lane_result = SemanticLane().run(
            envelope,
            profile,
            slices,
            config,
            embedding_provider,
            sidecar_store=sidecar_store,
            user_id=user_id,
        )
        telemetry.candidates_by_lane[lane_name] = len(lane_result.candidates)
        if lane_result.error:
            telemetry.lane_errors[lane_name] = lane_result.error
        else:
            lane_results.append(lane_result)
            telemetry.lanes_succeeded.append(lane_name)
        index_payload = lane_result.telemetry.get("semantic_embedding_index")
        if index_payload:
            telemetry.semantic_embedding_index = dict(index_payload)

    telemetry.total_candidates_before_fusion = sum(
        len(lane_result.candidates) for lane_result in lane_results
    )
    if _is_default_symbolic_only(config, telemetry):
        fused = lane_results[0].candidates if lane_results else []
    else:
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


def retrieve_dual_recall_candidates(
    *,
    query: DualRecallPlan,
    profile: UserMemoryProfile,
    slices: list[EpisodeSlice],
    user_message: str,
    plan: TravelPlanState,
    config: Stage3RecallConfig,
    embedding_provider: Any = None,
    sidecar_store: Any = None,
    user_id: str = "",
) -> DualStage3RecallResult:
    profile_result = _empty_source_result("profile")
    episode_result = _empty_source_result("episode_slice")

    if query.need_profile:
        profile_plan = RecallRetrievalPlan(
            source="profile",
            buckets=list(query.profile_buckets),
            domains=list(query.domains),
            destination=query.destination,
            keywords=list(query.keywords),
            top_k=query.top_k,
            reason=query.reason,
            fallback_used=query.fallback_used,
        )
        raw_profile = retrieve_recall_candidates(
            query=profile_plan,
            profile=profile,
            slices=[],
            user_message=user_message,
            plan=plan,
            config=config,
            embedding_provider=embedding_provider,
            sidecar_store=sidecar_store,
            user_id=user_id,
        )
        _assign_retrieval_scores(
            raw_profile.candidates, raw_profile.evidence_by_id, config.fusion.rrf_k
        )
        profile_result = SourceStage3RecallResult(
            source="profile",
            candidates=raw_profile.candidates,
            evidence_by_id=raw_profile.evidence_by_id,
            telemetry={"source": "profile", **raw_profile.telemetry.to_dict()},
        )

    if query.need_episode:
        episode_plan = RecallRetrievalPlan(
            source="episode_slice",
            buckets=[],
            domains=list(query.domains),
            destination=query.destination,
            keywords=list(query.keywords),
            top_k=query.top_k,
            reason=query.reason,
            fallback_used=query.fallback_used,
        )
        raw_episode = retrieve_recall_candidates(
            query=episode_plan,
            profile=profile,
            slices=slices,
            user_message=user_message,
            plan=plan,
            config=config,
            embedding_provider=embedding_provider,
            sidecar_store=sidecar_store,
            user_id=user_id,
        )
        _assign_retrieval_scores(
            raw_episode.candidates, raw_episode.evidence_by_id, config.fusion.rrf_k
        )
        episode_result = SourceStage3RecallResult(
            source="episode_slice",
            candidates=raw_episode.candidates,
            evidence_by_id=raw_episode.evidence_by_id,
            telemetry={"source": "episode_slice", **raw_episode.telemetry.to_dict()},
        )

    return DualStage3RecallResult(profile=profile_result, episode=episode_result)


def _empty_source_result(source: str) -> SourceStage3RecallResult:
    return SourceStage3RecallResult(
        source=source,
        candidates=[],
        evidence_by_id={},
        telemetry={"source": source},
    )


def _assign_retrieval_scores(
    candidates: list[RecallCandidate],
    evidence_by_id: dict[str, RetrievalEvidence],
    rrf_k: int,
) -> None:
    if not candidates:
        return

    evidence_items = [
        evidence_by_id.get(
            candidate.item_id, RetrievalEvidence(candidate.item_id, candidate.source)
        )
        for candidate in candidates
    ]
    fused_scores = [evidence.fused_score for evidence in evidence_items]
    max_fused = max(fused_scores) if fused_scores else 0.0
    if max_fused > 0.0:
        for candidate, fused_score in zip(candidates, fused_scores):
            candidate.retrieval_score = fused_score / max_fused
        return

    ordinal_scores = [
        1.0 / float(rrf_k + index + 1) for index in range(len(candidates))
    ]
    max_ordinal = max(ordinal_scores)
    for candidate, evidence, ordinal_score in zip(
        candidates, evidence_items, ordinal_scores
    ):
        if evidence.semantic_score is not None:
            candidate.retrieval_score = max(0.0, min(evidence.semantic_score, 1.0))
        elif candidate.score > 0.0:
            candidate.retrieval_score = max(0.0, min(candidate.score, 1.0))
        else:
            candidate.retrieval_score = ordinal_score / max_ordinal


def _is_default_symbolic_only(
    config: Stage3RecallConfig,
    telemetry: Stage3Telemetry,
) -> bool:
    return (
        config.symbolic.enabled
        and not config.lexical.enabled
        and not config.semantic.enabled
        and not config.entity.enabled
        and not config.temporal.enabled
        and config.fusion == Stage3FusionConfig()
        and telemetry.lanes_succeeded == ["symbolic"]
    )
