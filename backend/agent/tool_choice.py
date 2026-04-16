from __future__ import annotations

from typing import Any

from agent.types import Message
from state.models import TravelPlanState


class ToolChoiceDecider:
    """Decides tool_choice parameter for LLM calls.

    After the migration to single-responsibility plan-writing tools,
    hard-forcing a specific tool is no longer appropriate because the
    LLM must choose among multiple tools. Always return "auto" and
    rely on prompt discipline instead.

    If eval metrics show the LLM skipping state writes too often,
    reintroduce context-aware selection in a follow-up.
    """

    def decide(
        self, plan: TravelPlanState, messages: list[Message], phase: int
    ) -> str | dict[str, Any]:
        return "auto"
