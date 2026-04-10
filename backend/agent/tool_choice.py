from __future__ import annotations

import re
from typing import Any

from agent.types import Message, Role
from state.models import TravelPlanState

_FORCED = {"type": "function", "function": {"name": "update_plan_state"}}

_SKELETON_KEYWORDS = re.compile(r"骨架|方案\s*[A-C1-3]|轻松版|平衡版|高密度版|紧凑版")
_ITINERARY_DAY = re.compile(r"第\s*\d+\s*天|第\s*[一二三四五六七八九十]+\s*天|Day\s*\d+|DAY\s*\d+")
_TIME_SLOT = re.compile(r"\d{1,2}:\d{2}")
_ACTIVITY_KEYWORDS = ("活动", "景点", "行程", "安排", "上午", "下午", "晚上", "餐厅")


class ToolChoiceDecider:
    def decide(
        self, plan: TravelPlanState, messages: list[Message], phase: int
    ) -> str | dict[str, Any]:
        if phase == 3:
            return self._decide_phase3(plan, messages)
        if phase == 5:
            return self._decide_phase5(plan, messages)
        return "auto"

    def _decide_phase3(
        self, plan: TravelPlanState, messages: list[Message]
    ) -> str | dict[str, Any]:
        if plan.phase3_step == "brief" and not plan.trip_brief and self._count_user_messages(messages) >= 2:
            return _FORCED
        if (
            plan.phase3_step == "skeleton"
            and not plan.skeleton_plans
            and _SKELETON_KEYWORDS.search(self._last_assistant_text(messages) or "")
        ):
            return _FORCED
        return "auto"

    def _decide_phase5(
        self, plan: TravelPlanState, messages: list[Message]
    ) -> str | dict[str, Any]:
        if plan.dates is None:
            return "auto"
        planned_days = {
            getattr(day_plan, "day", None)
            if not isinstance(day_plan, dict)
            else day_plan.get("day")
            for day_plan in plan.daily_plans
        }
        planned_days.discard(None)
        if len(planned_days) >= plan.dates.total_days:
            return "auto"

        assistant_text = self._last_assistant_text(messages) or ""
        if _ITINERARY_DAY.search(assistant_text) and (
            _TIME_SLOT.search(assistant_text)
            or any(keyword in assistant_text for keyword in _ACTIVITY_KEYWORDS)
        ):
            return _FORCED
        return "auto"

    def _count_user_messages(self, messages: list[Message]) -> int:
        return sum(1 for message in messages if message.role == Role.USER)

    def _last_assistant_text(self, messages: list[Message]) -> str | None:
        for message in reversed(messages):
            if message.role == Role.ASSISTANT and message.content:
                return message.content
        return None
