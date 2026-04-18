# backend/agent/loop.py
from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncIterator

from opentelemetry import trace

from run import IterationProgress

from agent.hooks import HookManager
from agent.narration import compute_narration
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from telemetry.attributes import AGENT_PHASE, AGENT_ITERATION
from tools.engine import ToolEngine
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES


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
        memory_enabled: bool = True,
        user_id: str = "default_user",
        compression_events: list[dict] | None = None,
        reflection: Any | None = None,
        tool_choice_decider: Any | None = None,
        guardrail: Any | None = None,
        parallel_tool_execution: bool = True,
        cancel_event: asyncio.Event | None = None,
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
        self.memory_enabled = memory_enabled
        self.user_id = user_id
        self.compression_events: list[dict] = (
            compression_events if compression_events is not None else []
        )
        self.reflection = reflection
        self.tool_choice_decider = tool_choice_decider
        self.guardrail = guardrail
        self.parallel_tool_execution = parallel_tool_execution
        self._parallel_group_counter: int = 0
        self._prev_phase3_step: str | None = None
        self.cancel_event = cancel_event
        self._progress: IterationProgress = IterationProgress.NO_OUTPUT
        self._recent_search_queries: list[str] = []

    @property
    def progress(self) -> IterationProgress:
        return self._progress

    def _check_cancelled(self) -> None:
        if self.cancel_event and self.cancel_event.is_set():
            from llm.errors import LLMError, LLMErrorCode

            raise LLMError(
                code=LLMErrorCode.TRANSIENT,
                message="用户取消了本轮生成",
                retryable=False,
                provider=getattr(self.llm, "provider_name", "unknown"),
                model=getattr(self.llm, "model", "unknown"),
                failure_phase="cancelled",
            )

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

            iteration_idx = 0
            prev_iteration_had_tools = False
            phase_changed_in_prev_iteration = False
            for iteration in range(self.max_retries):  # safety limit on loop iterations
                self._check_cancelled()
                self._progress = IterationProgress.NO_OUTPUT
                with tracer.start_as_current_span("agent_loop.iteration") as iter_span:
                    iter_span.set_attribute(AGENT_ITERATION, iteration)

                    await self.hooks.run(
                        "before_llm_call",
                        messages=messages,
                        phase=current_phase,
                        tools=tools,
                    )

                    # Yield pending compression events from hook
                    if self.compression_events:
                        yield LLMChunk(
                            type=ChunkType.AGENT_STATUS,
                            agent_status={"stage": "compacting"},
                        )
                    while self.compression_events:
                        info = self.compression_events.pop(0)
                        yield LLMChunk(
                            type=ChunkType.CONTEXT_COMPRESSION,
                            compression_info=info,
                        )

                    stage = (
                        "summarizing"
                        if prev_iteration_had_tools
                        and not phase_changed_in_prev_iteration
                        else "thinking"
                    )
                    prev_iteration_had_tools = False
                    phase_changed_in_prev_iteration = False

                    hint = compute_narration(self.plan) if self.plan else None
                    yield LLMChunk(
                        type=ChunkType.AGENT_STATUS,
                        agent_status={
                            "stage": stage,
                            "iteration": iteration_idx,
                            "hint": hint,
                        },
                    )
                    iteration_idx += 1

                    if self.reflection is not None and self.plan is not None:
                        reflection_msg = self.reflection.check_and_inject(
                            messages,
                            self.plan,
                            self._prev_phase3_step,
                        )
                        if reflection_msg:
                            messages.append(
                                Message(role=Role.SYSTEM, content=reflection_msg)
                            )
                        self._prev_phase3_step = getattr(self.plan, "phase3_step", None)

                    tool_calls: list[ToolCall] = []
                    text_chunks: list[str] = []
                    tool_choice = "auto"
                    if self.tool_choice_decider is not None and self.plan is not None:
                        tool_choice = self.tool_choice_decider.decide(
                            self.plan,
                            messages,
                            current_phase,
                        )

                    chat_kwargs = {
                        "tools": tools,
                        "stream": True,
                    }
                    if tool_choice != "auto":
                        chat_kwargs["tool_choice"] = tool_choice

                    async for chunk in self.llm.chat(messages, **chat_kwargs):
                        self._check_cancelled()
                        if chunk.type == ChunkType.TEXT_DELTA:
                            if self._progress == IterationProgress.NO_OUTPUT:
                                self._progress = IterationProgress.PARTIAL_TEXT
                            text_chunks.append(chunk.content or "")
                            yield chunk
                        elif chunk.type == ChunkType.USAGE:
                            yield chunk
                        elif (
                            chunk.type == ChunkType.TOOL_CALL_START and chunk.tool_call
                        ):
                            self._progress = IterationProgress.PARTIAL_TOOL_CALL
                            if chunk.tool_call.human_label is None:
                                tool_def = self.tool_engine.get_tool(
                                    chunk.tool_call.name
                                )
                                if tool_def is not None:
                                    chunk.tool_call.human_label = tool_def.human_label
                            tool_calls.append(chunk.tool_call)
                            yield chunk
                        elif chunk.type == ChunkType.DONE:
                            pass

                    # If no tool calls, we're done — the LLM gave a final text response
                    if not tool_calls:
                        full_text = "".join(text_chunks)
                        repair_message = self._build_phase3_state_repair_message(
                            current_phase=current_phase,
                            assistant_text=full_text,
                            repair_hints_used=repair_hints_used,
                        ) or self._build_phase5_state_repair_message(
                            current_phase=current_phase,
                            assistant_text=full_text,
                            repair_hints_used=repair_hints_used,
                        )
                        if full_text:
                            messages.append(
                                Message(role=Role.ASSISTANT, content=full_text)
                            )
                        if repair_message:
                            messages.append(
                                Message(role=Role.SYSTEM, content=repair_message)
                            )
                            # repair_hints_used is now managed inside the
                            # repair builder methods themselves.
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
                    idx = 0
                    emitted_indices: set[int] = set()
                    while idx < len(tool_calls):
                        tc = tool_calls[idx]
                        result = self._pre_execution_skip_result(tc)
                        if result is None and self._is_parallel_read_call(tc):
                            read_batch: list[tuple[int, ToolCall]] = []
                            scan_idx = idx
                            while scan_idx < len(tool_calls):
                                scan_tc = tool_calls[scan_idx]
                                if self._pre_execution_skip_result(
                                    scan_tc
                                ) is not None or not self._is_parallel_read_call(
                                    scan_tc
                                ):
                                    break
                                read_batch.append((scan_idx, scan_tc))
                                scan_idx += 1

                            self._parallel_group_counter += 1
                            current_group = self._parallel_group_counter

                            batch_results = await self.tool_engine.execute_batch(
                                [call for _, call in read_batch]
                            )
                            for (batch_idx, batch_tc), batch_result in zip(
                                read_batch,
                                batch_results,
                            ):
                                if batch_result.metadata is None:
                                    batch_result.metadata = {}
                                batch_result.metadata["parallel_group"] = current_group
                                result = self._validate_tool_output(
                                    batch_tc,
                                    batch_result,
                                )
                                if (
                                    batch_tc.name in PLAN_WRITER_TOOL_NAMES
                                    and result.status == "success"
                                ):
                                    saw_state_update = True

                                if self._is_parallel_read_call(batch_tc):
                                    if (
                                        self._progress
                                        != IterationProgress.TOOLS_WITH_WRITES
                                    ):
                                        self._progress = (
                                            IterationProgress.TOOLS_READ_ONLY
                                        )
                                else:
                                    self._progress = IterationProgress.TOOLS_WITH_WRITES

                                messages.append(
                                    Message(
                                        role=Role.TOOL,
                                        tool_result=result,
                                    )
                                )
                                emitted_indices.add(batch_idx)

                                yield LLMChunk(type=ChunkType.KEEPALIVE)

                                await self.hooks.run(
                                    "after_tool_call",
                                    tool_name=batch_tc.name,
                                    tool_call=batch_tc,
                                    result=result,
                                )

                                yield LLMChunk(
                                    type=ChunkType.TOOL_RESULT,
                                    tool_result=result,
                                )

                            idx = scan_idx
                            continue

                        if result is None:
                            self._check_cancelled()
                            result = await self.tool_engine.execute(tc)
                            result = self._validate_tool_output(tc, result)
                        if (
                            tc.name in PLAN_WRITER_TOOL_NAMES
                            and result.status == "success"
                        ):
                            saw_state_update = True

                        if self._is_parallel_read_call(tc):
                            if self._progress != IterationProgress.TOOLS_WITH_WRITES:
                                self._progress = IterationProgress.TOOLS_READ_ONLY
                        else:
                            self._progress = IterationProgress.TOOLS_WITH_WRITES

                        messages.append(
                            Message(
                                role=Role.TOOL,
                                tool_result=result,
                            )
                        )
                        emitted_indices.add(idx)

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
                            for skipped_idx, skipped_tc in enumerate(
                                tool_calls[idx + 1 :],
                                start=idx + 1,
                            ):
                                if skipped_idx in emitted_indices:
                                    continue
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
                        idx += 1

                    if needs_rebuild:
                        prev_iteration_had_tools = True
                        phase_changed_in_prev_iteration = True
                        phase_after_batch = (
                            self.plan.phase if self.plan is not None else current_phase
                        )
                        yield LLMChunk(
                            type=ChunkType.PHASE_TRANSITION,
                            phase_info={
                                "from_phase": phase_before_batch,
                                "to_phase": phase_after_batch,
                                "from_step": phase3_step_before_batch,
                                "to_step": getattr(self.plan, "phase3_step", None),
                                "reason": "backtrack",
                            },
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
                        prev_iteration_had_tools = True
                        phase_changed_in_prev_iteration = True
                        yield LLMChunk(
                            type=ChunkType.PHASE_TRANSITION,
                            phase_info={
                                "from_phase": phase_before_batch,
                                "to_phase": phase_after_batch,
                                "from_step": phase3_step_before_batch,
                                "to_step": getattr(self.plan, "phase3_step", None),
                                "reason": "plan_tool_direct",
                            },
                        )
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
                            prev_iteration_had_tools = True
                            phase_changed_in_prev_iteration = True
                            yield LLMChunk(
                                type=ChunkType.PHASE_TRANSITION,
                                phase_info={
                                    "from_phase": phase_before_batch,
                                    "to_phase": phase_after_batch,
                                    "from_step": phase3_step_before_batch,
                                    "to_step": getattr(self.plan, "phase3_step", None),
                                    "reason": "check_and_apply_transition",
                                },
                            )
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
                        phase_changed_in_prev_iteration = True
                        messages[
                            :
                        ] = await self._rebuild_messages_for_phase3_step_change(
                            messages=messages,
                            original_user_message=original_user_message,
                        )
                        tools = self.tool_engine.get_tools_for_phase(
                            current_phase,
                            self.plan,
                        )

                    prev_iteration_had_tools = True

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
            raise RuntimeError(
                "Phase-aware rebuild requires router/context/plan/memory"
            )

        phase_prompt = self.phase_router.get_prompt_for_plan(self.plan)
        memory_context, _recalled_ids, *_ = (
            await self.memory_mgr.generate_context(self.user_id, self.plan)
            if self.memory_enabled
            else ("暂无相关用户记忆", [], 0, 0, 0)
        )
        rebuilt = [
            self.context_manager.build_system_message(
                self.plan,
                phase_prompt,
                memory_context,
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
            rebuilt.append(
                Message(
                    role=Role.ASSISTANT,
                    content=self.context_manager.build_phase_handoff_note(
                        plan=self.plan,
                        from_phase=from_phase,
                        to_phase=to_phase,
                    ),
                )
            )

        # Preserve the triggering user intent across hard-boundary rebuilds.
        # Some providers reject assistant-only message sequences, and the next
        # phase still needs the user's latest request as the active task.
        rebuilt.append(self._copy_message(original_user_message))
        return rebuilt

    async def _rebuild_messages_for_phase3_step_change(
        self,
        messages: list[Message],
        original_user_message: Message,
    ) -> list[Message]:
        """Phase 3 子阶段变化时重建 system message（不含 handoff / backtrack note）。"""
        if (
            self.phase_router is None
            or self.context_manager is None
            or self.plan is None
            or self.memory_mgr is None
        ):
            raise RuntimeError(
                "Phase3 step rebuild requires router/context/plan/memory"
            )

        phase_prompt = self.phase_router.get_prompt_for_plan(self.plan)
        memory_context, _recalled_ids, *_ = (
            await self.memory_mgr.generate_context(self.user_id, self.plan)
            if self.memory_enabled
            else ("暂无相关用户记忆", [], 0, 0, 0)
        )
        return [
            self.context_manager.build_system_message(
                self.plan,
                phase_prompt,
                memory_context,
                available_tools=self._current_tool_names(self.plan.phase),
            ),
            self._copy_message(original_user_message),
        ]

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

    def _pre_execution_skip_result(self, tool_call: ToolCall) -> ToolResult | None:
        if self._should_skip_redundant_update(tool_call):
            query = (tool_call.arguments or {}).get("query", "")
            return self._build_skipped_tool_result(
                tool_call.id,
                error=f'相同查询 "{query}" 已搜索过多次且未得到新结果。',
                error_code="REDUNDANT_SEARCH",
                suggestion=(
                    "请不要重复搜索相同内容。"
                    "如果搜索没有找到需要的信息，请换一个查询方向，"
                    "或直接根据已有信息推进规划（调用状态写入工具写入产物）。"
                ),
            )

        if self.guardrail is None:
            return None

        guardrail_result = self.guardrail.validate_input(tool_call)
        if guardrail_result.allowed:
            return None
        return self._build_skipped_tool_result(
            tool_call.id,
            error=guardrail_result.reason,
            error_code="GUARDRAIL_REJECTED",
            suggestion=guardrail_result.reason,
        )

    def _validate_tool_output(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        if self.guardrail is None or result.status != "success":
            return result

        output_check = self.guardrail.validate_output(tool_call.name, result.data)
        if output_check.level != "warn" or not output_check.reason:
            return result
        return ToolResult(
            tool_call_id=result.tool_call_id,
            status=result.status,
            data=result.data,
            metadata=result.metadata,
            suggestion=output_check.reason,
        )

    def _is_parallel_read_call(self, tool_call: ToolCall) -> bool:
        if not self.parallel_tool_execution:
            return False
        tool_def = self.tool_engine.get_tool(tool_call.name)
        return tool_def is None or tool_def.side_effect != "write"

    def _current_tool_names(self, phase: int | None = None) -> list[str]:
        target_phase = (
            phase
            if phase is not None
            else (self.plan.phase if self.plan is not None else None)
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
            # Allow a second attempt with a stronger key if the LLM
            # ignored the first repair hint and still hasn't written state.
            stronger_key = f"p3_{step}_retry"
            if stronger_key in repair_hints_used:
                return None
            repair_key = stronger_key

        _SKELETON_SIGNALS = ("骨架", "轻松版", "平衡版", "高密度版", "深度版", "跳岛")
        _has_skeleton_signals = any(
            token in text for token in _SKELETON_SIGNALS
        ) or bool(re.search(r"方案\s*[A-C1-3]", text))

        if (
            step == "brief"
            and not self.plan.trip_brief
            and any(
                token in text
                for token in ("画像", "偏好", "约束", "预算", "日期", "旅行")
            )
        ):
            repair_hints_used.add(repair_key)
            return (
                "[状态同步提醒]\n"
                "你刚刚已经完成了旅行画像说明，但 `trip_brief` 仍为空。"
                "请先调用 `set_trip_brief(fields={goal, pace, departure_city})`"
                " 写入画像核心字段；must_do 用 `add_preferences` 写入，"
                "avoid 用 `add_constraints` 写入，预算用 `update_trip_basics` 写入。"
                "写完后再继续，不要重复整段面向用户解释。"
            )

        if step == "candidate":
            # Case 1: shortlist 为空，Agent 描述了候选筛选
            if not self.plan.shortlist and any(
                token in text for token in ("候选", "推荐", "不建议", "why", "why_not")
            ):
                repair_hints_used.add(repair_key)
                if not self.plan.candidate_pool:
                    return (
                        "[状态同步提醒]\n"
                        "你刚刚已经给出了候选筛选结果，但 `candidate_pool` 仍为空。"
                        "请先调用 `set_candidate_pool(pool=[...])` 写入候选全集，"
                        "再调用 `set_shortlist(items=[...])` 写入第一轮筛选结果。"
                        "写入 shortlist 后系统会自动推进子阶段。"
                    )
                return (
                    "[状态同步提醒]\n"
                    "你刚刚已经给出了候选筛选结果，但 `shortlist` 仍为空。"
                    "请先调用 `set_shortlist(items=[...])` 写入第一轮筛选结果。"
                    "写入 shortlist 后系统会自动推进子阶段。"
                )

            # Case 2: Agent 在 candidate 阶段跳阶描述了骨架方案
            if not self.plan.skeleton_plans and _has_skeleton_signals:
                repair_hints_used.add(repair_key)
                return (
                    "[状态同步提醒]\n"
                    "你刚刚已经给出了骨架方案，但 `skeleton_plans` 仍为空。"
                    "请先调用 `set_skeleton_plans(plans=[...])`"
                    " 写入结构化骨架方案列表（每个方案必须包含 `id` 和 `name`）。"
                    '如果用户已经明确选中某套方案，再调用 `select_skeleton(id="...")`。'
                    "写入后系统会自动推进子阶段。"
                )

        if (
            step == "skeleton"
            and not self.plan.skeleton_plans
            and _has_skeleton_signals
        ):
            repair_hints_used.add(repair_key)
            return (
                "[状态同步提醒]\n"
                "你刚刚已经给出了 2-3 套骨架方案，但 `skeleton_plans` 仍为空。"
                "请先调用 `set_skeleton_plans(plans=[...])`"
                " 写入结构化骨架方案列表。"
                '如果用户已经明确选中某套方案，再调用 `select_skeleton(id="...")`，'
                "系统会自动推进到 lock 子阶段。"
            )

        if step == "lock":
            # Relaxed condition: check each category independently
            missing_fields: list[str] = []
            if not self.plan.transport_options and any(
                t in text for t in ("航班", "火车", "高铁", "交通")
            ):
                missing_fields.append("`set_transport_options(options=[...])`")
            if (
                not self.plan.accommodation_options
                and not self.plan.accommodation
                and any(t in text for t in ("住宿", "酒店", "民宿", "旅馆"))
            ):
                missing_fields.append(
                    "`set_accommodation_options(options=[...])` 或 `set_accommodation(area=...)`"
                )
            if not self.plan.risks and any(t in text for t in ("风险", "注意", "天气")):
                missing_fields.append("`set_risks(list=[...])`")
            if not self.plan.alternatives and any(
                t in text for t in ("备选", "替代", "雨天")
            ):
                missing_fields.append("`set_alternatives(list=[...])`")
            if missing_fields:
                repair_hints_used.add(repair_key)
                fields_str = "、".join(missing_fields)
                return (
                    "[状态同步提醒]\n"
                    f"你刚刚已经给出了锁定阶段建议，但以下字段仍未写入：{fields_str}。"
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
        """Detect when Phase 5 LLM outputs itinerary text but forgets to call plan tools."""
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
        day_pattern_count = len(
            re.findall(
                r"第\s*[1-9一二三四五六七八九十]\s*天|Day\s*\d|DAY\s*\d",
                text,
            )
        )
        has_time_slots = bool(re.search(r"\d{1,2}:\d{2}", text))
        has_activity_markers = any(
            kw in text
            for kw in ("活动", "景点", "行程", "安排", "上午", "下午", "晚上", "餐厅")
        )
        # Also detect JSON-schema style itinerary output
        has_json_markers = (
            sum(
                1
                for kw in ('"day"', '"date"', '"activities"', '"start_time"')
                if kw in text
            )
            >= 2
        )
        # Detect date-based patterns like 2026-04-15
        has_date_patterns = bool(re.search(r"\d{4}-\d{2}-\d{2}", text))

        if (
            (day_pattern_count >= 1 and (has_time_slots or has_activity_markers))
            or has_json_markers
            or (has_date_patterns and has_activity_markers)
        ):
            repair_hints_used.add(repair_key)
            remaining = total_days - planned_count
            return (
                "[状态同步提醒]\n"
                f"你刚刚已经给出了逐日行程安排，但 `daily_plans` 仍只有 {planned_count}/{total_days} 天。"
                '请立即调用 `save_day_plan(mode="create", day=缺失天数, date=对应日期, activities=活动列表)` 逐天保存缺失天数，'
                "或在需要一次性完整覆盖时调用 `replace_all_day_plans(days=完整天数列表)`。"
                "`optimize_day_route` 只做路线辅助，不能替代状态写入。"
            )
        return None

    def _should_skip_redundant_update(self, tool_call: ToolCall) -> bool:
        """Detect and skip repeated search queries.

        If the same search query has been used >= 2 times recently within this
        agent run, skip the call and return a helpful message to the LLM.
        """
        _SEARCH_TOOLS = {
            "web_search",
            "xiaohongshu_search",
            "xiaohongshu_search_notes",
            "quick_travel_search",
        }
        if tool_call.name not in _SEARCH_TOOLS:
            return False

        argument_name = (
            "keyword" if tool_call.name == "xiaohongshu_search_notes" else "query"
        )
        query = (tool_call.arguments or {}).get(argument_name, "")
        if not isinstance(query, str) or not query.strip():
            return False

        normalized = query.strip().lower()

        # Count how many times this exact query has been seen
        count = sum(1 for q in self._recent_search_queries if q == normalized)
        # Record current call
        self._recent_search_queries.append(normalized)
        # Keep list bounded
        if len(self._recent_search_queries) > 20:
            self._recent_search_queries = self._recent_search_queries[-20:]

        return count >= 2
