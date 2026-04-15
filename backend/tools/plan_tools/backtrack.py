from __future__ import annotations

from state.models import TravelPlanState
from state.plan_writers import execute_backtrack
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "to_phase": {
            "type": "integer",
            "description": "要回退到的目标阶段（必须小于当前阶段）",
        },
        "reason": {
            "type": "string",
            "description": "回退原因",
        },
    },
    "required": ["to_phase", "reason"],
}


def make_request_backtrack_tool(plan: TravelPlanState):
    @tool(
        name="request_backtrack",
        description="请求回退到更早的规划阶段。当用户想推翻之前的阶段决策时使用。目标阶段必须小于当前阶段。",
        phases=[1, 3, 5, 7],
        parameters=_PARAMETERS,
        side_effect="write",
        human_label="请求回退阶段",
    )
    async def request_backtrack(to_phase: int, reason: str) -> dict:
        if type(to_phase) is not int:
            raise ToolError(
                f"to_phase 必须是整数，收到 {type(to_phase).__name__}",
                error_code="INVALID_VALUE",
                suggestion="to_phase 应为整数，如 1、3、5",
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ToolError(
                "reason 必须是非空字符串",
                error_code="INVALID_VALUE",
                suggestion="请提供回退原因",
            )

        to_phase = 1 if to_phase == 2 else to_phase
        reason = reason.strip()

        if to_phase >= plan.phase:
            raise ToolError(
                f"只能回退到更早的阶段，当前阶段: {plan.phase}，目标: {to_phase}",
                error_code="INVALID_BACKTRACK",
                suggestion=f"目标阶段必须小于当前阶段 {plan.phase}，例如 1、3、5 中更早的阶段",
            )

        return execute_backtrack(plan, to_phase, reason)

    return request_backtrack
