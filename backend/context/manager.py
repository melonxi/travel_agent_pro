# backend/context/manager.py
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from opentelemetry import trace

from agent.types import Message, Role
from state.models import TravelPlanState
from telemetry.attributes import (
    CONTEXT_TOKENS_AFTER,
    CONTEXT_TOKENS_BEFORE,
    EVENT_CONTEXT_COMPRESSION,
)

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
        available_tools: list[str] | None = None,
    ) -> Message:
        runtime_clock = self.build_time_context()
        parts = [
            self._load_soul(),
            "",
            "---",
            "",
            f"## 当前时间\n\n{runtime_clock}",
            "",
            "---",
            "",
            "## 工具使用硬规则\n\n"
            "- 当用户提供了明确的规划信息（目的地、日期、预算、人数、偏好、约束、住宿、候选地等）时，如果这些信息尚未写入当前规划状态，或是在修改已有值，必须先调用 `update_plan_state` 写入状态，不能只在自然语言里复述。\n"
            "- 同一条用户消息里如果包含多个字段，可以连续调用多次 `update_plan_state`。\n"
            "- 如果某个字段已经准确体现在“当前规划状态”里，不要重复调用 `update_plan_state` 写入相同值。\n"
            "- 只能写入用户本轮或历史中明确说过的信息，不要把你的推断、推荐、联想、示例、默认值写入状态。\n"
            "- 如果用户只说了“玩5天”“3万预算”“3个人”，只能记录天数/预算/人数这一层信息；没有明确年月日时，不要擅自写入具体出发和返回日期。\n"
            "- `preferences` 只用于记录用户明确表达的偏好，例如“节奏轻松”“想住海边”“喜欢美食”；不要把你总结出来的必去景点、推荐区域、住宿分析、行程建议写进 `preferences`。\n"
            "- `constraints` 只用于用户明确提出的硬/软约束；不要把你为方便规划而脑补的需求写进 `constraints`。\n"
            "- 当用户要求推翻之前的阶段决策时，必须使用 `update_plan_state(field=\"backtrack\", value={...})`。\n"
            "- 如果用户问“你现在在哪个阶段 / 当前有哪些工具 / 现在能不能查航班或酒店”，必须严格按照“当前规划状态”和本轮提供的工具列表回答，不要凭记忆猜测。\n"
            "- 完成必要的状态写入后，再继续提问、解释或给建议。",
            "",
            "---",
            "",
            f"## 当前阶段指引\n\n{phase_prompt}",
        ]

        runtime = self.build_runtime_context(plan, available_tools=available_tools)
        if runtime:
            parts.extend(["", "---", "", f"## 当前规划状态\n\n{runtime}"])

        if user_summary:
            parts.extend(["", "---", "", f"## 用户画像\n\n{user_summary}"])

        return Message(role=Role.SYSTEM, content="\n".join(parts))

    def build_time_context(self) -> str:
        now = datetime.now().astimezone()
        tz_name = now.tzname() or "local"
        tz_offset = now.strftime("%z")
        tz_offset = (
            f"{tz_offset[:3]}:{tz_offset[3:]}" if tz_offset and len(tz_offset) == 5 else tz_offset
        )
        return (
            f"- 当前本地日期：{now.strftime('%Y-%m-%d')}\n"
            f"- 当前本地时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- 当前时区：{tz_name} ({tz_offset or 'unknown'})\n"
            "- 对“今天 / 明天 / 下周 / 五一 / 暑假 / 下个月”等相对时间的理解，必须以上述当前时间为基准。"
        )

    def build_runtime_context(
        self,
        plan: TravelPlanState,
        *,
        available_tools: list[str] | None = None,
    ) -> str:
        parts = [f"- 阶段：{plan.phase}"]
        if plan.phase == 3:
            parts.append(f"- Phase 3 子阶段：{plan.phase3_step}")
        if available_tools:
            parts.append(f"- 当前可用工具：{', '.join(available_tools)}")
        if plan.destination:
            parts.append(f"- 目的地：{plan.destination}")
        if plan.dates:
            parts.append(
                f"- 日期：{plan.dates.start} 至 {plan.dates.end}（{plan.dates.total_days} 天）"
            )
        if plan.trip_brief:
            parts.append(f"- 已生成旅行画像：{len(plan.trip_brief)} 项")
        if plan.candidate_pool:
            parts.append(f"- 候选池：{len(plan.candidate_pool)} 项")
        if plan.shortlist:
            parts.append(f"- shortlist：{len(plan.shortlist)} 项")
        if plan.skeleton_plans:
            parts.append(f"- 骨架方案：{len(plan.skeleton_plans)} 套")
        if plan.selected_skeleton_id:
            parts.append(f"- 已选骨架：{plan.selected_skeleton_id}")
        if plan.selected_transport:
            parts.append("- 已选大交通：是")
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
            result = estimated > max_tokens
            if result:
                must_keep, _ = self.classify_messages(messages)
                span.add_event(
                    EVENT_CONTEXT_COMPRESSION,
                    {
                        "message_count": len(messages),
                        "estimated_tokens": estimated,
                        "must_keep_count": len(must_keep),
                    },
                )
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

    async def compress_for_transition(
        self,
        messages: list[Message],
        from_phase: int,
        to_phase: int,
        llm_factory: Callable[[], Any],
    ) -> str:
        transition_messages = [m for m in messages if m.role != Role.SYSTEM]
        rendered = self._render_transition_messages(transition_messages)
        if len(transition_messages) < 4:
            return rendered

        llm = llm_factory()
        prompt = [
            Message(
                role=Role.SYSTEM,
                content=(
                    "你负责为旅行规划 agent 做阶段切换摘要。"
                    "只保留用户偏好、约束、已确认事实、关键决策和待确认事项。"
                    "输出简洁中文，不要杜撰，不要重复无关寒暄。"
                ),
            ),
            Message(
                role=Role.USER,
                content=(
                    f"当前对话从阶段 {from_phase} 切换到阶段 {to_phase}。\n"
                    "请基于下面的对话生成一个可供下阶段继续使用的摘要。\n\n"
                    f"{rendered}"
                ),
            ),
        ]

        parts: list[str] = []
        async for chunk in llm.chat(prompt, tools=[], stream=False):
            if chunk.content:
                parts.append(chunk.content)

        summary = "".join(parts).strip()
        return summary or rendered

    def _render_transition_messages(self, messages: list[Message]) -> str:
        lines: list[str] = []
        for message in messages:
            line = self._render_transition_message(message)
            if line:
                lines.append(line)
        return "\n".join(lines)

    def _render_transition_message(self, message: Message) -> str:
        if message.role == Role.USER:
            return f"用户: {message.content or ''}".strip()
        if message.role == Role.ASSISTANT:
            return f"助手: {message.content or ''}".strip()
        if message.role == Role.TOOL and message.tool_result:
            result = message.tool_result
            if result.status == "success":
                return f"工具结果: {result.data}"
            return f"工具错误: {result.error or ''}".strip()
        return message.content or ""
