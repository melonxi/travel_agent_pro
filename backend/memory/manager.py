# backend/memory/manager.py
from __future__ import annotations

from config import MemoryRerankerConfig, MemoryRetrievalConfig
from memory.formatter import MemoryRecallTelemetry, format_v3_memory_context
from memory.retrieval_candidates import RecallCandidate
from memory.recall_query import RecallRetrievalPlan
from memory.recall_reranker import RecallRerankResult, choose_reranker_path
from memory.symbolic_recall import (
    heuristic_retrieval_plan_from_message,
    rank_episode_slices,
    rank_profile_items,
    should_trigger_memory_recall,
)
from memory.v3_models import EpisodeSlice, WorkingMemoryItem
from memory.v3_store import FileMemoryV3Store
from state.models import TravelPlanState


_WORKING_MEMORY_LIMIT = 10
_QUERY_PROFILE_LIMIT = 5
_QUERY_SLICE_LIMIT = 5


async def select_recall_candidates(
    *,
    user_message: str,
    plan: TravelPlanState,
    retrieval_plan: RecallRetrievalPlan | None,
    candidates: list[RecallCandidate],
    reranker_config: MemoryRerankerConfig | None = None,
) -> tuple[list[RecallCandidate], RecallRerankResult]:
    if not candidates:
        return [], RecallRerankResult(
            selected_item_ids=[],
            final_reason="",
            per_item_reason={},
            fallback_used="none",
        )

    path = choose_reranker_path(
        candidates=candidates,
        user_message=user_message,
        plan=plan,
        retrieval_plan=retrieval_plan,
        config=reranker_config,
    )
    return list(path.selected_candidates), path.result


