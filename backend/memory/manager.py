# backend/memory/manager.py
from __future__ import annotations

from dataclasses import asdict, is_dataclass

from config import MemoryRetrievalConfig
from memory.destination_normalization import match_destination
from memory.embedding_provider import (
    CachedEmbeddingProvider,
    FastEmbedProvider,
    NullEmbeddingProvider,
)
from memory.formatter import MemoryRecallTelemetry, format_v3_memory_context
from memory.recall_stage3 import retrieve_dual_recall_candidates
from memory.retrieval_candidates import RecallCandidate
from memory.recall_query import (
    DualRecallPlan,
    RecallRetrievalPlan,
    dual_plan_from_retrieval_plan,
)
from memory.recall_reranker import (
    empty_rerank_result,
    rerank_episode_candidates,
    rerank_profile_candidates,
    selection_metrics_placeholder,
)
from memory.symbolic_recall import (
    heuristic_retrieval_plan_from_message,
    should_trigger_memory_recall,
)
from memory.v3_models import EpisodeSlice, WorkingMemoryItem
from memory.v3_store import FileMemoryV3Store
from state.models import TravelPlanState


_WORKING_MEMORY_LIMIT = 10
_QUERY_PROFILE_LIMIT = 5
_QUERY_SLICE_LIMIT = 5


