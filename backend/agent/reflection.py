from __future__ import annotations

from agent.types import Message
from state.models import Constraint, Preference, TravelPlanState


class ReflectionInjector:
    def __init__(self) -> None:
        self._triggered: set[str] = set()

    def check_and_inject(
        self, messages: list[Message], plan: TravelPlanState, prev_step: str | None
    ) -> str | None:
        key = self._compute_trigger_key(plan, prev_step)
        if key is None or key in self._triggered:
            return None
        self._triggered.add(key)
        return self._build_prompt(key, plan)

    def _compute_trigger_key(
        self, plan: TravelPlanState, prev_step: str | None
    ) -> str | None:
        if plan.phase == 3 and prev_step == "skeleton" and plan.phase3_step == "lock":
            return "phase3_lock"
        if (
            plan.phase == 5
            and plan.dates is not None
            and plan.dates.total_days > 0
            and len(plan.daily_plans) >= plan.dates.total_days
        ):
            return "phase5_complete"
        return None

    def _build_prompt(self, key: str, plan: TravelPlanState) -> str:
        if key == "phase3_lock":
            return self._build_phase3_lock_prompt(plan)
        if key == "phase5_complete":
            return self._build_phase5_complete_prompt(plan)
        return ""

    def _build_phase3_lock_prompt(self, plan: TravelPlanState) -> str:
        return (
            "[自检]\n"
            "你即将进入交通住宿锁定阶段，请先快速回顾：\n"
            f"1. 用户的偏好（{self._summarize_preferences(plan.preferences)}）是否都在骨架方案中体现了？\n"
            f"2. 用户的约束（{self._summarize_constraints(plan.constraints)}）有没有被违反？\n"
            '3. 有没有用户明确说过"必须"或"不要"的内容被遗漏？\n'
            "如果发现问题，先修正骨架再继续。如果没有问题，直接进入锁定。"
        )

    def _build_phase5_complete_prompt(self, plan: TravelPlanState) -> str:
        pace_preference = self._extract_pace_preference(plan.preferences)
        return (
            "[自检]\n"
            "所有天数的行程已填写完毕，请快速检查：\n"
            '1. 用户最初提到的所有"必去"景点是否都安排了？\n'
            f"2. 每天的节奏是否符合用户偏好（{pace_preference}）？\n"
            "3. 有没有连续两天重复相似类型的活动？\n"
            "如果发现内容问题，优先调用 `replace_daily_plans(...)` 修正；"
            "如果只是缺少某几天，调用 `append_day_plan(...)` 填充；"
            "如果问题需要回到上游重新决策，调用 `request_backtrack`。"
            "如果没有问题，继续。"
        )

    def _summarize_preferences(self, preferences: list[Preference]) -> str:
        if not preferences:
            return "暂无明确偏好"
        items = []
        for preference in preferences[:5]:
            label = getattr(preference, "category", None) or getattr(preference, "key", "")
            items.append(f"{label}={preference.value}")
        return "、".join(items) if items else "暂无明确偏好"

    def _summarize_constraints(self, constraints: list[Constraint]) -> str:
        if not constraints:
            return "暂无明确约束"
        items = [constraint.description for constraint in constraints[:5] if constraint.description]
        return "、".join(items) if items else "暂无明确约束"

    def _extract_pace_preference(self, preferences: list[Preference]) -> str:
        for preference in preferences:
            label = (getattr(preference, "category", None) or getattr(preference, "key", "")).lower()
            if "节奏" in label or "pace" in label:
                return preference.value or "未指定"
        return "未指定"
