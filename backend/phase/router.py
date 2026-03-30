# backend/phase/router.py
from __future__ import annotations

from opentelemetry import trace

from phase.prompts import PHASE_CONTROL_MODE, PHASE_PROMPTS
from state.models import BacktrackEvent, TravelPlanState
from telemetry.attributes import EVENT_PHASE_PLAN_SNAPSHOT, PHASE_FROM, PHASE_TO


class PhaseRouter:
    def infer_phase(self, plan: TravelPlanState) -> int:
        if not plan.destination:
            if plan.preferences:
                return 2
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
        """Execute backtrack: record event, clear downstream, switch phase."""
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
