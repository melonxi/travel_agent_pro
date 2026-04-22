from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from memory.v3_models import (
    ArchivedTripEpisode,
    EpisodeSlice,
    MemoryAuditEvent,
    MemoryProfileItem,
    SessionWorkingMemory,
    UserMemoryProfile,
    WorkingMemoryItem,
)


class FileMemoryV3Store:
    def __init__(self, data_dir: str | Path = "./data"):
        self.data_dir = Path(data_dir)
        self._lock_registry: dict[str, asyncio.Lock] = {}

    def _lock_for(self, user_id: str) -> asyncio.Lock:
        lock = self._lock_registry.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._lock_registry[user_id] = lock
        return lock

    def _user_memory_dir(self, user_id: str) -> Path:
        return self.data_dir / "users" / user_id / "memory"

    def _profile_path(self, user_id: str) -> Path:
        return self._user_memory_dir(user_id) / "profile.json"

    def _working_memory_path(
        self, user_id: str, session_id: str, trip_id: str | None
    ) -> Path:
        trip_key = trip_id or "_none"
        return (
            self._user_memory_dir(user_id)
            / "sessions"
            / session_id
            / "trips"
            / trip_key
            / "working_memory.json"
        )

    def _episodes_path(self, user_id: str) -> Path:
        return self._user_memory_dir(user_id) / "episodes.jsonl"

    def _events_path(self, user_id: str) -> Path:
        return self._user_memory_dir(user_id) / "events.jsonl"

    def _episode_slices_path(self, user_id: str) -> Path:
        return self._user_memory_dir(user_id) / "episode_slices.jsonl"

    def _delete_all_legacy_memory_files_sync(self) -> list[Path]:
        removed: list[Path] = []
        for user_dir in (self.data_dir / "users").glob("*"):
            if not user_dir.is_dir():
                continue
            for filename in ("memory.json", "memory_events.jsonl", "trip_episodes.jsonl"):
                path = user_dir / filename
                if path.exists():
                    path.unlink()
                    removed.append(path)
        return removed

    def _write_json_atomic_sync(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)

    def _read_json_sync(self, path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _read_jsonl_sync(self, path: Path) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    async def load_profile(self, user_id: str) -> UserMemoryProfile:
        async with self._lock_for(user_id):
            return await asyncio.to_thread(self._load_profile_sync, user_id)

    def _load_profile_sync(self, user_id: str) -> UserMemoryProfile:
        path = self._profile_path(user_id)
        if not path.exists():
            return UserMemoryProfile.empty(user_id)
        return UserMemoryProfile.from_dict(self._read_json_sync(path), user_id=user_id)

    async def save_profile(self, profile: UserMemoryProfile) -> None:
        async with self._lock_for(profile.user_id):
            await asyncio.to_thread(self._write_json_atomic_sync, self._profile_path(profile.user_id), profile.to_dict())

    async def upsert_profile_item(self, user_id: str, bucket: str, item: MemoryProfileItem) -> None:
        if bucket not in {
            "constraints",
            "rejections",
            "stable_preferences",
            "preference_hypotheses",
        }:
            raise ValueError(f"unsupported profile bucket: {bucket}")

        async with self._lock_for(user_id):
            profile = await asyncio.to_thread(self._load_profile_sync, user_id)
            bucket_items = getattr(profile, bucket)
            replaced = False
            for index, existing in enumerate(bucket_items):
                if existing.id == item.id:
                    bucket_items[index] = item
                    replaced = True
                    break
            if not replaced:
                bucket_items.append(item)
            await asyncio.to_thread(self._write_json_atomic_sync, self._profile_path(user_id), profile.to_dict())

    async def load_working_memory(
        self, user_id: str, session_id: str, trip_id: str | None
    ) -> SessionWorkingMemory:
        async with self._lock_for(user_id):
            return await asyncio.to_thread(
                self._load_working_memory_sync, user_id, session_id, trip_id
            )

    def _load_working_memory_sync(
        self, user_id: str, session_id: str, trip_id: str | None
    ) -> SessionWorkingMemory:
        path = self._working_memory_path(user_id, session_id, trip_id)
        if not path.exists():
            return SessionWorkingMemory.empty(user_id, session_id, trip_id)
        memory = SessionWorkingMemory.from_dict(self._read_json_sync(path))
        memory.user_id = user_id
        memory.session_id = session_id
        memory.trip_id = trip_id
        return memory

    async def upsert_working_memory_item(
        self, user_id: str, session_id: str, trip_id: str | None, item: WorkingMemoryItem
    ) -> None:
        async with self._lock_for(user_id):
            memory = await asyncio.to_thread(
                self._load_working_memory_for_write_sync, user_id, session_id, trip_id
            )
            memory.user_id = user_id
            memory.session_id = session_id
            memory.trip_id = trip_id
            replaced = False
            for index, existing in enumerate(memory.items):
                if existing.id == item.id:
                    memory.items[index] = item
                    replaced = True
                    break
            if not replaced:
                memory.items.append(item)
            await asyncio.to_thread(
                self._write_json_atomic_sync,
                self._working_memory_path(user_id, session_id, trip_id),
                memory.to_dict(),
            )

    def _load_working_memory_for_write_sync(
        self, user_id: str, session_id: str, trip_id: str | None
    ) -> SessionWorkingMemory:
        path = self._working_memory_path(user_id, session_id, trip_id)
        if not path.exists():
            return SessionWorkingMemory.empty(user_id, session_id, trip_id)
        return SessionWorkingMemory.from_dict(self._read_json_sync(path))

    async def append_episode(self, episode: ArchivedTripEpisode) -> None:
        async with self._lock_for(episode.user_id):
            await asyncio.to_thread(self._append_episode_sync, episode)

    def _append_episode_sync(self, episode: ArchivedTripEpisode) -> None:
        path = self._episodes_path(episode.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            for row in self._read_jsonl_sync(path):
                if row.get("id") == episode.id:
                    return
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(episode.to_dict(), ensure_ascii=False) + "\n")

    async def list_episodes(self, user_id: str) -> list[ArchivedTripEpisode]:
        async with self._lock_for(user_id):
            return await asyncio.to_thread(self._list_episodes_sync, user_id)

    def _list_episodes_sync(self, user_id: str) -> list[ArchivedTripEpisode]:
        return [
            ArchivedTripEpisode.from_dict(row)
            for row in self._read_jsonl_sync(self._episodes_path(user_id))
        ]

    async def append_event(self, event: MemoryAuditEvent) -> None:
        async with self._lock_for(event.user_id):
            await asyncio.to_thread(self._append_event_sync, event)

    def _append_event_sync(self, event: MemoryAuditEvent) -> None:
        path = self._events_path(event.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    async def delete_all_legacy_memory_files(self) -> list[Path]:
        return await asyncio.to_thread(self._delete_all_legacy_memory_files_sync)

    async def append_episode_slice(self, slice_: EpisodeSlice) -> None:
        async with self._lock_for(slice_.user_id):
            await asyncio.to_thread(self._append_episode_slice_sync, slice_)

    def _append_episode_slice_sync(self, slice_: EpisodeSlice) -> None:
        path = self._episode_slices_path(slice_.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            for row in self._read_jsonl_sync(path):
                if row.get("id") == slice_.id:
                    return
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(slice_.to_dict(), ensure_ascii=False) + "\n")

    async def list_episode_slices(
        self, user_id: str, destination: str | None = None
    ) -> list[EpisodeSlice]:
        async with self._lock_for(user_id):
            return await asyncio.to_thread(self._list_episode_slices_sync, user_id, destination)

    def _list_episode_slices_sync(
        self, user_id: str, destination: str | None
    ) -> list[EpisodeSlice]:
        path = self._episode_slices_path(user_id)
        rows = self._read_jsonl_sync(path)
        slices = [EpisodeSlice.from_dict(row) for row in rows]
        if destination is not None:
            slices = [
                slice_
                for slice_ in slices
                if slice_.entities.get("destination") == destination
            ]
        return slices
