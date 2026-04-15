"""Rule-based narration hints for each planning phase/step.

Returns a short Chinese sentence describing what the agent is currently
doing, suitable for display in the ThinkingBubble hint field.
"""

from __future__ import annotations

from state.models import TravelPlanState


def compute_narration(plan: TravelPlanState) -> str | None:
    """Return a human-readable hint for the current phase/step, or None."""
    if plan.phase == 1 and not plan.destination:
        return "先搞清楚你想去哪，然后翻点真实游记"
    if plan.phase == 1 and plan.destination:
        return "围绕目的地再收几条真实游记，定细节"
    if plan.phase == 3:
        step = getattr(plan, "phase3_step", None)
        if step == "brief":
            return "建立旅行画像，理清你的节奏和偏好"
        if step == "candidate":
            return "挑几个候选景点，看看哪些对你胃口"
        if step == "skeleton":
            return "把候选拼成 2–3 套骨架方案"
        if step == "lock":
            return "锁定交通和住宿，核一下预算"
    if plan.phase == 5:
        return "把骨架展开成日程，核对冲突"
    if plan.phase == 7:
        return "做出发前检查清单"
    return None
