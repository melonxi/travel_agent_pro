# backend/memory/manager.py
from __future__ import annotations

import json
from pathlib import Path

from memory.formatter import RetrievedMemory, format_memory_context
from memory.models import MemoryItem, Rejection, UserMemory
from memory.retriever import MemoryRetriever
from memory.store import FileMemoryStore
from state.models import TravelPlanState


class MemoryManager:
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.store = FileMemoryStore(data_dir)
        self.retriever = MemoryRetriever()

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
        self, user_id: str, plan: TravelPlanState
    ) -> tuple[str, list[str]]:
        items = await self.store.list_items(user_id)
        retrieved = RetrievedMemory(
            core=self.retriever.retrieve_core_profile(items),
            trip=self.retriever.retrieve_trip_memory(items, plan),
            phase=self.retriever.retrieve_phase_relevant(items, plan, plan.phase),
        )
        item_ids = [it.id for it in retrieved.core + retrieved.trip + retrieved.phase]
        return format_memory_context(retrieved), item_ids
