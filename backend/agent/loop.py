# backend/agent/loop.py
from __future__ import annotations

import re
from typing import Any, AsyncIterator

from opentelemetry import trace

from agent.hooks import HookManager
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from telemetry.attributes import AGENT_PHASE, AGENT_ITERATION
from tools.engine import ToolEngine
from tools.update_plan_state import is_redundant_update_plan_state


class AgentLoop:
    def __init__(
        self,
        llm,
        tool_engine: ToolEngine,
        hooks: HookManager,
        max_retries: int = 3,
        phase_router: Any | None = None,
        context_manager: Any | None = None,
        plan: Any | None = None,
        llm_factory: Any | None = None,
        memory_mgr: Any | None = None,
        user_id: str = "default_user",
        compression_events: list[dict] | None = None,
    ):
        self.llm = llm
        self.tool_engine = tool_engine
        self.hooks = hooks
        self.max_retries = max_retries
        self.phase_router = phase_router
        self.context_manager = context_manager
        self.plan = plan
        self.llm_factory = llm_factory
        self.memory_mgr = memory_mgr
        self.user_id = user_id
        self.compression_events: list[dict] = compression_events if compression_events is not None else []

    async def run(
        self,
        messages: list[Message],
        phase: int,
        tools_override: list[dict] | None = None,
    ) -> AsyncIterator[LLMChunk]:
        tracer = trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("agent_loop.run") as span:
            span.set_attribute(AGENT_PHASE, phase)
            current_phase = self.plan.phase if self.plan is not None else phase
            tools = tools_override or self.tool_engine.get_tools_for_phase(
                current_phase,
                self.plan,
            )
            original_user_message = self._extract_original_user_message(messages)
            repair_hints_used: set[str] = set()

            for iteration in range(self.max_retries):  # safety limit on loop iterations
                with tracer.start_as_current_span("agent_loop.iteration") as iter_span:
                    iter_span.set_attribute(AGENT_ITERATION, iteration)

                    await self.hooks.run(
                        "before_llm_call",
                        messages=messages,
                        phase=current_phase,
                        tools=tools,
                    )

                    # Yield pending compression events from hook
                    while self.compression_events:
                        info = self.compression_events.pop(0)
                        yield LLMChunk(
                            type=ChunkType.CONTEXT_COMPRESSION,
                            compression_info=info,
                        )

                    tool_calls: list[ToolCall] = []
                    text_chunks: list[str] = []

                    async for chunk in self.llm.chat(
                        messages, tools=tools, stream=True
                    ):
                        if chunk.type == ChunkType.TEXT_DELTA:
                            text_chunks.append(chunk.content or "")
                            yield chunk
                        elif (
                            chunk.type == ChunkType.TOOL_CALL_START and chunk.tool_call
                        ):
                            tool_calls.append(chunk.tool_call)
                            yield chunk
                        elif chunk.type == ChunkType.DONE:
                            pass

                    # If no tool calls, we're done — the LLM gave a final text response
                    if not tool_calls:
                        full_text = "".join(text_chunks)
                        repair_message = (
                            self._build_phase3_state_repair_message(
                                current_phase=current_phase,
                                assistant_text=full_text,
                                repair_hints_used=repair_hints_used,
                            )
                            or self._build_phase5_state_repair_message(
                                current_phase=current_phase,
                                assistant_text=full_text,
                                repair_hints_used=repair_hints_used,
                            )
                        )
                        if full_text:
                            messages.append(
                                Message(role=Role.ASSISTANT, content=full_text)
                            )
                        if repair_message:
                            messages.append(
                                Message(role=Role.SYSTEM, content=repair_message)
                            )
                            if current_phase == 5:
                                repair_hints_used.add("p5_daily")
                            else:
                                repair_hints_used.add(
                                    f"p{current_phase}_{getattr(self.plan, 'phase3_step', '')}"
                                )
                            continue
                        yield LLMChunk(type=ChunkType.DONE)
                        return

                    # Record assistant message with tool calls
                    messages.append(
                        Message(
                            role=Role.ASSISTANT,
                            content="".join(text_chunks) or None,
                            tool_calls=tool_calls,
                        )
                    )

                    # Execute one tool batch, then evaluate phase transition once.
                    phase_before_batch = (
                        self.plan.phase if self.plan is not None else current_phase
                    )
                    phase3_step_before_batch = (
                        getattr(self.plan, "phase3_step", None)
                        if self.plan is not None
                        else None
                    )
                    needs_rebuild = False
                    saw_state_update = False
                    rebuild_result: ToolResult | None = None
                    for idx, tc in enumerate(tool_calls):
                        if self._should_skip_redundant_update(tc):
                            result = self._build_skipped_tool_result(
                                tc.id,
                                error="Skipped redundant state update",
                                error_code="REDUNDANT_STATE_UPDATE",
                                suggestion="This value is already reflected in the current plan state.",
                            )
                        else:
                            result = await self.tool_engine.execute(tc)
                        if tc.name == "update_plan_state" and result.status == "success":
                            saw_state_update = True

                        messages.append(
                            Message(
                                role=Role.TOOL,
                                tool_result=result,
                            )
                        )

                        # Keepalive ping so the SSE connection stays alive during
                        # back-to-back tool executions that produce no text output
                        yield LLMChunk(type=ChunkType.KEEPALIVE)

                        await self.hooks.run(
                            "after_tool_call",
                            tool_name=tc.name,
                            tool_call=tc,
                            result=result,
                        )

                        yield LLMChunk(
                            type=ChunkType.TOOL_RESULT,
                            tool_result=result,
                        )

                        if self._is_backtrack_result(result):
                            rebuild_result = result
                            for skipped_tc in tool_calls[idx + 1:]:
                                yield LLMChunk(
                                    type=ChunkType.TOOL_RESULT,
                                    tool_result=self._build_skipped_tool_result(
                                        skipped_tc.id,
                                        error="Skipped after backtrack",
                                        error_code="BACKTRACK_CHANGED",
                                        suggestion="The conversation moved to an earlier phase before this tool ran.",
                                    ),
                                )
                            needs_rebuild = True
                            break

                    if needs_rebuild:
                        phase_after_batch = (
                            self.plan.phase if self.plan is not None else current_phase
                        )
                        messages[:] = await self._rebuild_messages_for_phase_change(
                            messages=messages,
                            from_phase=phase_before_batch,
                            to_phase=phase_after_batch,
                            original_user_message=original_user_message,
                            result=rebuild_result
                            or ToolResult(
                                tool_call_id="",
                                status="success",
                            ),
                        )
                        current_phase = phase_after_batch
                        tools = self.tool_engine.get_tools_for_phase(
                            current_phase,
                            self.plan,
                        )
                        continue

                    phase_after_batch = (
                        self.plan.phase if self.plan is not None else current_phase
                    )
                    if phase_after_batch != phase_before_batch:
                        messages[:] = await self._rebuild_messages_for_phase_change(
                            messages=messages,
                            from_phase=phase_before_batch,
                            to_phase=phase_after_batch,
                            original_user_message=original_user_message,
                            result=ToolResult(
                                tool_call_id="",
                                status="success",
                            ),
                        )
                        current_phase = phase_after_batch
                        tools = self.tool_engine.get_tools_for_phase(
                            current_phase,
                            self.plan,
                        )
                        continue

                    if (
                        saw_state_update
                        and self.phase_router is not None
                        and self.plan is not None
                    ):
                        phase_changed = (
                            await self.phase_router.check_and_apply_transition(
                                self.plan, hooks=self.hooks
                            )
                        )
                        phase_after_batch = self.plan.phase
                        if phase_changed:
                            messages[:] = await self._rebuild_messages_for_phase_change(
                                messages=messages,
                                from_phase=phase_before_batch,
                                to_phase=phase_after_batch,
                                original_user_message=original_user_message,
                                result=ToolResult(
                                    tool_call_id="",
                                    status="success",
                                ),
                            )
                            current_phase = phase_after_batch
                            tools = self.tool_engine.get_tools_for_phase(
                                current_phase,
                                self.plan,
                            )
                            continue

                    phase3_step_after_batch = (
                        getattr(self.plan, "phase3_step", None)
                        if self.plan is not None
                        else None
                    )
                    if phase3_step_after_batch != phase3_step_before_batch:
                        tools = self.tool_engine.get_tools_for_phase(
                            current_phase,
                            self.plan,
                        )

                    # Loop continues — LLM will see tool results and decide next step

            # Safety limit reached
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA, content="[达到最大循环次数，请重新发送消息]"
            )
            yield LLMChunk(type=ChunkType.DONE)

    def _extract_original_user_message(self, messages: list[Message]) -> Message:
        for message in reversed(messages):
            if message.role == Role.USER:
                return self._copy_message(message)
        return Message(role=Role.USER, content="")

    def _copy_message(self, message: Message) -> Message:
        return Message(
            role=message.role,
            content=message.content,
            tool_calls=message.tool_calls,
            tool_result=message.tool_result,
            name=message.name,
        )

    async def _rebuild_messages_for_phase_change(
        self,
        messages: list[Message],
        from_phase: int,
        to_phase: int,
        original_user_message: Message,
        result: ToolResult,
    ) -> list[Message]:
        if (
            self.phase_router is None
            or self.context_manager is None
            or self.plan is None
            or self.memory_mgr is None
        ):
            raise RuntimeError("Phase-aware rebuild requires router/context/plan/memory")

        phase_prompt = self.phase_router.get_prompt(to_phase)
        memory = await self.memory_mgr.load(self.user_id)
        user_summary = self.memory_mgr.generate_summary(memory)
        rebuilt = [
            self.context_manager.build_system_message(
                self.plan,
                phase_prompt,
                user_summary,
                available_tools=self._current_tool_names(to_phase),
            )
        ]

        if to_phase < from_phase:
            rebuilt.append(
                Message(
                    role=Role.SYSTEM,
                    content=self._build_backtrack_notice(from_phase, to_phase, result),
                )
            )
        else:
            summary = await self.context_manager.compress_for_transition(
                messages=messages,
                from_phase=from_phase,
                to_phase=to_phase,
                llm_factory=self.llm_factory,
            )
            if summary:
                rebuilt.append(
                    Message(
                        role=Role.ASSISTANT,
                        content=(
                            f"以下是阶段 {from_phase} 的对话与工具调用回顾，"
                            f"现在进入阶段 {to_phase}。\n{summary}"
                        ),
                    )
                )
            # Keep the user's current request as a non-system anchor for the next
            # phase. Some Anthropic-compatible gateways reject payloads whose
            # messages array is empty even when a system prompt is present.
            rebuilt.append(self._copy_message(original_user_message))

        if to_phase < from_phase:
            rebuilt.append(self._copy_message(original_user_message))
        return rebuilt

    def _build_backtrack_notice(
        self, from_phase: int, to_phase: int, result: ToolResult
    ) -> str:
        reason = "用户请求回退"
        if isinstance(result.data, dict) and result.data.get("reason"):
            reason = str(result.data["reason"])
        elif getattr(self.plan, "backtrack_history", None):
            reason = self.plan.backtrack_history[-1].reason
        return f"[阶段回退]\n用户从 phase {from_phase} 回退到 phase {to_phase}，原因：{reason}"

    def _is_backtrack_result(self, result: ToolResult) -> bool:
        return (
            result.status == "success"
            and isinstance(result.data, dict)
            and bool(result.data.get("backtracked"))
        )

    def _build_skipped_tool_result(
        self,
        tool_call_id: str,
        *,
        error: str,
        error_code: str,
        suggestion: str,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            status="skipped",
            error=error,
            error_code=error_code,
            suggestion=suggestion,
        )

    def _current_tool_names(self, phase: int | None = None) -> list[str]:
        target_phase = phase if phase is not None else (
            self.plan.phase if self.plan is not None else None
        )
        if target_phase is None:
            return []
        return [
            tool["name"]
            for tool in self.tool_engine.get_tools_for_phase(target_phase, self.plan)
        ]

    def _build_phase3_state_repair_message(
        self,
        *,
        current_phase: int,
        assistant_text: str,
        repair_hints_used: set[str],
    ) -> str | None:
        if current_phase != 3 or self.plan is None:
            return None
        if not self.plan.destination:
            return None
        text = assistant_text.strip()
        if len(text) < 12:
            return None

        step = getattr(self.plan, "phase3_step", "")
        repair_key = f"p3_{step}"
        if repair_key in repair_hints_used:
            return None

        if (
            step == "brief"
            and not self.plan.trip_brief
            and any(token in text for token in ("画像", "偏好", "约束", "预算", "日期", "旅行"))
        ):
            return (
                "[状态同步提醒]\n"
                "你刚刚已经完成了旅行画像说明，但 `trip_brief` 仍为空。"
                "请先调用 `update_plan_state(field=\"trip_brief\", value={...})`"
                " 写入结构化 brief；如果日期、预算、人数、偏好、约束是用户明确说过的，也要补写对应状态。"
                "写完后再继续，不要重复整段面向用户解释。"
            )

        if (
            step == "candidate"
            and not self.plan.candidate_pool
            and not self.plan.shortlist
            and any(token in text for token in ("候选", "推荐", "不建议", "why", "why_not"))
        ):
            return (
                "[状态同步提醒]\n"
                "你刚刚已经给出了候选筛选结果，但 `candidate_pool` / `shortlist` 仍为空。"
                "请先调用 `update_plan_state` 把候选全集写入 `candidate_pool`，把第一轮筛选结果写入 `shortlist`。"
                "写入 shortlist 后系统会自动推进子阶段。"
            )

        if (
            step == "skeleton"
            and not self.plan.skeleton_plans
            and (
                "骨架" in text
                or re.search(r"方案\s*[A-C1-3]", text)
                or any(token in text for token in ("轻松版", "平衡版", "高密度版"))
            )
        ):
            return (
                "[状态同步提醒]\n"
                "你刚刚已经给出了 2-3 套骨架方案，但 `skeleton_plans` 仍为空。"
                "请先调用 `update_plan_state(field=\"skeleton_plans\", value=[...])`"
                " 写入结构化骨架方案列表（传 list 整体替换）。"
                "如果用户已经明确选中某套方案，再写 `selected_skeleton_id`，系统会自动推进到 lock 子阶段。"
            )

        if (
            step == "lock"
            and not self.plan.transport_options
            and not self.plan.accommodation_options
            and not self.plan.risks
            and not self.plan.alternatives
            and self.plan.accommodation is None
            and any(
                token in text
                for token in ("住宿", "酒店", "航班", "火车", "交通", "风险", "备选")
            )
        ):
            return (
                "[状态同步提醒]\n"
                "你刚刚已经给出了锁定阶段建议，但 `transport_options` / `accommodation_options` / `risks` / `alternatives` 仍未写入。"
                "请先把结构化结果写入对应字段；只有用户明确选中了交通或住宿时，才写 `selected_transport` 或 `accommodation`。"
            )

        return None

    def _build_phase5_state_repair_message(
        self,
        *,
        current_phase: int,
        assistant_text: str,
        repair_hints_used: set[str],
    ) -> str | None:
        """Detect when Phase 5 LLM outputs itinerary text but forgets to call update_plan_state."""
        if current_phase != 5 or self.plan is None:
            return None
        if not self.plan.dates:
            return None
        repair_key = f"p5_daily"
        if repair_key in repair_hints_used:
            return None
        text = assistant_text.strip()
        if len(text) < 20:
            return None

        total_days = self.plan.dates.total_days
        # Use unique day numbers to avoid counting duplicate entries
        planned_days = set()
        for dp in self.plan.daily_plans:
            if hasattr(dp, "day"):
                planned_days.add(dp.day)
            elif isinstance(dp, dict):
                planned_days.add(dp.get("day"))
        planned_count = len(planned_days)

        if planned_count >= total_days:
            return None

        # Detect itinerary-like content in text without state write
        day_pattern_count = len(re.findall(
            r"第\s*[1-9一二三四五六七八九十]\s*天|Day\s*\d|DAY\s*\d",
            text,
        ))
        has_time_slots = bool(re.search(r"\d{1,2}:\d{2}", text))
        has_activity_markers = any(
            kw in text for kw in ("活动", "景点", "行程", "安排", "上午", "下午", "晚上", "餐厅")
        )
        # Also detect JSON-schema style itinerary output
        has_json_markers = sum(
            1 for kw in ('"day"', '"date"', '"activities"', '"start_time"')
            if kw in text
        ) >= 2
        # Detect date-based patterns like 2026-04-15
        has_date_patterns = bool(re.search(r"\d{4}-\d{2}-\d{2}", text))

        if (day_pattern_count >= 1 and (has_time_slots or has_activity_markers)) \
                or has_json_markers \
                or (has_date_patterns and has_activity_markers):
            remaining = total_days - planned_count
            return (
                "[状态同步提醒]\n"
                f"你刚刚已经给出了逐日行程安排，但 `daily_plans` 仍只有 {planned_count}/{total_days} 天。"
                f"还需要写入 {remaining} 天的行程。"
                "请立即调用 `update_plan_state(field=\"daily_plans\", value=[...])` "
                "把你刚才描述的每一天行程以结构化 JSON 写入。"
                "每天必须包含 day、date、activities（含 name/location/start_time/end_time/category/cost）。"
                "可以一次性传入 list[dict] 写入全部天数，也可以逐天传入单个 dict 追加。"
            )
        return None

    def _should_skip_redundant_update(self, tool_call: ToolCall) -> bool:
        if tool_call.name != "update_plan_state" or self.plan is None:
            return False
        field = tool_call.arguments.get("field")
        if not isinstance(field, str):
            return False
        return is_redundant_update_plan_state(
            self.plan,
            field=field,
            value=tool_call.arguments.get("value"),
        )
