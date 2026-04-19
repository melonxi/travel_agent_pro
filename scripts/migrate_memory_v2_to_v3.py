#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from memory.episode_slices import build_episode_slices
from memory.models import MemoryEvent, MemoryItem, TripEpisode
from memory.v3_models import MemoryProfileItem, UserMemoryProfile, generate_profile_item_id
from memory.v3_store import FileMemoryV3Store

PROFILE_BUCKETS = ("constraints", "rejections", "stable_preferences")
SOURCE_FILENAMES = ("memory.json", "memory_events.jsonl", "trip_episodes.jsonl")


def migrate_user(data_dir: Path, user_id: str, *, dry_run: bool = False) -> dict[str, int | bool]:
    """Migrate one user's v2 memory files into the v3 memory/ directory.

    Return counters:
    {
        "would_write": bool,
        "profile_items": int,
        "ignored_trip_items": int,
        "events": int,
        "episodes": int,
        "slices": int,
    }
    """

    return asyncio.run(_migrate_user_async(Path(data_dir), user_id, dry_run=dry_run))


async def _migrate_user_async(
    data_dir: Path, user_id: str, *, dry_run: bool = False
) -> dict[str, int | bool]:
    user_dir = data_dir / "users" / user_id
    memory_dir = user_dir / "memory"
    source_paths = {name: user_dir / name for name in SOURCE_FILENAMES}
    source_exists = any(path.exists() for path in source_paths.values())
    result: dict[str, int | bool] = {
        "would_write": source_exists,
        "profile_items": 0,
        "ignored_trip_items": 0,
        "events": 0,
        "episodes": 0,
        "slices": 0,
    }
    if not source_exists:
        return result

    store = FileMemoryV3Store(data_dir)
    profile = await store.load_profile(user_id)
    profile_changed = False

    profile_index = {
        bucket: {item.id: item for item in getattr(profile, bucket)}
        for bucket in PROFILE_BUCKETS
    }

    ignored_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    episode_rows: list[dict[str, Any]] = []
    slice_rows: list[dict[str, Any]] = []

    for raw_item in _load_memory_rows(source_paths["memory.json"]):
        item = MemoryItem.from_dict(raw_item)
        bucket = _bucket_for_memory_item(item)
        if bucket is None:
            ignored_rows.append(item.to_dict())
            if item.scope == "trip":
                result["ignored_trip_items"] = int(result["ignored_trip_items"]) + 1
            continue

        profile_item = _memory_item_to_profile_item(item, bucket)
        existing = profile_index[bucket].get(profile_item.id)
        if existing is None or existing.to_dict() != profile_item.to_dict():
            _upsert_profile_bucket_item(profile, bucket, profile_item)
            profile_index[bucket][profile_item.id] = profile_item
            profile_changed = True
            result["profile_items"] = int(result["profile_items"]) + 1

    for raw_event in _load_jsonl_rows(source_paths["memory_events.jsonl"]):
        event = MemoryEvent.from_dict(raw_event)
        event_rows.append(event.to_dict())

    for raw_episode in _load_jsonl_rows(source_paths["trip_episodes.jsonl"]):
        episode = TripEpisode.from_dict(raw_episode)
        episode_rows.append(episode.to_dict())
        created_at = episode.created_at or _utc_now_iso()
        slices = build_episode_slices(episode, now=created_at)
        slice_rows.extend(slice_.to_dict() for slice_ in slices)

    result["events"] = len(event_rows)
    result["episodes"] = len(episode_rows)
    result["slices"] = len(slice_rows)

    if dry_run:
        return result

    if profile_changed:
        await store.save_profile(profile)

    result["events"] = _merge_jsonl_rows(memory_dir / "events.jsonl", event_rows)
    result["episodes"] = _merge_jsonl_rows(memory_dir / "episodes.jsonl", episode_rows)
    result["slices"] = _merge_jsonl_rows(memory_dir / "episode_slices.jsonl", slice_rows)
    _merge_jsonl_rows(memory_dir / "legacy_ignored.jsonl", ignored_rows)

    _move_source_files_to_legacy(user_dir, source_paths)
    return result


