from __future__ import annotations

from fastapi import FastAPI, HTTPException


def register_memory_routes(
    app: FastAPI,
    *,
    sessions: dict[str, dict],
    ensure_storage_ready,
    restore_session,
    memory_mgr,
    now_iso,
) -> None:
    @app.get("/api/memory/{user_id}/profile")
    async def get_memory_profile(user_id: str):
        await ensure_storage_ready()
        profile = await memory_mgr.v3_store.load_profile(user_id)
        return profile.to_dict()

    @app.get("/api/memory/{user_id}/episode-slices")
    async def list_memory_episode_slices(user_id: str):
        await ensure_storage_ready()
        slices = await memory_mgr.v3_store.list_episode_slices(user_id)
        return {"slices": [slice_.to_dict() for slice_ in slices]}

    @app.get("/api/memory/{user_id}/sessions/{session_id}/working-memory")
    async def get_session_working_memory(user_id: str, session_id: str):
        await ensure_storage_ready()
        session = sessions.get(session_id)
        if session is None:
            restored = await restore_session(session_id)
            if restored is not None:
                sessions[session_id] = restored
                session = restored
        trip_id: str | None = None
        if session is not None:
            plan = session.get("plan")
            if plan is not None:
                trip_id = getattr(plan, "trip_id", None)
        memory = await memory_mgr.v3_store.load_working_memory(
            user_id, session_id, trip_id
        )
        return memory.to_dict()

    async def _set_v3_profile_item_status(
        user_id: str,
        item_id: str,
        status: str,
    ) -> bool:
        profile = await memory_mgr.v3_store.load_profile(user_id)
        updated = False
        now = now_iso()

        for bucket in (
            "constraints",
            "rejections",
            "stable_preferences",
            "preference_hypotheses",
        ):
            items = getattr(profile, bucket)
            for index, item in enumerate(items):
                if item.id != item_id:
                    continue
                should_remove = status == "obsolete" or (
                    bucket == "preference_hypotheses" and status == "rejected"
                )
                if should_remove:
                    del items[index]
                else:
                    item.status = status
                    item.updated_at = now
                updated = True
                break
            if updated:
                break

        if not updated:
            return False

        await memory_mgr.v3_store.save_profile(profile)
        return True

    @app.post("/api/memory/{user_id}/profile/{item_id}/confirm")
    async def confirm_profile_item(user_id: str, item_id: str):
        await ensure_storage_ready()
        if not await _set_v3_profile_item_status(user_id, item_id, "active"):
            raise HTTPException(status_code=404, detail="Profile item not found")
        return {"item_id": item_id, "status": "active"}

    @app.post("/api/memory/{user_id}/profile/{item_id}/reject")
    async def reject_profile_item(user_id: str, item_id: str):
        await ensure_storage_ready()
        if not await _set_v3_profile_item_status(user_id, item_id, "rejected"):
            raise HTTPException(status_code=404, detail="Profile item not found")
        return {"item_id": item_id, "status": "rejected"}

    @app.get("/api/memory/{user_id}/episodes")
    async def list_memory_episodes(user_id: str):
        await ensure_storage_ready()
        episodes = await memory_mgr.v3_store.list_episodes(user_id)
        return {"episodes": [episode.to_dict() for episode in episodes]}

    @app.delete("/api/memory/{user_id}/profile/{item_id}")
    async def delete_profile_item(user_id: str, item_id: str):
        await ensure_storage_ready()
        if not await _set_v3_profile_item_status(user_id, item_id, "obsolete"):
            raise HTTPException(status_code=404, detail="Profile item not found")
        return {"item_id": item_id, "status": "obsolete"}
