from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from memory.episode_slices import build_episode_slices
from memory.v3_models import ArchivedTripEpisode, MemoryProfileItem, generate_profile_item_id
from memory.v3_store import FileMemoryV3Store


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


def _month_dates(label: str) -> dict[str, str]:
    return {
        "start": f"{label}-01",
        "end": f"{label}-31",
        "label": label,
    }


async def seed_demo_memory(
    *,
    seed_file: Path,
    data_dir: Path,
    reset_user: bool = False,
) -> SeedSummary:
    payload = json.loads(seed_file.read_text(encoding="utf-8"))
    user_id = str(payload["user_id"])
    events = payload.get("events", [])
    store = FileMemoryV3Store(data_dir)

    if reset_user:
        user_dir = data_dir / "users" / user_id
        if user_dir.exists():
            shutil.rmtree(user_dir)

    profile = await store.load_profile(user_id)
    existing_profile_ids = {
        item.id
        for bucket in (
            profile.constraints,
            profile.rejections,
            profile.stable_preferences,
            profile.preference_hypotheses,
        )
        for item in bucket
    }
    existing_episode_ids = {episode.id for episode in await store.list_episodes(user_id)}

    items_seeded = 0
    episodes_seeded = 0

    for event in events:
        event_type = str(event.get("event_type", ""))
        object_payload = event.get("object_payload", {})
        reason_text = str(event.get("reason_text", ""))
        timestamp = _now_iso()

        if event_type == "preference_learned":
            item = MemoryProfileItem(
                id="",
                domain=str(object_payload["domain"]),
                key=str(object_payload["key"]),
                value=object_payload.get("value"),
                polarity="prefer",
                stability="stable",
                confidence=1.0,
                status="active",
                context={},
                applicability=reason_text or "适用于后续相似旅行。",
                recall_hints={
                    "domains": [str(object_payload["domain"])],
                    "keywords": [str(object_payload["key"])],
                },
                source_refs=[{"kind": "seed", "quote": reason_text}] if reason_text else [],
                created_at=timestamp,
                updated_at=timestamp,
            )
            item.id = generate_profile_item_id("stable_preferences", item)
            await store.upsert_profile_item(user_id, "stable_preferences", item)
            if item.id not in existing_profile_ids:
                existing_profile_ids.add(item.id)
                items_seeded += 1
            continue

        if event_type == "trip_completed":
            destination = str(object_payload["destination"])
            date_label = str(object_payload["date"])
            episode = ArchivedTripEpisode(
                id=_episode_id(user_id, destination, date_label),
                user_id=user_id,
                session_id="seeded-demo-session",
                trip_id=None,
                destination=destination,
                dates=_month_dates(date_label),
                travelers=None,
                budget=None,
                selected_skeleton={"id": "seeded-history", "name": str(object_payload.get("highlight", destination))},
                selected_transport=None,
                accommodation=None,
                daily_plan_summary=[],
                final_plan_summary=str(object_payload.get("highlight", destination)),
                decision_log=[],
                lesson_log=[
                    {
                        "kind": "pitfall",
                        "content": str(object_payload.get("lesson", "")),
                        "timestamp": timestamp,
                    }
                ]
                if object_payload.get("lesson")
                else [],
                created_at=timestamp,
                completed_at=timestamp,
            )
            await store.append_episode(episode)
            if episode.id not in existing_episode_ids:
                existing_episode_ids.add(episode.id)
                episodes_seeded += 1
            for slice_ in build_episode_slices(episode, now=timestamp):
                await store.append_episode_slice(slice_)

    return SeedSummary(
        user_id=user_id,
        items_seeded=items_seeded,
        episodes_seeded=episodes_seeded,
    )


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Seed demo memory into backend data files.")
    parser.add_argument("--seed-file", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--reset-user", action="store_true")
    args = parser.parse_args()

    summary = await seed_demo_memory(
        seed_file=Path(args.seed_file),
        data_dir=Path(args.data_dir),
        reset_user=args.reset_user,
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
