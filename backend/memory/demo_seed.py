from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from memory.models import MemoryItem, MemorySource, TripEpisode, generate_memory_id
from memory.store import FileMemoryStore


@dataclass
class SeedSummary:
    user_id: str
    items_seeded: int
    episodes_seeded: int


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _episode_id(user_id: str, destination: str, dates: str) -> str:
    raw = f"{user_id}:{destination}:{dates}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"demo-episode-{digest}"


async def seed_demo_memory(*, seed_file: Path, data_dir: Path) -> SeedSummary:
    payload = json.loads(seed_file.read_text(encoding="utf-8"))
    user_id = str(payload["user_id"])
    events = payload.get("events", [])
    store = FileMemoryStore(data_dir)

    existing_items = {item.id for item in await store.list_items(user_id)}
    existing_episodes = {episode.id for episode in await store.list_episodes(user_id)}

    items_seeded = 0
    episodes_seeded = 0

    for event in events:
        event_type = str(event.get("event_type", ""))
        object_payload = event.get("object_payload", {})
        reason_text = str(event.get("reason_text", ""))
        timestamp = _now_iso()

        if event_type == "preference_learned":
            domain = str(object_payload["domain"])
            key = str(object_payload["key"])
            item = MemoryItem(
                id=generate_memory_id(
                    user_id=user_id,
                    type="preference",
                    domain=domain,
                    key=key,
                    scope="global",
                ),
                user_id=user_id,
                type="preference",
                domain=domain,
                key=key,
                value=object_payload.get("value"),
                scope="global",
                polarity="neutral",
                confidence=1.0,
                status="active",
                source=MemorySource(kind="seed", session_id=""),
                created_at=timestamp,
                updated_at=timestamp,
                attributes={"reason": reason_text},
            )
            await store.upsert_item(item)
            if item.id not in existing_items:
                existing_items.add(item.id)
                items_seeded += 1
            continue

        if event_type == "trip_completed":
            destination = str(object_payload["destination"])
            dates = str(object_payload["date"])
            episode = TripEpisode(
                id=_episode_id(user_id, destination, dates),
                user_id=user_id,
                session_id="seeded-demo-session",
                trip_id=None,
                destination=destination,
                dates=dates,
                travelers=None,
                budget=None,
                selected_skeleton=None,
                final_plan_summary=str(object_payload.get("highlight", destination)),
                accepted_items=[],
                rejected_items=[],
                lessons=[str(object_payload.get("lesson", ""))] if object_payload.get("lesson") else [],
                satisfaction=object_payload.get("rating"),
                created_at=timestamp,
            )
            await store.append_episode(episode)
            if episode.id not in existing_episodes:
                existing_episodes.add(episode.id)
                episodes_seeded += 1

    return SeedSummary(
        user_id=user_id,
        items_seeded=items_seeded,
        episodes_seeded=episodes_seeded,
    )


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Seed demo memory into backend data files.")
    parser.add_argument("--seed-file", required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()

    summary = await seed_demo_memory(
        seed_file=Path(args.seed_file),
        data_dir=Path(args.data_dir),
    )
    print(
        json.dumps(
            {
                "user_id": summary.user_id,
                "items_seeded": summary.items_seeded,
                "episodes_seeded": summary.episodes_seeded,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
