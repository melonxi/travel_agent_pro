from __future__ import annotations

from collections.abc import Callable
from typing import Any

from memory.archival import build_archived_trip_episode
from memory.episode_slices import build_episode_slices
from state.models import TravelPlanState


async def append_archived_trip_episode_once(
    *,
    user_id: str,
    session_id: str,
    plan: TravelPlanState,
    memory_mgr: Any,
    now_iso: Callable[[], str],
) -> bool:
    episode = build_archived_trip_episode(
        user_id=user_id,
        session_id=session_id,
        plan=plan,
        now=now_iso(),
    )
    episodes = await memory_mgr.v3_store.list_episodes(user_id)
    if any(existing.id == episode.id for existing in episodes):
        await append_episode_slices(episode, memory_mgr=memory_mgr, now_iso=now_iso)
        return False
    await memory_mgr.v3_store.append_episode(episode)
    await append_episode_slices(episode, memory_mgr=memory_mgr, now_iso=now_iso)
    return True


async def append_episode_slices(
    episode: Any,
    *,
    memory_mgr: Any,
    now_iso: Callable[[], str],
) -> None:
    now = now_iso()
    for slice_ in build_episode_slices(episode, now=now):
        await memory_mgr.v3_store.append_episode_slice(slice_)
