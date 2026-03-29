# backend/state/manager.py
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from state.models import TravelPlanState


class StateManager:
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)

    def _session_dir(self, session_id: str) -> Path:
        return self.data_dir / "sessions" / session_id

    async def create_session(self) -> TravelPlanState:
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        plan = TravelPlanState(session_id=session_id, version=0)
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "snapshots").mkdir(exist_ok=True)
        (session_dir / "tool_results").mkdir(exist_ok=True)
        await self.save(plan)
        return plan

    async def save(self, plan: TravelPlanState) -> None:
        plan.last_updated = datetime.now().isoformat()
        plan.version += 1
        path = self._session_dir(plan.session_id) / "plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))

    async def load(self, session_id: str) -> TravelPlanState:
        path = self._session_dir(session_id) / "plan.json"
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")
        data = json.loads(path.read_text())
        return TravelPlanState.from_dict(data)

    async def save_snapshot(self, plan: TravelPlanState) -> str:
        snapshot_dir = self._session_dir(plan.session_id) / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        path = snapshot_dir / f"{int(time.time())}.json"
        path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return str(path)

    async def save_tool_result(
        self, session_id: str, tool_name: str, data: dict
    ) -> str:
        results_dir = self._session_dir(session_id) / "tool_results"
        results_dir.mkdir(parents=True, exist_ok=True)
        path = results_dir / f"{tool_name}-{int(time.time())}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return str(path)
