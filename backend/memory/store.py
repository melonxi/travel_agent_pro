from __future__ import annotations

import asyncio
import json
import os
import uuid
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
        self._lock_registry: dict[str, asyncio.Lock] = {}

    def _lock_for(self, user_id: str) -> asyncio.Lock:
        lock = self._lock_registry.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._lock_registry[user_id] = lock
        return lock

    def _user_dir(self, user_id: str) -> Path:
        return self.data_dir / "users" / user_id

    def _memory_path(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "memory.json"

    async def load_envelope(self, user_id: str) -> dict[str, Any]:
        async with self._lock_for(user_id):
            return await asyncio.to_thread(self._load_envelope_sync, user_id)

    def _load_envelope_sync(self, user_id: str) -> dict[str, Any]:
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
        explicit_items: dict[str, MemoryItem] = {}

        for key, value in memory.explicit_preferences.items():
            explicit_items[str(key)] = MemoryItem(
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
        items.extend(explicit_items.values())

        for key, value in memory.implicit_preferences.items():
            if str(key) in explicit_items:
                continue
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

    def _write_envelope_sync(self, user_id: str, envelope: dict[str, Any]) -> None:
        user_dir = self._user_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = self._memory_path(user_id)
        tmp_path = path.with_name(
            f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        tmp_path.write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)

    async def list_items(self, user_id: str, *, status: str | None = None) -> list[MemoryItem]:
        async with self._lock_for(user_id):
            envelope = await asyncio.to_thread(self._load_envelope_sync, user_id)
            items = [MemoryItem.from_dict(row) for row in envelope.get("items", [])]
            if status is not None:
                items = [item for item in items if item.status == status]
            return items

    async def upsert_item(self, item: MemoryItem) -> None:
        async with self._lock_for(item.user_id):
            envelope = await asyncio.to_thread(self._load_envelope_sync, item.user_id)
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
            await asyncio.to_thread(self._write_envelope_sync, item.user_id, envelope)

    async def update_status(self, user_id: str, item_id: str, status: str) -> bool:
        async with self._lock_for(user_id):
            path = self._memory_path(user_id)
            if not path.exists():
                return False

            envelope = await asyncio.to_thread(self._load_envelope_sync, user_id)
            rows = [MemoryItem.from_dict(row) for row in envelope.get("items", [])]
            changed = False
            for item in rows:
                if item.id == item_id:
                    if item.status != status:
                        item.status = status
                        changed = True
                    break

            if not changed:
                return False

            envelope["items"] = [row.to_dict() for row in rows]
            await asyncio.to_thread(self._write_envelope_sync, user_id, envelope)
            return True

    async def append_event(self, event: MemoryEvent) -> None:
        async with self._lock_for(event.user_id):
            def _write() -> None:
                user_dir = self._user_dir(event.user_id)
                user_dir.mkdir(parents=True, exist_ok=True)
                with (user_dir / "memory_events.jsonl").open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            await asyncio.to_thread(_write)

    async def append_episode(self, episode: TripEpisode) -> None:
        async with self._lock_for(episode.user_id):
            def _write() -> None:
                user_dir = self._user_dir(episode.user_id)
                user_dir.mkdir(parents=True, exist_ok=True)
                path = user_dir / "trip_episodes.jsonl"
                if path.exists():
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        if json.loads(line).get("id") == episode.id:
                            return
                with path.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(episode.to_dict(), ensure_ascii=False) + "\n")
            await asyncio.to_thread(_write)

    async def list_episodes(
        self, user_id: str, *, destination: str | None = None
    ) -> list[TripEpisode]:
        async with self._lock_for(user_id):
            envelope = await asyncio.to_thread(self._load_envelope_sync, user_id)
            path = self._user_dir(user_id) / "trip_episodes.jsonl"
            episodes: list[TripEpisode] = []
            if path.exists():
                episodes.extend(
                    TripEpisode.from_dict(json.loads(line))
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )

            legacy = envelope.get("legacy", {})
            if legacy:
                for trip in legacy.get("trip_history", []):
                    episodes.append(
                        TripEpisode(
                            id=generate_memory_id(
                                user_id=user_id,
                                type="episode",
                                domain="general",
                                key=str(trip.get("destination", "")),
                                scope="trip",
                                trip_id=str(trip.get("dates", "")),
                            ),
                            user_id=user_id,
                            session_id="",
                            trip_id=None,
                            destination=trip.get("destination"),
                            dates=trip.get("dates"),
                            travelers=None,
                            budget=None,
                            selected_skeleton=None,
                            final_plan_summary=str(trip.get("notes", "")),
                            accepted_items=[],
                            rejected_items=[],
                            lessons=[str(trip.get("notes", ""))]
                            if trip.get("notes")
                            else [],
                            satisfaction=trip.get("satisfaction"),
                            created_at="1970-01-01T00:00:00",
                        )
                    )

            if destination is not None:
                episodes = [episode for episode in episodes if episode.destination == destination]
            return episodes
