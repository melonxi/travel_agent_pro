# backend/agent/loop.py
from __future__ import annotations

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
            tools = tools_override or self.tool_engine.get_tools_for_phase(current_phase)
            original_user_message = self._extract_original_user_message(messages)

            for iteration in range(self.max_retries):  # safety limit on loop iterations
                with tracer.start_as_current_span("agent_loop.iteration") as iter_span:
                    iter_span.set_attribute(AGENT_ITERATION, iteration)

                    await self.hooks.run(
                        "before_llm_call", messages=messages, phase=current_phase
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
                        if full_text:
                            messages.append(
                                Message(role=Role.ASSISTANT, content=full_text)
                            )
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
                        tools = self.tool_engine.get_tools_for_phase(current_phase)
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
                        tools = self.tool_engine.get_tools_for_phase(current_phase)
                        continue

                    if (
                        saw_state_update
                        and self.phase_router is not None
                        and self.plan is not None
                    ):
                        phase_changed = self.phase_router.check_and_apply_transition(
                            self.plan
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
                            tools = self.tool_engine.get_tools_for_phase(current_phase)
                            continue

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
                self.plan, phase_prompt, user_summary
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
                    Message(role=Role.SYSTEM, content=f"[前序阶段摘要]\n{summary}")
                )

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