class MemoryManager:
    def __init__(
        self,
        data_dir: str = "./data",
        retrieval_config: MemoryRetrievalConfig | None = None,
    ):
        self.v3_store = FileMemoryV3Store(data_dir)
        self.retrieval_config = retrieval_config or MemoryRetrievalConfig()

    async def generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        recall_gate: bool | None = None,
        short_circuit: str = "undecided",
        retrieval_plan: RecallRetrievalPlan | None = None,
        stage0_matched_rule: str = "",
        stage0_signals: dict[str, list[str] | tuple[str, ...]] | None = None,
        query_plan_source: str = "",
        query_plan_fallback: str = "none",
    ) -> tuple[str, MemoryRecallTelemetry]:
        profile = await self.v3_store.load_profile(user_id)
        working_memory = await self.v3_store.load_working_memory(
            user_id,
            plan.session_id,
            plan.trip_id,
        )
        working_items = self._active_working_memory_items(working_memory.items)

        recall_candidates: list[RecallCandidate] = []
        normalized_stage0_signals = self._normalize_stage0_signals(stage0_signals)
        active_plan = retrieval_plan
        effective_query_plan_source = query_plan_source
        effective_query_plan_fallback = query_plan_fallback
        if active_plan is None and user_message:
            active_plan = heuristic_retrieval_plan_from_message(
                user_message,
                stage0_decision=short_circuit,
                stage0_signals=normalized_stage0_signals,
            )
            if not effective_query_plan_source:
                effective_query_plan_source = "heuristic"
        elif active_plan is not None and not effective_query_plan_source:
            effective_query_plan_source = (
                "default_fallback"
                if active_plan.fallback_used != "none"
                else "llm"
            )
        should_run_query_recall = False
        final_recall_decision = "no_recall_applied"
        if recall_gate is None:
            should_run_query_recall = bool(
                user_message and should_trigger_memory_recall(user_message)
            )
            final_recall_decision = (
                "query_recall_enabled"
                if should_run_query_recall
                else "no_recall_applied"
            )
        elif recall_gate:
            should_run_query_recall = True
            final_recall_decision = "query_recall_enabled"

        recall_attempted = should_run_query_recall and active_plan is not None
        if should_run_query_recall and active_plan is not None:
            query_profile_limit = (
                active_plan.top_k if active_plan is not None else _QUERY_PROFILE_LIMIT
            )
            recall_candidates.extend(
                rank_profile_items(active_plan, profile)[:query_profile_limit]
            )
            if active_plan.source in {"episode_slice", "hybrid_history"}:
                candidate_slices = await self.v3_store.list_episode_slices(
                    user_id,
                    destination=active_plan.destination or None,
                )
                recall_candidates.extend(
                    rank_episode_slices(active_plan, candidate_slices)[: active_plan.top_k]
                )

        selected_candidates = list(recall_candidates)
        rerank_result = RecallRerankResult(
            selected_item_ids=[],
            final_reason="",
            per_item_reason={},
            fallback_used="none",
        )
        if recall_candidates:
            selected_candidates, rerank_result = await select_recall_candidates(
                user_message=user_message,
                plan=plan,
                retrieval_plan=retrieval_plan,
                candidates=recall_candidates,
                reranker_config=self.retrieval_config.reranker,
            )

        telemetry = self._build_v3_telemetry(
            working_items,
            selected_candidates,
        )
        telemetry.stage0_decision = short_circuit
        telemetry.stage0_matched_rule = stage0_matched_rule
        telemetry.stage0_signals = normalized_stage0_signals
        telemetry.gate_needs_recall = recall_gate
        telemetry.final_recall_decision = final_recall_decision
        telemetry.candidate_count = len(recall_candidates)
        telemetry.recall_attempted_but_zero_hit = (
            recall_attempted and len(recall_candidates) == 0
        )
        telemetry.reranker_selected_ids = list(rerank_result.selected_item_ids)
        telemetry.reranker_final_reason = rerank_result.final_reason
        telemetry.reranker_fallback = rerank_result.fallback_used
        telemetry.reranker_per_item_reason = dict(rerank_result.per_item_reason)
        if recall_attempted and active_plan is not None:
            telemetry.query_plan = {
                "buckets": list(active_plan.buckets),
                "domains": list(active_plan.domains),
                "destination": active_plan.destination,
                "top_k": active_plan.top_k,
            }
            telemetry.query_plan_source = effective_query_plan_source
            telemetry.query_plan_fallback = (
                effective_query_plan_fallback
                if effective_query_plan_fallback != "none"
                else active_plan.fallback_used
            )
        context = format_v3_memory_context(
            working_items=working_items,
            recall_candidates=selected_candidates,
        )
        return context, telemetry

    def _active_working_memory_items(
        self, items: list[WorkingMemoryItem]
    ) -> list[WorkingMemoryItem]:
        active_items = [item for item in items if item.status == "active"]
        return active_items[:_WORKING_MEMORY_LIMIT]

    def _build_v3_telemetry(
        self,
        working_items: list[WorkingMemoryItem],
        recall_candidates: list[RecallCandidate],
    ) -> MemoryRecallTelemetry:
        query_profile_ids = self._dedupe_ids(
            [candidate.item_id for candidate in recall_candidates if candidate.source == "profile"]
        )
        working_memory_ids = self._dedupe_ids([item.id for item in working_items])
        slice_ids = self._dedupe_ids(
            [candidate.item_id for candidate in recall_candidates if candidate.source == "episode_slice"]
        )
        matched_reasons = self._dedupe_values(
            [reason for candidate in recall_candidates for reason in candidate.matched_reason]
        )
        return MemoryRecallTelemetry(
            sources={
                "query_profile": len(query_profile_ids),
                "working_memory": len(working_memory_ids),
                "episode_slice": len(slice_ids),
            },
            profile_ids=query_profile_ids,
            working_memory_ids=working_memory_ids,
            slice_ids=slice_ids,
            matched_reasons=matched_reasons,
        )

    def _dedupe_ids(self, values: list[str]) -> list[str]:
        return self._dedupe_values(values)

    def _dedupe_values(self, values: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        return deduped

    def _normalize_stage0_signals(
        self,
        signals: dict[str, list[str] | tuple[str, ...]] | None,
    ) -> dict[str, list[str]]:
        if not isinstance(signals, dict):
            return {}
        normalized: dict[str, list[str]] = {}
        for name, hits in signals.items():
            if not isinstance(name, str) or not isinstance(hits, (list, tuple)):
                continue
            normalized[name] = [hit for hit in hits if isinstance(hit, str)]
        return normalized
