# backend/memory/manager.py
from __future__ import annotations

import json
from pathlib import Path

from memory.formatter import MemoryRecallTelemetry, format_v3_memory_context
from memory.retrieval_candidates import RecallCandidate
from memory.models import MemoryItem, Rejection, UserMemory
from memory.recall_query import RecallRetrievalPlan
from memory.recall_query_adapter import plan_to_legacy_recall_query
from memory.recall_reranker import RecallRerankResult, choose_reranker_path
from memory.store import FileMemoryStore
from memory.symbolic_recall import (
    build_recall_query,
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
) -> tuple[list[RecallCandidate], RecallRerankResult]:
    del user_message, plan, retrieval_plan

    if not candidates:
        return [], RecallRerankResult(
            selected_item_ids=[],
            final_reason="",
            per_item_reason={},
            fallback_used="none",
        )

    path = choose_reranker_path(candidates)
    selected_candidates = list(path.selected_candidates)
    selected_item_ids = [candidate.item_id for candidate in selected_candidates]
    per_item_reason = {
        candidate.item_id: "selected from symbolic recall candidates"
        for candidate in selected_candidates
    }
    final_reason = (
        "candidate set is small enough to skip reranker"
        if path.fallback_used == "skipped_small_candidate_set"
        else "fallback_top_n_from_symbolic_recall"
    )
    return selected_candidates, RecallRerankResult(
        selected_item_ids=selected_item_ids,
        final_reason=final_reason,
        per_item_reason=per_item_reason,
        fallback_used=path.fallback_used,
    )


class MemoryManager:
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.store = FileMemoryStore(data_dir)
        self.v3_store = FileMemoryV3Store(data_dir)

    def _user_dir(self, user_id: str) -> Path:
        return self.data_dir / "users" / user_id

    async def save(self, memory: UserMemory) -> None:
        user_dir = self._user_dir(memory.user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / "memory.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema_version") == 2:
                data["legacy"] = memory.to_dict()
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
                return
        path.write_text(json.dumps(memory.to_dict(), ensure_ascii=False, indent=2))

    async def load(self, user_id: str) -> UserMemory:
        path = self._user_dir(user_id) / "memory.json"
        if not path.exists():
            return UserMemory(user_id=user_id)
        data = json.loads(path.read_text())
        if data.get("schema_version") == 2:
            legacy = data.get("legacy") or {}
            if legacy:
                return UserMemory.from_dict(legacy)
            return self._legacy_memory_from_items(user_id, data.get("items", []))
        return UserMemory.from_dict(data)

    def _legacy_memory_from_items(
        self, user_id: str, raw_items: list[dict]
    ) -> UserMemory:
        memory = UserMemory(user_id=user_id)
        for raw_item in raw_items:
            try:
                item = MemoryItem.from_dict(raw_item)
            except (KeyError, TypeError, ValueError):
                continue
            if item.status != "active":
                continue
            if item.type == "rejection":
                memory.rejections.append(
                    Rejection(
                        item=str(item.value),
                        reason=str(item.attributes.get("reason", "")),
                        permanent=item.scope == "global",
                        context=str(item.attributes.get("context", "")),
                    )
                )
                continue
            if item.type == "preference":
                memory.explicit_preferences[item.key] = item.value
        return memory

    def generate_summary(self, memory: UserMemory) -> str:
        parts: list[str] = []

        if memory.explicit_preferences:
            prefs = ", ".join(
                f"{k}: {v}" for k, v in memory.explicit_preferences.items()
            )
            parts.append(f"偏好：{prefs}")

        if memory.trip_history:
            trips = "; ".join(
                f"{t.destination}({t.dates}, 满意度{t.satisfaction}/5)"
                if t.satisfaction
                else f"{t.destination}({t.dates})"
                for t in memory.trip_history
            )
            parts.append(f"出行历史：{trips}")

        permanent_rejections = [r for r in memory.rejections if r.permanent]
        if permanent_rejections:
            rejects = ", ".join(f"{r.item}({r.reason})" for r in permanent_rejections)
            parts.append(f"永久排除：{rejects}")

        return "\n".join(parts) if parts else "暂无用户画像"

    async def generate_context(
        self,
        user_id: str,
        plan: TravelPlanState,
        user_message: str = "",
        recall_gate: bool | None = None,
        short_circuit: str = "undecided",
        retrieval_plan: RecallRetrievalPlan | None = None,
    ) -> tuple[str, MemoryRecallTelemetry]:
        profile = await self.v3_store.load_profile(user_id)
        working_memory = await self.v3_store.load_working_memory(
            user_id,
            plan.session_id,
            plan.trip_id,
        )
        working_items = self._active_working_memory_items(working_memory.items)

        recall_candidates: list[RecallCandidate] = []
        legacy_recall_query = build_recall_query(user_message) if user_message else None
        profile_recall_query = None
        slice_recall_query = None
        should_run_query_recall = False
        final_recall_decision = "no_recall_applied"
        if recall_gate is None:
            should_run_query_recall = user_message and (
                should_trigger_memory_recall(user_message)
                or (legacy_recall_query.needs_memory if legacy_recall_query else False)
            )
            if should_run_query_recall:
                profile_recall_query = (
                    plan_to_legacy_recall_query(retrieval_plan)
                    if retrieval_plan is not None
                    else legacy_recall_query
                )
                slice_recall_query = legacy_recall_query
            final_recall_decision = (
                "query_recall_enabled"
                if should_run_query_recall
                else "no_recall_applied"
            )
        elif recall_gate:
            should_run_query_recall = True
            profile_recall_query = (
                plan_to_legacy_recall_query(retrieval_plan)
                if retrieval_plan is not None
                else legacy_recall_query
            )
            slice_recall_query = legacy_recall_query
            final_recall_decision = "query_recall_enabled"

        if should_run_query_recall and profile_recall_query is not None:
            query_profile_limit = (
                retrieval_plan.top_k if retrieval_plan is not None else _QUERY_PROFILE_LIMIT
            )
            if profile_recall_query.include_profile:
                recall_candidates.extend(
                    rank_profile_items(profile_recall_query, profile)[:query_profile_limit]
                )
            if slice_recall_query is not None and slice_recall_query.include_slices:
                candidate_slices = await self.v3_store.list_episode_slices(
                    user_id,
                    destination=slice_recall_query.entities.get("destination"),
                )
                recall_candidates.extend(
                    rank_episode_slices(slice_recall_query, candidate_slices)[
                        :_QUERY_SLICE_LIMIT
                    ]
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
            )

        telemetry = self._build_v3_telemetry(
            working_items,
            selected_candidates,
        )
        telemetry.stage0_decision = short_circuit
        telemetry.gate_needs_recall = recall_gate
        telemetry.final_recall_decision = final_recall_decision
        telemetry.candidate_count = len(recall_candidates)
        telemetry.reranker_selected_ids = list(rerank_result.selected_item_ids)
        telemetry.reranker_final_reason = rerank_result.final_reason
        telemetry.reranker_fallback = rerank_result.fallback_used
        if retrieval_plan is not None:
            telemetry.query_plan = {
                "buckets": list(retrieval_plan.buckets),
                "domains": list(retrieval_plan.domains),
                "strictness": retrieval_plan.strictness,
                "top_k": retrieval_plan.top_k,
            }
            telemetry.query_plan_fallback = retrieval_plan.fallback_used
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
