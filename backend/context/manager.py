# backend/context/manager.py
from __future__ import annotations

import re
from pathlib import Path

from opentelemetry import trace

from agent.types import Message, Role
from state.models import TravelPlanState
from telemetry.attributes import CONTEXT_TOKENS_BEFORE, CONTEXT_TOKENS_AFTER

# Keywords that signal user preferences — these messages must survive compression
_PREFERENCE_SIGNALS = [
    "不要",
    "不想",
    "不坐",
    "不住",
    "不去",
    "不吃",
    "必须",
    "一定要",
    "偏好",
    "喜欢",
    "讨厌",
    "预算",
    "上限",
    "最多",
    "至少",
    "过敏",
    "素食",
    "忌口",
]


class ContextManager:
    def __init__(self, soul_path: str = "backend/context/soul.md"):
        self._soul_path = Path(soul_path)
        self._soul_cache: str | None = None

    def _load_soul(self) -> str:
        if self._soul_cache is None:
            if self._soul_path.exists():
                self._soul_cache = self._soul_path.read_text(encoding="utf-8")
            else:
                self._soul_cache = "你是一个旅行规划 Agent。"
        return self._soul_cache

    def build_system_message(
        self,
        plan: TravelPlanState,
        phase_prompt: str,
        user_summary: str = "",
    ) -> Message:
        parts = [
            self._load_soul(),
            "",
            "---",
            "",
            f"## 当前阶段指引\n\n{phase_prompt}",
        ]

        runtime = self.build_runtime_context(plan)
        if runtime:
            parts.extend(["", "---", "", f"## 当前规划状态\n\n{runtime}"])

        if user_summary:
            parts.extend(["", "---", "", f"## 用户画像\n\n{user_summary}"])

        return Message(role=Role.SYSTEM, content="\n".join(parts))

    def build_runtime_context(self, plan: TravelPlanState) -> str:
        parts = [f"- 阶段：{plan.phase}"]
        if plan.destination:
            parts.append(f"- 目的地：{plan.destination}")
        if plan.dates:
            parts.append(
                f"- 日期：{plan.dates.start} 至 {plan.dates.end}（{plan.dates.total_days} 天）"
            )
        if plan.budget:
            allocated = sum(
                act.cost for day in plan.daily_plans for act in day.activities
            )
            parts.append(
                f"- 预算：{plan.budget.total} {plan.budget.currency}，已分配：{allocated}"
            )
        if plan.accommodation:
            parts.append(f"- 住宿区域：{plan.accommodation.area}")
        if plan.daily_plans:
            total_days = plan.dates.total_days if plan.dates else "?"
            parts.append(f"- 已规划 {len(plan.daily_plans)}/{total_days} 天")
        if plan.backtrack_history:
            last = plan.backtrack_history[-1]
            parts.append(
                f"- 最近回溯：阶段{last.from_phase}→{last.to_phase}，原因：{last.reason}"
            )
        return "\n".join(parts)

    def should_compress(self, messages: list[Message], max_tokens: int) -> bool:
        tracer = trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("context.should_compress") as span:
            estimated = sum(len(m.content or "") // 3 for m in messages)
            span.set_attribute(CONTEXT_TOKENS_BEFORE, estimated)
            span.set_attribute("context.max_tokens", max_tokens)
            result = estimated > max_tokens * 0.5
            return result

    def classify_messages(
        self, messages: list[Message]
    ) -> tuple[list[Message], list[Message]]:
        must_keep: list[Message] = []
        compressible: list[Message] = []

        for msg in messages:
            content = msg.content or ""
            if msg.role == Role.USER and any(
                kw in content for kw in _PREFERENCE_SIGNALS
            ):
                must_keep.append(msg)
            else:
                compressible.append(msg)

        return must_keep, compressible
