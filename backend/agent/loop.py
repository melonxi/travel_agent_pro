# backend/agent/loop.py
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, AsyncIterator

from opentelemetry import trace

from run import IterationProgress

from agent.execution.llm_turn import LlmTurnOutcome, run_llm_turn
from agent.hooks import HookManager
from agent.execution.limits import AgentLoopLimits
from agent.execution.loop_config import AgentLoopConfig, AgentLoopDeps
from agent.execution.message_rebuild import (
    build_backtrack_notice,
    copy_message,
    current_tool_names,
    extract_original_user_message,
    rebuild_messages_for_phase3_step_change,
    rebuild_messages_for_phase_change,
)
from agent.execution.phase_rebuild_callback import invoke_phase_rebuild_callback
from agent.execution.phase_transition import (
    PhaseTransitionRequest,
    detect_phase_transition,
)
from agent.execution.repair_hints import (
    RepairHintOutcome,
    build_phase3_state_repair_message,
    build_phase5_state_repair_message,
)
from agent.execution.tool_batches import ToolBatchOutcome, execute_tool_batch
from agent.execution.tool_invocation import (
    SearchHistoryTracker,
    build_skipped_tool_result,
    is_backtrack_result,
    is_parallel_read_call,
    pre_execution_skip_result,
    validate_tool_output,
)
from agent.internal_tasks import InternalTask
from agent.phase5.parallel import (
    run_parallel_phase5_orchestrator,
    should_enter_parallel_phase5_at_iteration_boundary,
    should_enter_parallel_phase5_now,
    should_use_parallel_phase5,
)
from agent.types import Message, Role, ToolCall, ToolResult
from llm.types import ChunkType, LLMChunk
from telemetry.attributes import AGENT_PHASE, AGENT_ITERATION
from tools.engine import ToolEngine
from config import Phase5ParallelConfig


@dataclass
class PhaseTransitionOutcome:
    messages: list[Message]
    current_phase: int
    tools: list[dict]


