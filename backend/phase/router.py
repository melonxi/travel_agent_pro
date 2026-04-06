# backend/phase/router.py
from __future__ import annotations

from opentelemetry import trace

from phase.backtrack import BacktrackService
from phase.prompts import PHASE_CONTROL_MODE, PHASE_PROMPTS
from state.models import TravelPlanState
from telemetry.attributes import EVENT_PHASE_PLAN_SNAPSHOT, PHASE_FROM, PHASE_TO


class PhaseRouter:
    def __init__(self) -> None:
        self._backtrack_service = BacktrackService()

    def infer_phase(self, plan: TravelPlanState) -> int:
        if not plan.destination:
            return 1
        if not plan.dates:
            return 3
        if not plan.accommodation:
            return 4
        if len(plan.daily_plans) < plan.dates.total_days:
            return 5
        return 7

    def get_prompt(self, phase: int) -> str:
        return PHASE_PROMPTS.get(phase, PHASE_PROMPTS[1])

    def get_control_mode(self, phase: int) -> str:
        return PHASE_CONTROL_MODE.get(phase, "conversational")

    def check_and_apply_transition(self, plan: TravelPlanState) -> bool:
        """Check if plan_state warrants a phase change. Returns True if phase changed."""
        inferred = self.infer_phase(plan)
        if inferred != plan.phase:
            tracer = trace.get_tracer("travel-agent-pro")
            with tracer.start_as_current_span("phase.transition") as span:
                span.set_attribute(PHASE_FROM, plan.phase)
                span.set_attribute(PHASE_TO, inferred)
                span.add_event(
                    EVENT_PHASE_PLAN_SNAPSHOT,
                    {
                        "destination": plan.destination or "",
                        "dates": (
                            f"{plan.dates.start} ~ {plan.dates.end}"
                            if plan.dates
                            else ""
                        ),
                        "daily_plans_count": len(plan.daily_plans),
                    },
                )
                plan.phase = inferred
            return True
        return False

    def prepare_backtrack(
        self,
        plan: TravelPlanState,
        to_phase: int,
        reason: str,
        snapshot_path: str,
    ) -> None:
        """Execute backtrack: delegate to BacktrackService."""
        self._backtrack_service.execute(plan, to_phase, reason, snapshot_path)
