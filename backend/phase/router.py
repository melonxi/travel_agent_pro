# backend/phase/router.py
from __future__ import annotations

from typing import Any

from opentelemetry import trace

from phase.backtrack import BacktrackService
from phase.prompts import PHASE_CONTROL_MODE, PHASE_PROMPTS
from state.models import TravelPlanState, infer_phase3_step_from_state
from telemetry.attributes import EVENT_PHASE_PLAN_SNAPSHOT, PHASE_FROM, PHASE_TO


class PhaseRouter:
    def __init__(self) -> None:
        self._backtrack_service = BacktrackService()

    def _hydrate_phase3_brief(self, plan: TravelPlanState) -> None:
        if plan.phase < 3 or not plan.destination:
            return

        brief = dict(plan.trip_brief)
        brief.setdefault("destination", plan.destination)
        if plan.dates:
            brief.setdefault("dates", plan.dates.to_dict())
            brief.setdefault("total_days", plan.dates.total_days)
        if plan.travelers:
            brief.setdefault("travelers", plan.travelers.to_dict())
        if plan.budget:
            brief.setdefault("budget", plan.budget.to_dict())
        if plan.preferences:
            brief.setdefault(
                "preferences",
                [p.to_dict() for p in plan.preferences],
            )
        if plan.constraints:
            brief.setdefault(
                "constraints",
                [c.to_dict() for c in plan.constraints],
            )

        if brief != plan.trip_brief:
            plan.trip_brief = brief

    def sync_phase_state(self, plan: TravelPlanState) -> None:
        self._hydrate_phase3_brief(plan)
        plan.phase3_step = infer_phase3_step_from_state(
            phase=plan.phase,
            dates=plan.dates,
            trip_brief=plan.trip_brief,
            candidate_pool=plan.candidate_pool,
            shortlist=plan.shortlist,
            skeleton_plans=plan.skeleton_plans,
            selected_skeleton_id=plan.selected_skeleton_id,
            accommodation=plan.accommodation,
        )

    def infer_phase(self, plan: TravelPlanState) -> int:
        self.sync_phase_state(plan)
        if not plan.destination:
            return 1
        if not plan.dates or not plan.selected_skeleton_id or not plan.accommodation:
            return 3
        if len(plan.daily_plans) < plan.dates.total_days:
            return 5
        return 7

    def get_prompt(self, phase: int) -> str:
        return PHASE_PROMPTS.get(phase, PHASE_PROMPTS[1])

    def get_control_mode(self, phase: int) -> str:
        return PHASE_CONTROL_MODE.get(phase, "conversational")

    async def check_and_apply_transition(
        self,
        plan: TravelPlanState,
        hooks: Any | None = None,
    ) -> bool:
        """Check if plan_state warrants a phase change. Returns True if phase changed."""
        inferred = self.infer_phase(plan)
        if inferred == plan.phase:
            return False

        if hooks is not None:
            gate_result = await hooks.run_gate(
                "before_phase_transition",
                plan=plan,
                from_phase=plan.phase,
                to_phase=inferred,
            )
            if not gate_result.allowed:
                return False

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
            self.sync_phase_state(plan)
        return True

    def prepare_backtrack(
        self,
        plan: TravelPlanState,
        to_phase: int,
        reason: str,
        snapshot_path: str,
    ) -> None:
        """Execute backtrack: delegate to BacktrackService."""
        self._backtrack_service.execute(plan, to_phase, reason, snapshot_path)
