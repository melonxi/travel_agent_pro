# backend/context/manager.py
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from opentelemetry import trace

from agent.compaction import estimate_messages_tokens
from agent.types import Message, Role, ToolCall, ToolResult
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
        if plan.travelers:
            parts.append(
                f"- 出行人数：{plan.travelers.adults} 成人"
                + (f"、{plan.travelers.children} 儿童" if plan.travelers.children else "")
            )

        # Phase 3 later sub-stages & Phase 5+: inject trip_brief content
        if plan.trip_brief:
            if plan.phase >= 5 or (plan.phase == 3 and plan.phase3_step in ("candidate", "skeleton", "lock")):
                parts.append("- 旅行画像：")
                for key, val in plan.trip_brief.items():
                    parts.append(f"  - {key}: {val}")
            else:
                parts.append(f"- 已生成旅行画像：{len(plan.trip_brief)} 项")

        if plan.candidate_pool:
            parts.append(f"- 候选池：{len(plan.candidate_pool)} 项")
            # Phase 3 skeleton+: show shortlist item summaries
            if plan.phase == 3 and plan.phase3_step in ("skeleton", "lock") and plan.shortlist:
                parts.append(f"- shortlist（{len(plan.shortlist)} 项）：")
                for item in plan.shortlist[:8]:
                    if isinstance(item, dict):
                        label = item.get("name") or item.get("title") or item.get("area") or str(item)[:60]
                        parts.append(f"  - {label}")
            elif plan.shortlist:
                parts.append(f"- shortlist：{len(plan.shortlist)} 项")

        # Phase 5+: inject selected skeleton full content
        # Phase 3 lock: also inject selected skeleton content
        if plan.skeleton_plans:
            inject_skeleton = (plan.phase >= 5 and plan.selected_skeleton_id) or \
                              (plan.phase == 3 and plan.phase3_step == "lock" and plan.selected_skeleton_id)
            if inject_skeleton:
                selected = self._find_selected_skeleton(plan)
                if selected:
                    parts.append(f"- 已选骨架方案（{plan.selected_skeleton_id}）：")
                    for key, val in selected.items():
                        if key == "id":
                            continue
                        parts.append(f"  - {key}: {val}")
                else:
                    parts.append(f"- 骨架方案：{len(plan.skeleton_plans)} 套")
                    parts.append(f"- 已选骨架：{plan.selected_skeleton_id}")
            else:
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
            if plan.accommodation.hotel:
                parts.append(f"- 住宿酒店：{plan.accommodation.hotel}")

        # Phase 3 later sub-stages & Phase 5+: inject preferences and constraints
        if plan.preferences and (plan.phase >= 5 or (plan.phase == 3 and plan.phase3_step in ("skeleton", "lock"))):
            pref_strs = [f"{p.key}: {p.value}" for p in plan.preferences if p.key]
            if pref_strs:
                parts.append(f"- 用户偏好：{'; '.join(pref_strs)}")
        if plan.constraints and (plan.phase >= 5 or (plan.phase == 3 and plan.phase3_step in ("skeleton", "lock"))):
            cons_strs = [f"[{c.type}] {c.description}" for c in plan.constraints]
            if cons_strs:
                parts.append(f"- 用户约束：{'; '.join(cons_strs)}")

        # Phase 5: inject daily_plans progress with summary
        if plan.daily_plans:
            total_days = plan.dates.total_days if plan.dates else "?"
            parts.append(f"- 已规划 {len(plan.daily_plans)}/{total_days} 天")
            if plan.phase == 5:
                for dp in plan.daily_plans:
                    act_names = [a.name for a in dp.activities[:5]]
                    act_summary = "、".join(act_names) if act_names else "无活动"
                    parts.append(f"  - 第{dp.day}天（{dp.date}）：{act_summary}")
                if plan.dates:
                    planned_days = {dp.day for dp in plan.daily_plans}
                    missing = [
                        d for d in range(1, plan.dates.total_days + 1)
                        if d not in planned_days
                    ]
                    if missing:
                        parts.append(f"  - 待规划天数：{', '.join(map(str, missing))}")
        if plan.backtrack_history:
            last = plan.backtrack_history[-1]
            parts.append(
                f"- 最近回溯：阶段{last.from_phase}→{last.to_phase}，原因：{last.reason}"
            )
        return "\n".join(parts)

    def _find_selected_skeleton(self, plan: TravelPlanState) -> dict | None:
        """Find the skeleton plan matching selected_skeleton_id."""
        if not plan.selected_skeleton_id or not plan.skeleton_plans:
            return None
        sid = plan.selected_skeleton_id
        for skeleton in plan.skeleton_plans:
            if not isinstance(skeleton, dict):
                continue
            if skeleton.get("id") == sid or skeleton.get("name") == sid:
                return skeleton
        # Fallback: if exactly one skeleton exists, use it (no ambiguity)
        valid = [s for s in plan.skeleton_plans if isinstance(s, dict)]
        if len(valid) == 1:
            return valid[0]
        return None

    def should_compress(
        self,
        messages: list[Message],
        max_tokens: int,
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> bool:
        tracer = trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("context.should_compress") as span:
            estimated = estimate_messages_tokens(messages, tools=tools)
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
        llm_factory: Callable[[], Any] | None = None,
    ) -> str:
        """Produce a rule-based, deterministic summary of prior-phase context.

        This used to spin up an extra LLM call per phase transition. That cost
        real latency and money, and the summarizer could silently drop details.
        We now build the summary from the message log directly:

        - Keep every user message verbatim.
        - Condense each assistant text turn to its first 200 chars.
        - Render every tool call as one line (``update_plan_state`` shows
          ``field → value``; other tools show name + status + a short result
          fingerprint).
        - System messages are skipped; they are re-emitted by the new phase's
          system message rebuild path.

        The ``llm_factory`` parameter is accepted for signature compatibility
        with older callers/tests but is no longer used.
        """
        del llm_factory  # explicitly unused

        lines: list[str] = []
        # Track the assistant message whose tool_calls produced each tool
        # result, so ``update_plan_state(field=..., value=...)`` can be
        # rendered as a single decision line.
        pending_tool_calls: dict[str, ToolCall] = {}

        for message in messages:
            if message.role == Role.SYSTEM:
                continue

            if message.role == Role.USER and message.content:
                lines.append(f"用户: {message.content.strip()}")
                continue

            if message.role == Role.ASSISTANT:
                if message.tool_calls:
                    for tc in message.tool_calls:
                        pending_tool_calls[tc.id] = tc
                if message.content:
                    snippet = message.content.strip()
                    if len(snippet) > 200:
                        snippet = snippet[:200].rstrip() + "…"
                    lines.append(f"助手: {snippet}")
                continue

            if message.role == Role.TOOL and message.tool_result:
                tool_call = pending_tool_calls.get(message.tool_result.tool_call_id)
                line = self._render_tool_event(tool_call, message.tool_result)
                if line:
                    lines.append(line)
                continue

        return "\n".join(lines)

    def _render_tool_event(
        self,
        tool_call: ToolCall | None,
        result: ToolResult,
    ) -> str:
        name = tool_call.name if tool_call else "tool"

        # update_plan_state is the richest signal — render it as a decision.
        if tool_call and tool_call.name == "update_plan_state":
            field = tool_call.arguments.get("field", "?")
            value = tool_call.arguments.get("value")
            value_preview = self._short_repr(value)
            if result.status == "success":
                return f"决策: update_plan_state {field} = {value_preview}"
            if result.status == "skipped":
                return f"跳过: update_plan_state {field}（{result.error_code or 'skipped'}）"
            return (
                f"失败: update_plan_state {field} — {result.error_code or ''} "
                f"{(result.error or '').strip()}"
            ).strip()

        if result.status == "success":
            data_preview = self._short_repr(result.data)
            return f"工具 {name} 成功: {data_preview}"
        if result.status == "skipped":
            return f"工具 {name} 跳过: {result.error_code or ''} {(result.error or '').strip()}".strip()
        return f"工具 {name} 失败: {result.error_code or ''} {(result.error or '').strip()}".strip()

    def _short_repr(self, value: Any, limit: int = 160) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value.strip()
        elif isinstance(value, (int, float, bool)):
            text = str(value)
        else:
            # dict / list / other — use a compact str() rather than JSON to
            # avoid dependency surprises with non-serializable entries.
            text = str(value)
        text = text.replace("\n", " ")
        if len(text) > limit:
            return text[:limit].rstrip() + "…"
        return text