class AgentLoop:
    def __init__(
        self,
        llm: Any | None = None,
        tool_engine: ToolEngine | None = None,
        hooks: HookManager | None = None,
        deps: AgentLoopDeps | None = None,
        config: AgentLoopConfig | None = None,
        max_retries: int | None = 3,
        max_iterations: int | None = None,
        max_llm_errors: int | None = None,
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
        phase5_parallel_config: Phase5ParallelConfig | None = None,
        internal_task_events: list[InternalTask] | None = None,
        on_phase_rebuild: Callable[..., Awaitable[None]] | None = None,
    ):
        if deps is not None:
            llm = deps.llm
            tool_engine = deps.tool_engine
            hooks = deps.hooks
            phase_router = deps.phase_router
            context_manager = deps.context_manager
            plan = deps.plan
            llm_factory = deps.llm_factory
            memory_mgr = deps.memory_mgr
            reflection = deps.reflection
            tool_choice_decider = deps.tool_choice_decider
            guardrail = deps.guardrail

        if config is not None:
            max_iterations = config.max_iterations
            max_llm_errors = config.max_llm_errors
            memory_enabled = config.memory_enabled
            user_id = config.user_id
            compression_events = config.compression_events
            parallel_tool_execution = config.parallel_tool_execution
            cancel_event = config.cancel_event
            phase5_parallel_config = config.phase5_parallel_config
            internal_task_events = config.internal_task_events

        if llm is None or tool_engine is None or hooks is None:
            raise TypeError("AgentLoop requires llm, tool_engine, and hooks")

        self.llm = llm
        self.tool_engine = tool_engine
        self.hooks = hooks
        self.limits = AgentLoopLimits.from_constructor_args(
            max_iterations=max_iterations,
            max_retries=max_retries,
            max_llm_errors=max_llm_errors,
        )
        self.max_iterations = self.limits.max_iterations
        self.max_retries = self.limits.max_iterations
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
        self.phase5_parallel_config = phase5_parallel_config
        self.internal_task_events = (
            internal_task_events if internal_task_events is not None else []
        )
        self._progress: IterationProgress = IterationProgress.NO_OUTPUT
        self._search_history = SearchHistoryTracker()
        self.on_phase_rebuild = on_phase_rebuild

    @property
    def progress(self) -> IterationProgress:
        return self._progress

    def _drain_internal_task_events(self) -> list[InternalTask]:
        events = list(self.internal_task_events)
        self.internal_task_events.clear()
        return events

    async def _run_after_tool_result_hook(
        self,
        *,
        tool_name: str,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> AsyncIterator[LLMChunk]:
        hook_task = asyncio.create_task(
            self.hooks.run(
                "after_tool_result",
                tool_name=tool_name,
                tool_call=tool_call,
                result=result,
            )
        )
        try:
            while not hook_task.done():
                for task in self._drain_internal_task_events():
                    yield LLMChunk(type=ChunkType.INTERNAL_TASK, internal_task=task)
                await asyncio.sleep(0.05)
            await hook_task
        except Exception:
            hook_task.cancel()
            with suppress(asyncio.CancelledError):
                await hook_task
            raise

        for task in self._drain_internal_task_events():
            yield LLMChunk(type=ChunkType.INTERNAL_TASK, internal_task=task)

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

    @staticmethod
    def should_use_parallel_phase5(
        plan: Any | None,
        config: Phase5ParallelConfig | None,
    ) -> bool:
        return should_use_parallel_phase5(plan, config)

    async def _run_parallel_phase5_orchestrator(
        self,
        *,
        messages: list[Message],
        original_user_message: Message,
    ) -> AsyncIterator[LLMChunk]:
        _handoff: Any | None = None

        def _capture_handoff(handoff: Any) -> None:
            nonlocal _handoff
            _handoff = handoff

        async for chunk in run_parallel_phase5_orchestrator(
            plan=self.plan,
            llm=self.llm,
            tool_engine=self.tool_engine,
            config=self.phase5_parallel_config,
            on_handoff=_capture_handoff,
        ):
            yield chunk

        if _handoff is None or not _handoff.dayplans:
            yield LLMChunk(type=ChunkType.DONE)
            return

        commit_call = ToolCall(
            id="internal_phase5_parallel_commit",
            name="replace_all_day_plans",
            arguments={"days": list(_handoff.dayplans)},
            human_label="写入并行逐日行程",
        )
        # Preserve the same message-history shape as a normal assistant tool call.
        # _execute_tool_batch() appends the matching TOOL message and then existing
        # phase-transition detection can reason over a standard write-tool batch.
        messages.append(
            Message(
                role=Role.ASSISTANT,
                content=None,
                tool_calls=[commit_call],
            )
        )

        phase_before_batch = self.plan.phase if self.plan is not None else 5
        phase3_step_before_batch = (
            getattr(self.plan, "phase3_step", None) if self.plan is not None else None
        )
        batch_outcome: ToolBatchOutcome | None = None
        async for batch_item in self._execute_tool_batch(
            tool_calls=[commit_call],
            messages=messages,
        ):
            if isinstance(batch_item, LLMChunk):
                yield batch_item
            else:
                batch_outcome = batch_item

        if batch_outcome is None:
            raise RuntimeError("Parallel Phase 5 commit finished without an outcome")

        if not batch_outcome.saw_state_update:
            commit_result = None
            for message in reversed(messages):
                result = message.tool_result
                if result and result.tool_call_id == commit_call.id:
                    commit_result = result
                    break
            detail = (
                commit_result.error
                if commit_result is not None and commit_result.error
                else "replace_all_day_plans 未成功写入状态"
            )
            suggestion = (
                f" {commit_result.suggestion}"
                if commit_result is not None and commit_result.suggestion
                else ""
            )
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA,
                content=(
                    "\n\n⚠️ 并行行程写入失败，当前行程尚未保存到规划状态。"
                    f"原因：{detail}{suggestion}"
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return

        transition_detection = await detect_phase_transition(
            plan=self.plan,
            phase_router=self.phase_router,
            hooks=self.hooks,
            batch_outcome=batch_outcome,
            phase_before_batch=phase_before_batch,
            phase3_step_before_batch=phase3_step_before_batch,
            current_phase=phase_before_batch,
            drain_internal_task_events=self._drain_internal_task_events,
        )
        for task in transition_detection.internal_tasks:
            yield LLMChunk(type=ChunkType.INTERNAL_TASK, internal_task=task)

        if transition_detection.request is not None:
            async for transition_item in self._handle_phase_transition(
                messages=messages,
                request=transition_detection.request,
                original_user_message=original_user_message,
            ):
                if isinstance(transition_item, LLMChunk):
                    yield transition_item

        yield LLMChunk(type=ChunkType.DONE)

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
            for iteration in range(
                self.max_iterations
            ):  # safety limit on loop iterations
                # Loop-top guard covers both cold start and hot switch after
                # a write tool upgrades phase to 5 mid-run.
                if should_enter_parallel_phase5_now(
                    self.plan,
                    self.phase5_parallel_config,
                ):
                    async for chunk in self._run_parallel_phase5_orchestrator(
                        messages=messages,
                        original_user_message=original_user_message,
                    ):
                        yield chunk
                    return

                self._check_cancelled()
                self._progress = IterationProgress.NO_OUTPUT
                with tracer.start_as_current_span("agent_loop.iteration") as iter_span:
                    iter_span.set_attribute(AGENT_ITERATION, iteration)

                    turn_outcome: LlmTurnOutcome | None = None
                    async for turn_item in run_llm_turn(
                        llm=self.llm,
                        tool_engine=self.tool_engine,
                        hooks=self.hooks,
                        messages=messages,
                        tools=tools,
                        current_phase=current_phase,
                        plan=self.plan,
                        reflection=self.reflection,
                        tool_choice_decider=self.tool_choice_decider,
                        compression_events=self.compression_events,
                        iteration_idx=iteration_idx,
                        previous_iteration_had_tools=prev_iteration_had_tools,
                        phase_changed_in_previous_iteration=phase_changed_in_prev_iteration,
                        previous_phase3_step=self._prev_phase3_step,
                        check_cancelled=self._check_cancelled,
                        update_progress=lambda progress: setattr(
                            self,
                            "_progress",
                            progress,
                        ),
                    ):
                        if isinstance(turn_item, LLMChunk):
                            yield turn_item
                        else:
                            turn_outcome = turn_item

                    if turn_outcome is None:
                        raise RuntimeError("LLM turn finished without an outcome")

                    iteration_idx = turn_outcome.next_iteration_idx
                    self._prev_phase3_step = turn_outcome.previous_phase3_step
                    self._progress = turn_outcome.progress
                    prev_iteration_had_tools = False
                    phase_changed_in_prev_iteration = False
                    tool_calls = turn_outcome.tool_calls
                    text_chunks = turn_outcome.text_chunks

                    # If no tool calls, we're done — the LLM gave a final text response
                    if not tool_calls:
                        full_text = "".join(text_chunks)
                        repair_outcome = self._build_phase3_state_repair_message(
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
                                Message(
                                    role=Role.ASSISTANT,
                                    content=full_text,
                                    provider_state=turn_outcome.provider_state,
                                )
                            )
                        if repair_outcome:
                            messages.append(
                                Message(
                                    role=Role.SYSTEM,
                                    content=repair_outcome.message,
                                )
                            )
                            repair_hints_used.add(repair_outcome.key)
                            continue
                        yield LLMChunk(type=ChunkType.DONE)
                        return

                    # Record assistant message with tool calls
                    messages.append(
                        Message(
                            role=Role.ASSISTANT,
                            content="".join(text_chunks) or None,
                            tool_calls=tool_calls,
                            provider_state=turn_outcome.provider_state,
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
                    batch_outcome: ToolBatchOutcome | None = None
                    async for batch_item in self._execute_tool_batch(
                        tool_calls=tool_calls,
                        messages=messages,
                    ):
                        if isinstance(batch_item, LLMChunk):
                            yield batch_item
                        else:
                            batch_outcome = batch_item

                    if batch_outcome is None:
                        raise RuntimeError(
                            "Tool batch execution finished without an outcome"
                        )

                    transition_detection = await detect_phase_transition(
                        plan=self.plan,
                        phase_router=self.phase_router,
                        hooks=self.hooks,
                        batch_outcome=batch_outcome,
                        phase_before_batch=phase_before_batch,
                        phase3_step_before_batch=phase3_step_before_batch,
                        current_phase=current_phase,
                        drain_internal_task_events=self._drain_internal_task_events,
                    )
                    for task in transition_detection.internal_tasks:
                        yield LLMChunk(
                            type=ChunkType.INTERNAL_TASK,
                            internal_task=task,
                        )

                    if transition_detection.request is not None:
                        prev_iteration_had_tools = True
                        phase_changed_in_prev_iteration = True
                        transition_outcome: PhaseTransitionOutcome | None = None
                        async for transition_item in self._handle_phase_transition(
                            messages=messages,
                            request=transition_detection.request,
                            original_user_message=original_user_message,
                        ):
                            if isinstance(transition_item, LLMChunk):
                                yield transition_item
                            else:
                                transition_outcome = transition_item
                        if transition_outcome is None:
                            raise RuntimeError(
                                "Phase transition finished without an outcome"
                            )
                        messages[:] = transition_outcome.messages
                        current_phase = transition_outcome.current_phase
                        tools = transition_outcome.tools
                        continue

                    phase3_step_after_batch = (
                        transition_detection.phase3_step_after_batch
                    )
                    if phase3_step_after_batch != phase3_step_before_batch:
                        phase_changed_in_prev_iteration = True
                        messages[
                            :
                        ] = await self._rebuild_messages_for_phase3_step_change(
                            messages=messages,
                            original_user_message=original_user_message,
                            from_step=phase3_step_before_batch,
                        )
                        tools = self.tool_engine.get_tools_for_phase(
                            current_phase,
                            self.plan,
                        )

                    prev_iteration_had_tools = True

                    # Loop continues — LLM will see tool results and decide next step

            # Boundary case: a write tool in the final iteration may have just
            # promoted phase to 5. Give the parallel orchestrator one more shot
            # before the safety-limit fallback so we don't drop the upgrade.
            if should_enter_parallel_phase5_at_iteration_boundary(
                self.plan,
                self.phase5_parallel_config,
            ):
                async for chunk in self._run_parallel_phase5_orchestrator(
                    messages=messages,
                    original_user_message=original_user_message,
                ):
                    yield chunk
                return

            # Safety limit reached
            yield LLMChunk(
                type=ChunkType.TEXT_DELTA, content="[达到最大循环次数，请重新发送消息]"
            )
            yield LLMChunk(type=ChunkType.DONE)

    async def _execute_tool_batch(
        self,
        *,
        tool_calls: list[ToolCall],
        messages: list[Message],
    ) -> AsyncIterator[LLMChunk | ToolBatchOutcome]:
        async for batch_item in execute_tool_batch(
            tool_calls=tool_calls,
            messages=messages,
            tool_engine=self.tool_engine,
            hooks=self.hooks,
            guardrail=self.guardrail,
            parallel_tool_execution=self.parallel_tool_execution,
            parallel_group_counter=self._parallel_group_counter,
            search_history=self._search_history,
            check_cancelled=self._check_cancelled,
            run_after_tool_result_hook=self._run_after_tool_result_hook,
            current_progress=self._progress,
        ):
            if isinstance(batch_item, ToolBatchOutcome):
                self._parallel_group_counter = batch_item.next_parallel_group_counter
                self._progress = batch_item.progress
            yield batch_item

    async def _handle_phase_transition(
        self,
        *,
        messages: list[Message],
        request: PhaseTransitionRequest,
        original_user_message: Message,
    ) -> AsyncIterator[LLMChunk | PhaseTransitionOutcome]:
        yield LLMChunk(
            type=ChunkType.PHASE_TRANSITION,
            phase_info={
                "from_phase": request.from_phase,
                "to_phase": request.to_phase,
                "from_step": request.from_step,
                "to_step": getattr(self.plan, "phase3_step", None),
                "reason": request.reason,
            },
        )
        rebuilt_messages = await self._rebuild_messages_for_phase_change(
            messages=messages,
            from_phase=request.from_phase,
            to_phase=request.to_phase,
            from_step=request.from_step,
            original_user_message=original_user_message,
            result=request.result,
        )
        yield PhaseTransitionOutcome(
            messages=rebuilt_messages,
            current_phase=request.to_phase,
            tools=self.tool_engine.get_tools_for_phase(request.to_phase, self.plan),
        )

    def _extract_original_user_message(self, messages: list[Message]) -> Message:
        return extract_original_user_message(messages)

    def _copy_message(self, message: Message) -> Message:
        return copy_message(message)

    async def _rebuild_messages_for_phase_change(
        self,
        messages: list[Message],
        from_phase: int,
        to_phase: int,
        from_step: str | None,
        original_user_message: Message,
        result: ToolResult,
    ) -> list[Message]:
        await invoke_phase_rebuild_callback(
            self.on_phase_rebuild, messages=messages, from_phase=from_phase, from_step=from_step
        )
        return await rebuild_messages_for_phase_change(
            phase_router=self.phase_router,
            context_manager=self.context_manager,
            plan=self.plan,
            memory_mgr=self.memory_mgr,
            memory_enabled=self.memory_enabled,
            user_id=self.user_id,
            tool_engine=self.tool_engine,
            from_phase=from_phase,
            to_phase=to_phase,
            original_user_message=original_user_message,
            result=result,
        )

    async def _rebuild_messages_for_phase3_step_change(
        self,
        messages: list[Message],
        original_user_message: Message,
        from_step: str | None,
    ) -> list[Message]:
        await invoke_phase_rebuild_callback(
            self.on_phase_rebuild, messages=messages, from_phase=3, from_step=from_step
        )
        return await rebuild_messages_for_phase3_step_change(
            phase_router=self.phase_router,
            context_manager=self.context_manager,
            plan=self.plan,
            memory_mgr=self.memory_mgr,
            memory_enabled=self.memory_enabled,
            user_id=self.user_id,
            tool_engine=self.tool_engine,
            original_user_message=original_user_message,
        )

    def _build_backtrack_notice(
        self, from_phase: int, to_phase: int, result: ToolResult
    ) -> str:
        return build_backtrack_notice(
            plan=self.plan,
            from_phase=from_phase,
            to_phase=to_phase,
            result=result,
        )

    def _is_backtrack_result(self, result: ToolResult) -> bool:
        return is_backtrack_result(result)

    def _build_skipped_tool_result(
        self,
        tool_call_id: str,
        *,
        error: str,
        error_code: str,
        suggestion: str,
    ) -> ToolResult:
        return build_skipped_tool_result(
            tool_call_id=tool_call_id,
            error=error,
            error_code=error_code,
            suggestion=suggestion,
        )

    def _pre_execution_skip_result(self, tool_call: ToolCall) -> ToolResult | None:
        return pre_execution_skip_result(
            tool_call=tool_call,
            guardrail=self.guardrail,
            search_history=self._search_history,
        )

    def _validate_tool_output(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> ToolResult:
        return validate_tool_output(
            guardrail=self.guardrail,
            tool_call=tool_call,
            result=result,
        )

    def _is_parallel_read_call(self, tool_call: ToolCall) -> bool:
        return is_parallel_read_call(
            parallel_tool_execution=self.parallel_tool_execution,
            tool_engine=self.tool_engine,
            tool_call=tool_call,
        )

    def _current_tool_names(self, phase: int | None = None) -> list[str]:
        return current_tool_names(
            tool_engine=self.tool_engine,
            plan=self.plan,
            phase=phase,
        )

    def _build_phase3_state_repair_message(
        self,
        *,
        current_phase: int,
        assistant_text: str,
        repair_hints_used: set[str],
    ) -> RepairHintOutcome | None:
        return build_phase3_state_repair_message(
            plan=self.plan,
            current_phase=current_phase,
            assistant_text=assistant_text,
            repair_hints_used=repair_hints_used,
        )

    def _build_phase5_state_repair_message(
        self,
        *,
        current_phase: int,
        assistant_text: str,
        repair_hints_used: set[str],
    ) -> RepairHintOutcome | None:
        return build_phase5_state_repair_message(
            plan=self.plan,
            current_phase=current_phase,
            assistant_text=assistant_text,
            repair_hints_used=repair_hints_used,
        )

    def _should_skip_redundant_update(self, tool_call: ToolCall) -> bool:
        return self._search_history.should_skip_redundant_update(tool_call)
