# backend/memory/manager.py
from __future__ import annotations

import json
from pathlib import Path

from memory.formatter import MemoryRecallTelemetry, format_v3_memory_context
from memory.models import MemoryItem, Rejection, UserMemory
from memory.store import FileMemoryStore
from memory.symbolic_recall import (
    build_recall_query,
    rank_episode_slices,
    rank_profile_items,
    should_trigger_memory_recall,
)
from memory.v3_models import EpisodeSlice, MemoryProfileItem, WorkingMemoryItem
from memory.v3_store import FileMemoryV3Store
from state.models import TravelPlanState


_FIXED_PROFILE_LIMIT = 10
_WORKING_MEMORY_LIMIT = 10
_QUERY_PROFILE_LIMIT = 5
_QUERY_SLICE_LIMIT = 5


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
    ) -> tuple[str, MemoryRecallTelemetry]:
        profile = await self.v3_store.load_profile(user_id)
        fixed_profile_items = self._fixed_profile_items(profile)
        working_memory = await self.v3_store.load_working_memory(
            user_id,
            plan.session_id,
            plan.trip_id,
        )
        working_items = self._active_working_memory_items(working_memory.items)

        query_profile_items: list[tuple[str, MemoryProfileItem, str]] = []
        query_slices: list[tuple[EpisodeSlice, str]] = []
        recall_query = build_recall_query(user_message)
        should_run_query_recall = False
        final_recall_decision = "fixed_only"
        if recall_gate is None:
            should_run_query_recall = user_message and (
                should_trigger_memory_recall(user_message) or recall_query.needs_memory
            )
            final_recall_decision = (
                "query_recall_enabled" if should_run_query_recall else "fixed_only"
            )
        elif recall_gate:
            should_run_query_recall = True
            final_recall_decision = "query_recall_enabled"

        if should_run_query_recall:
            query_profile_items = rank_profile_items(recall_query, profile)[
                :_QUERY_PROFILE_LIMIT
            ]
            candidate_slices = await self.v3_store.list_episode_slices(
                user_id,
                destination=recall_query.entities.get("destination"),
            )
            query_slices = rank_episode_slices(recall_query, candidate_slices)[
                :_QUERY_SLICE_LIMIT
            ]

        telemetry = self._build_v3_telemetry(
            fixed_profile_items,
            working_items,
            query_profile_items,
            query_slices,
        )
        telemetry.stage0_decision = short_circuit
        telemetry.gate_needs_recall = recall_gate
        telemetry.final_recall_decision = final_recall_decision
        context = format_v3_memory_context(
            profile_items=fixed_profile_items,
            working_items=working_items,
            query_profile_items=query_profile_items,
            query_slices=query_slices,
        )
        return context, telemetry

    def _fixed_profile_items(
        self, profile
    ) -> list[tuple[str, MemoryProfileItem]]:
        items: list[tuple[str, MemoryProfileItem]] = []
        for bucket in ("constraints", "rejections", "stable_preferences"):
            for item in getattr(profile, bucket, []):
                if item.status != "active":
                    continue
                items.append((bucket, item))
                if len(items) >= _FIXED_PROFILE_LIMIT:
                    return items
        return items

    def _active_working_memory_items(
        self, items: list[WorkingMemoryItem]
    ) -> list[WorkingMemoryItem]:
        active_items = [item for item in items if item.status == "active"]
        return active_items[:_WORKING_MEMORY_LIMIT]

    def _build_v3_telemetry(
        self,
        fixed_profile_items: list[tuple[str, MemoryProfileItem]],
        working_items: list[WorkingMemoryItem],
        query_profile_items: list[tuple[str, MemoryProfileItem, str]],
        query_slices: list[tuple[EpisodeSlice, str]],
    ) -> MemoryRecallTelemetry:
        fixed_profile_ids = self._dedupe_ids(
            [item.id for _, item in fixed_profile_items]
        )
        query_profile_ids = self._dedupe_ids(
            [item.id for _, item, _ in query_profile_items]
        )
        profile_ids = self._dedupe_ids(fixed_profile_ids + query_profile_ids)
        working_memory_ids = self._dedupe_ids([item.id for item in working_items])
        slice_ids = self._dedupe_ids([slice_.id for slice_, _ in query_slices])
        matched_reasons = self._dedupe_values(
            [reason for _, _, reason in query_profile_items]
            + [reason for _, reason in query_slices]
        )
        return MemoryRecallTelemetry(
            sources={
                "profile_fixed": len(fixed_profile_ids),
                "query_profile": len(query_profile_ids),
                "working_memory": len(working_memory_ids),
                "episode_slice": len(slice_ids),
            },
            profile_ids=profile_ids,
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
