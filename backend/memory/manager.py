# backend/memory/manager.py
from __future__ import annotations

import json
from pathlib import Path

from memory.models import UserMemory


class MemoryManager:
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)

    def _user_dir(self, user_id: str) -> Path:
        return self.data_dir / "users" / user_id

    async def save(self, memory: UserMemory) -> None:
        user_dir = self._user_dir(memory.user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / "memory.json"
        path.write_text(json.dumps(memory.to_dict(), ensure_ascii=False, indent=2))

    async def load(self, user_id: str) -> UserMemory:
        path = self._user_dir(user_id) / "memory.json"
        if not path.exists():
            return UserMemory(user_id=user_id)
        data = json.loads(path.read_text())
        return UserMemory.from_dict(data)

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
