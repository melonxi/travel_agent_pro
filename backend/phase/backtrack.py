# backend/phase/backtrack.py
from __future__ import annotations

from state.models import BacktrackEvent, TravelPlanState


class BacktrackService:
    def execute(
        self,
        plan: TravelPlanState,
        to_phase: int,
        reason: str,
        snapshot_path: str,
    ) -> None:
        """Execute backtrack: validate, record event, clear downstream, switch phase."""
        if to_phase >= plan.phase:
            raise ValueError("只能回退到更早的阶段")

        plan.backtrack_history.append(
            BacktrackEvent(
                from_phase=plan.phase,
                to_phase=to_phase,
                reason=reason,
                snapshot_path=snapshot_path,
            )
        )
        plan.clear_downstream(from_phase=to_phase)
        plan.phase = to_phase
