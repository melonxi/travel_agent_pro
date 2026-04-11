from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from memory.models import (
    MemoryEvent,
    MemoryItem,
    MemorySource,
    Rejection,
    TripEpisode,
    UserMemory,
    generate_memory_id,
)


class FileMemoryStore:
    def __init__(self, data_dir: str | Path = "./data"):
        self.data_dir = Path(data_dir)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _user_dir(self, user_id: str) -> Path:
        return self.data_dir / "users" / user_id

    def _memory_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "memory.json"

    async def load_envelope(self, user_id: str) -> dict[str, Any]:
        async with self._locks[user_id]:
            return self._load_envelope_unlocked(user_id)

    def _load_envelope_unlocked(self, user_id: str) -> dict[str, Any]:
        path = self._memory_path(user_id)
        if not path.exists():
            return {"schema_version": 2, "user_id": user_id, "items": [], "legacy": {}}

        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") == 2:
            data.setdefault("items", [])
            data.setdefault("legacy", {})
            data.setdefault("user_id", user_id)
            data.setdefault("schema_version", 2)
            return data

        legacy = UserMemory.from_dict(data)
        items = self._migrate_legacy(legacy)
        return {
            "schema_version": 2,
            "user_id": user_id,
            "items": [item.to_dict() for item in items],
            "legacy": data,
        }

    def _migrate_legacy(self, memory: UserMemory) -> list[MemoryItem]:
        items: list[MemoryItem] = []
        timestamp = "1970-01-01T00:00:00"

        for key, value in memory.explicit_preferences.items():
            items.append(
                MemoryItem(
                    id=generate_memory_id(
                        user_id=memory.user_id,
                        type="preference",
                        domain="general",
                        key=str(key),
                        scope="global",
                    ),
                    user_id=memory.user_id,
                    type="preference",
                    domain="general",
                    key=str(key),
                    value=value,
                    scope="global",
                    polarity="neutral",
                    confidence=0.8,
                    status="active",
                    source=MemorySource(kind="migration", session_id=""),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )

        for key, value in memory.implicit_preferences.items():
            items.append(
                MemoryItem(
                    id=generate_memory_id(
                        user_id=memory.user_id,
                        type="preference",
                        domain="general",
                        key=str(key),
                        scope="global",
                    ),
                    user_id=memory.user_id,
                    type="preference",
                    domain="general",
                    key=str(key),
                    value=value,
                    scope="global",
                    polarity="neutral",
                    confidence=0.6,
                    status="active",
                    source=MemorySource(kind="migration", session_id=""),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )

        for rejection in memory.rejections:
            items.append(
                MemoryItem(
                    id=generate_memory_id(
                        user_id=memory.user_id,
                        type="rejection",
                        domain="general",
                        key="avoid",
                        scope="global",
                        value=rejection.item,
                    ),
                    user_id=memory.user_id,
                    type="rejection",
                    domain="general",
                    key="avoid",
                    value=rejection.item,
                    scope="global",
                    polarity="avoid",
                    confidence=0.9 if rejection.permanent else 0.6,
                    status="active" if rejection.permanent else "pending",
                    source=MemorySource(kind="migration", session_id=""),
                    created_at=timestamp,
                    updated_at=timestamp,
                    attributes={"reason": rejection.reason, "context": rejection.context},
                )
            )

        return items

    def _write_envelope_unlocked(self, user_id: str, envelope: dict[str, Any]) -> None:
        user_dir = self._user_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = self._memory_path(user_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)

    async def list_items(self, user_id: str, *, status: str | None = None) -> list[MemoryItem]:
        async with self._locks[user_id]:
            envelope = self._load_envelope_unlocked(user_id)
            items = [MemoryItem.from_dict(row) for row in envelope.get("items", [])]
            if status is not None:
                items = [item for item in items if item.status == status]
            return items

    async def upsert_item(self, item: MemoryItem) -> None:
        async with self._locks[item.user_id]:
            envelope = self._load_envelope_unlocked(item.user_id)
            rows = [MemoryItem.from_dict(row) for row in envelope.get("items", [])]
            replaced = False
            for index, existing in enumerate(rows):
                if existing.id == item.id:
                    rows[index] = item
                    replaced = True
                    break
            if not replaced:
                rows.append(item)
            envelope["items"] = [row.to_dict() for row in rows]
            envelope["schema_version"] = 2
            envelope["user_id"] = item.user_id
            self._write_envelope_unlocked(item.user_id, envelope)

    async def update_status(self, user_id: str, item_id: str, status: str) -> None:
        async with self._locks[user_id]:
            envelope = self._load_envelope_unlocked(user_id)
            rows = [MemoryItem.from_dict(row) for row in envelope.get("items", [])]
            for item in rows:
                if item.id == item_id:
                    item.status = status
            envelope["items"] = [row.to_dict() for row in rows]
            self._write_envelope_unlocked(user_id, envelope)

    async def append_event(self, event: MemoryEvent) -> None:
        async with self._locks[event.user_id]:
            user_dir = self._user_dir(event.user_id)
            user_dir.mkdir(parents=True, exist_ok=True)
            with (user_dir / "memory_events.jsonl").open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    async def append_episode(self, episode: TripEpisode) -> None:
        async with self._locks[episode.user_id]:
            user_dir = self._user_dir(episode.user_id)
            user_dir.mkdir(parents=True, exist_ok=True)
            with (user_dir / "trip_episodes.jsonl").open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(episode.to_dict(), ensure_ascii=False) + "\n")

    async def list_episodes(
        self, user_id: str, *, destination: str | None = None
    ) -> list[TripEpisode]:
        async with self._locks[user_id]:
            path = self._user_dir(user_id) / "trip_episodes.jsonl"
            if not path.exists():
                return []
            episodes = [
                TripEpisode.from_dict(json.loads(line))
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if destination is not None:
                episodes = [
                    episode for episode in episodes if episode.destination == destination
                ]
            return episodes