class MemoryManager:
    def __init__(
        self,
        data_dir: str = "./data",
        retrieval_config: MemoryRetrievalConfig | None = None,
    ):
        self.v3_store = FileMemoryV3Store(data_dir)
        self.retrieval_config = retrieval_config or MemoryRetrievalConfig()
        self._embedding_provider = None
        self._sidecar_store = None

    def _get_stage3_embedding_provider(self):
        semantic_config = self.retrieval_config.stage3.semantic
        if not semantic_config.enabled:
            return None
        if self._embedding_provider is not None:
            return self._embedding_provider
        if semantic_config.provider != "fastembed":
            self._embedding_provider = NullEmbeddingProvider()
            return self._embedding_provider
        try:
            self._embedding_provider = CachedEmbeddingProvider(
                FastEmbedProvider(
                    model_name=semantic_config.model_name,
                    cache_dir=semantic_config.cache_dir,
                    local_files_only=semantic_config.local_files_only,
                ),
                max_items=semantic_config.cache_max_items,
            )
        except Exception:
            return None
        return self._embedding_provider

    def _get_sidecar_store(self):
        index_cfg = self.retrieval_config.stage3.semantic.embedding_index
        if not index_cfg.enabled:
            return None
        if self._sidecar_store is not None:
            return self._sidecar_store
        from memory.embedding_sidecar import SidecarStore

        self._sidecar_store = SidecarStore(data_dir=self.v3_store.data_dir)
        return self._sidecar_store

    async def warm_profile_item(self, user_id, bucket, item):
        import asyncio
        from datetime import datetime, timezone

        from memory.embedding_sidecar import (
            PROFILE_TEXT_BUILDER_VERSION,
            SidecarRow,
            compute_text_hash,
        )
        from memory.recall_stage3_lanes import _profile_item_text

        index_cfg = self.retrieval_config.stage3.semantic.embedding_index
        if not index_cfg.enabled or not index_cfg.warm_on_write:
            return
        if bucket not in index_cfg.warm_buckets:
            return
        provider = self._get_stage3_embedding_provider()
        store = self._get_sidecar_store()
        if provider is None or store is None:
            return

        text = _profile_item_text(bucket, item)
        if not text:
            return
        try:
            vectors = provider.embed([text])
        except Exception:
            return
        if not vectors:
            return
        vector = list(vectors[0])
        semantic_cfg = self.retrieval_config.stage3.semantic
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = SidecarRow(
            source="profile",
            item_id=item.id,
            text_hash=compute_text_hash(text),
            text_builder=PROFILE_TEXT_BUILDER_VERSION,
            embedding_provider=semantic_cfg.provider,
            embedding_model=semantic_cfg.model_name,
            dimension=len(vector),
            vector=vector,
            bucket=bucket,
            created_at=now,
            updated_at=now,
        )
        try:
            await asyncio.to_thread(store.upsert_many, user_id, [row])
        except Exception:
            return

    async def warm_episode_slice(self, user_id, slice_):
        import asyncio
        from datetime import datetime, timezone

        from memory.embedding_sidecar import (
            SLICE_TEXT_BUILDER_VERSION,
            SidecarRow,
            compute_text_hash,
        )
        from memory.recall_stage3_lanes import _slice_text

        index_cfg = self.retrieval_config.stage3.semantic.embedding_index
        if not index_cfg.enabled or not index_cfg.warm_on_write:
            return
        provider = self._get_stage3_embedding_provider()
        store = self._get_sidecar_store()
        if provider is None or store is None:
            return

        text = _slice_text(slice_)
        if not text:
            return
        try:
            vectors = provider.embed([text])
        except Exception:
            return
        if not vectors:
            return
        vector = list(vectors[0])
        semantic_cfg = self.retrieval_config.stage3.semantic
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = SidecarRow(
            source="episode_slice",
            item_id=slice_.id,
            text_hash=compute_text_hash(text),
            text_builder=SLICE_TEXT_BUILDER_VERSION,
            embedding_provider=semantic_cfg.provider,
            embedding_model=semantic_cfg.model_name,
            dimension=len(vector),
            vector=vector,
            bucket="",
            created_at=now,
            updated_at=now,
        )
        try:
            await asyncio.to_thread(store.upsert_many, user_id, [row])
        except Exception:
            return

    async def generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        recall_gate: bool | None = None,
        short_circuit: str = "undecided",
        retrieval_plan: RecallRetrievalPlan | DualRecallPlan | None = None,
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

        dual_plan = self._build_dual_plan(
            active_plan,
            user_message,
            short_circuit,
            normalized_stage0_signals,
        )
        recall_attempted = (
            should_run_query_recall and dual_plan is not None and dual_plan.needs_recall
        )
        stage3_result = None
        profile_candidates: list[RecallCandidate] = []
        episode_candidates: list[RecallCandidate] = []
        profile_rerank = empty_rerank_result()
        episode_rerank = empty_rerank_result()

        if recall_attempted and dual_plan is not None:
            should_load_slices = (
                dual_plan.need_episode
                or self.retrieval_config.stage3.source_widening.enabled
            )
            candidate_slices = []
            if should_load_slices:
                destination_filter = (
                    dual_plan.destination
                    if not self.retrieval_config.stage3.destination_normalization_enabled
                    else None
                )
                candidate_slices = await self.v3_store.list_episode_slices(
                    user_id,
                    destination=destination_filter or None,
                )
                candidate_slices = self._filter_slices_by_normalized_destination(
                    dual_plan.destination,
                    candidate_slices,
                )
            stage3_result = retrieve_dual_recall_candidates(
                query=dual_plan,
                profile=profile,
                slices=candidate_slices,
                user_message=user_message,
                plan=plan,
                config=self.retrieval_config.stage3,
                embedding_provider=self._get_stage3_embedding_provider(),
                sidecar_store=self._get_sidecar_store(),
                user_id=user_id,
            )
            if stage3_result.profile.candidates:
                profile_rerank = rerank_profile_candidates(
                    candidates=stage3_result.profile.candidates,
                    user_message=user_message,
                    destination=dual_plan.destination,
                    domains=dual_plan.domains,
                    keywords=dual_plan.keywords,
                    config=self.retrieval_config.reranker.profile,
                )
            if stage3_result.episode.candidates:
                episode_rerank = rerank_episode_candidates(
                    candidates=stage3_result.episode.candidates,
                    user_message=user_message,
                    destination=dual_plan.destination,
                    domains=dual_plan.domains,
                    keywords=dual_plan.keywords,
                    config=self.retrieval_config.reranker.episode,
                )
            profile_candidates = self._candidates_for_ids(
                stage3_result.profile.candidates,
                profile_rerank.selected_item_ids,
            )
            episode_candidates = self._candidates_for_ids(
                stage3_result.episode.candidates,
                episode_rerank.selected_item_ids,
            )

        raw_profile_candidates = (
            stage3_result.profile.candidates if stage3_result is not None else []
        )
        raw_episode_candidates = (
            stage3_result.episode.candidates if stage3_result is not None else []
        )
        recall_candidates = [*raw_profile_candidates, *raw_episode_candidates]
        selected_candidates = [*profile_candidates, *episode_candidates]
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
        if stage3_result is not None:
            telemetry.stage3_profile = dict(stage3_result.profile.telemetry)
            telemetry.stage3_episode = dict(stage3_result.episode.telemetry)
            if dual_plan is not None and dual_plan.need_profile and not dual_plan.need_episode:
                telemetry.stage3 = dict(stage3_result.profile.telemetry)
            elif dual_plan is not None and dual_plan.need_episode and not dual_plan.need_profile:
                telemetry.stage3 = dict(stage3_result.episode.telemetry)
            else:
                telemetry.stage3 = {
                    "profile": dict(stage3_result.profile.telemetry),
                    "episode": dict(stage3_result.episode.telemetry),
                }
        telemetry.profile_reranker_selected_ids = list(
            profile_rerank.selected_item_ids
        )
        telemetry.episode_reranker_selected_ids = list(
            episode_rerank.selected_item_ids
        )
        telemetry.profile_reranker_final_reason = profile_rerank.final_reason
        telemetry.episode_reranker_final_reason = episode_rerank.final_reason
        telemetry.profile_reranker_per_item_scores = self._serialize_reranker_scores(
            profile_rerank.per_item_scores
        )
        telemetry.episode_reranker_per_item_scores = self._serialize_reranker_scores(
            episode_rerank.per_item_scores
        )
        telemetry.reranker_selected_ids = [
            *telemetry.profile_reranker_selected_ids,
            *telemetry.episode_reranker_selected_ids,
        ]
        telemetry.reranker_final_reason = (
            (
                f"dual rerank selected {len(telemetry.profile_reranker_selected_ids)} "
                f"profile, {len(telemetry.episode_reranker_selected_ids)} episode"
            )
            if recall_attempted
            else ""
        )
        telemetry.reranker_fallback = "none"
        telemetry.reranker_per_item_reason = {
            **profile_rerank.per_item_reason,
            **episode_rerank.per_item_reason,
        }
        telemetry.reranker_per_item_scores = {
            **telemetry.profile_reranker_per_item_scores,
            **telemetry.episode_reranker_per_item_scores,
        }
        telemetry.reranker_intent_label = "dual" if recall_attempted else ""
        telemetry.reranker_selection_metrics = selection_metrics_placeholder()
        if recall_attempted and dual_plan is not None:
            telemetry.dual_recall_plan = self._dual_plan_to_dict(dual_plan)
            telemetry.query_plan = {
                "buckets": list(dual_plan.profile_buckets),
                "profile_buckets": list(dual_plan.profile_buckets),
                "domains": list(dual_plan.domains),
                "destination": dual_plan.destination,
                "top_k": dual_plan.top_k,
                "need_profile": dual_plan.need_profile,
                "need_episode": dual_plan.need_episode,
            }
            telemetry.query_plan_source = effective_query_plan_source
            telemetry.query_plan_fallback = (
                effective_query_plan_fallback
                if effective_query_plan_fallback != "none"
                else dual_plan.fallback_used
            )
        context = format_v3_memory_context(
            working_items=working_items,
            profile_candidates=profile_candidates,
            episode_candidates=episode_candidates,
        )
        return context, telemetry

    def _build_dual_plan(
        self,
        active_plan: RecallRetrievalPlan | DualRecallPlan | None,
        user_message: str,
        short_circuit: str,
        stage0_signals: dict[str, list[str] | tuple[str, ...]] | None,
    ) -> DualRecallPlan | None:
        if isinstance(active_plan, DualRecallPlan):
            return active_plan
        if isinstance(active_plan, RecallRetrievalPlan):
            if active_plan.fallback_used == "no_historical_recall_cue":
                return None
            return dual_plan_from_retrieval_plan(active_plan)
        if not user_message:
            return None
        legacy = heuristic_retrieval_plan_from_message(
            user_message,
            stage0_decision=short_circuit,
            stage0_signals=stage0_signals,
        )
        if legacy.fallback_used == "no_historical_recall_cue":
            return None
        return dual_plan_from_retrieval_plan(legacy)

    def _candidates_for_ids(
        self,
        candidates: list[RecallCandidate],
        selected_item_ids: list[str],
    ) -> list[RecallCandidate]:
        by_id = {candidate.item_id: candidate for candidate in candidates}
        return [
            by_id[item_id]
            for item_id in selected_item_ids
            if item_id in by_id
        ]

    def _serialize_reranker_scores(
        self,
        scores: dict[str, object],
    ) -> dict[str, dict[str, float | str | None]]:
        serialized: dict[str, dict[str, float | str | None]] = {}
        for item_id, detail in scores.items():
            if isinstance(detail, dict):
                serialized[item_id] = dict(detail)
            elif is_dataclass(detail):
                serialized[item_id] = asdict(detail)
        return serialized

    def _dual_plan_to_dict(self, plan: DualRecallPlan) -> dict[str, object]:
        return {
            "need_profile": plan.need_profile,
            "need_episode": plan.need_episode,
            "profile_buckets": list(plan.profile_buckets),
            "domains": list(plan.domains),
            "destination": plan.destination,
            "keywords": list(plan.keywords),
            "top_k": plan.top_k,
            "reason": plan.reason,
            "fallback_used": plan.fallback_used,
        }

    def _active_working_memory_items(
        self, items: list[WorkingMemoryItem]
    ) -> list[WorkingMemoryItem]:
        active_items = [item for item in items if item.status == "active"]
        return active_items[:_WORKING_MEMORY_LIMIT]

    def _filter_slices_by_normalized_destination(
        self,
        destination: str,
        slices: list[EpisodeSlice],
    ) -> list[EpisodeSlice]:
        if (
            not destination
            or not self.retrieval_config.stage3.destination_normalization_enabled
        ):
            return slices

        matched_slices: list[EpisodeSlice] = []
        for slice_ in slices:
            candidate_destination = str(slice_.entities.get("destination", ""))
            if match_destination(destination, candidate_destination).match_type != "none":
                matched_slices.append(slice_)
        return matched_slices

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
