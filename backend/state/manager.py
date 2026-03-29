# backend/state/manager.py
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from state.models import TravelPlanState

_SESSION_ID_RE = re.compile(r"^sess_[a-f0-9]{12}$")


class StateManager:
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)

    def _validate_session_id(self, session_id: str) -> None:
        if not _SESSION_ID_RE.match(session_id):
            raise ValueError(f"Invalid session_id format: {session_id}")

    def _session_dir(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        return self.data_dir / "sessions" / session_id

    async def create_session(self) -> TravelPlanState:
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        # version=0 so that the internal save() call below increments it to 1
        plan = TravelPlanState(session_id=session_id, version=0)
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "snapshots").mkdir(exist_ok=True)
        (session_dir / "tool_results").mkdir(exist_ok=True)
        await self.save(plan)
        return plan

    async def save(self, plan: TravelPlanState) -> None:
        """Persist plan to disk. Mutates plan in-place: increments version and updates last_updated."""
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
        path = snapshot_dir / f"{time.time_ns()}.json"
        path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return str(path)

    async def save_tool_result(
        self, session_id: str, tool_name: str, data: dict
    ) -> str:
        results_dir = self._session_dir(session_id) / "tool_results"
        results_dir.mkdir(parents=True, exist_ok=True)
        path = results_dir / f"{tool_name}-{time.time_ns()}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return str(path)