def _bucket_for_memory_item(item: MemoryItem) -> str | None:
    if item.scope != "global":
        return None
    mapping = {
        "constraint": "constraints",
        "rejection": "rejections",
        "preference": "stable_preferences",
    }
    return mapping.get(item.type)


def _memory_item_to_profile_item(item: MemoryItem, bucket: str) -> MemoryProfileItem:
    applicability = {
        "constraints": "适用于所有旅行，除非用户明确临时允许。",
        "rejections": "适用于所有旅行，除非用户明确临时允许。",
        "stable_preferences": "适用于所有旅行，除非用户明确临时调整。",
    }[bucket]
    stability = "explicit_declared" if item.source.kind in {"message", "migration"} else "repeated_confirmed"
    profile_item = MemoryProfileItem(
        id="",
        domain=item.domain,
        key=item.key,
        value=item.value,
        polarity=item.polarity or _default_polarity(bucket),
        stability=stability,
        confidence=item.confidence,
        status=item.status,
        context={
            key: value
            for key, value in {
                "source_scope": item.scope,
                "source_type": item.type,
                "source_destination": item.destination,
                "source_trip_id": item.trip_id,
            }.items()
            if value is not None and value != ""
        },
        applicability=applicability,
        recall_hints={
            "domains": [item.domain] if item.domain else [],
            "keywords": [value for value in [item.key, _normalize_hint(item.value)] if value],
            "priority": "high" if bucket != "stable_preferences" else "medium",
        },
        source_refs=[_source_ref(item.source)],
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
    profile_item.id = generate_profile_item_id(bucket, profile_item)
    return profile_item


def _default_polarity(bucket: str) -> str:
    if bucket == "rejections":
        return "avoid"
    if bucket == "constraints":
        return "must"
    return "like"


def _source_ref(source: Any) -> dict[str, Any]:
    payload = {
        "kind": source.kind,
        "session_id": source.session_id,
        "message_id": source.message_id,
        "tool_call_id": source.tool_call_id,
        "quote": source.quote,
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _upsert_profile_bucket_item(profile: UserMemoryProfile, bucket: str, item: MemoryProfileItem) -> None:
    items = getattr(profile, bucket)
    for index, existing in enumerate(items):
        if existing.id == item.id:
            items[index] = item
            break
    else:
        items.append(item)


def _load_memory_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        rows = data.get("items", [])
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def _load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
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


def _merge_jsonl_rows(path: Path, new_rows: list[dict[str, Any]], *, key: str = "id") -> int:
    existing_rows = _load_jsonl_rows(path)
    seen = {row.get(key) for row in existing_rows if row.get(key) is not None}
    appended = 0
    for row in new_rows:
        marker = row.get(key)
        if marker is not None and marker in seen:
            continue
        if marker is not None:
            seen.add(marker)
        existing_rows.append(row)
        appended += 1
    if appended:
        _write_jsonl(path, existing_rows)
    return appended


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp")
    payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    if rows:
        payload += "\n"
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


def _move_source_files_to_legacy(user_dir: Path, source_paths: dict[str, Path]) -> None:
    legacy_dir = user_dir / "legacy_memory_v2"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    for path in source_paths.values():
        if path.exists():
            path.replace(legacy_dir / path.name)


def _normalize_hint(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return "；".join(f"{key}:{_normalize_hint(value[key])}" for key in sorted(value))
    if isinstance(value, list):
        parts = [_normalize_hint(item) for item in value]
        return "、".join(part for part in parts if part)
    return str(value).strip()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate one user's v2 memory files into v3")
    parser.add_argument("--data-dir", required=True, help="root data directory, e.g. backend/data")
    parser.add_argument("--user-id", required=True, help="user ID to migrate")
    parser.add_argument("--dry-run", action="store_true", help="compute migration without writing")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = migrate_user(Path(args.data_dir), args.user_id, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
