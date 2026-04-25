from __future__ import annotations

from dataclasses import replace

from agent.loop import AgentLoop
from agent.reflection import ReflectionInjector
from agent.tool_choice import ToolChoiceDecider
from harness.guardrail import ToolGuardrail

from api.orchestration.agent.hooks import build_agent_hooks
from api.orchestration.agent.tools import build_tool_engine


def build_agent(
    *,
    plan,
    user_id: str,
    config,
    sessions: dict[str, dict],
    resolved_context_window: dict[str, int],
    context_mgr,
    phase_router,
    memory_mgr,
    reflection_cache: dict,
    quality_gate_retries: dict,
    create_llm_provider_func,
    collect_forced_tool_call_arguments,
    compression_events: list[dict] | None = None,
):
    llm = create_llm_provider_func(config.llm)

    def llm_factory(model: str | None = None):
        llm_config = replace(config.llm, model=model) if model else config.llm
        return create_llm_provider_func(llm_config)

    tool_engine = build_tool_engine(config=config, plan=plan)

    hooks, internal_task_events = build_agent_hooks(
        plan=plan,
        sessions=sessions,
        resolved_context_window=resolved_context_window,
        config=config,
        context_mgr=context_mgr,
        compression_events=compression_events,
        create_llm_provider_func=create_llm_provider_func,
        collect_forced_tool_call_arguments=collect_forced_tool_call_arguments,
        quality_gate_retries=quality_gate_retries,
    )

    reflection = reflection_cache.setdefault(plan.session_id, ReflectionInjector())
    tool_choice_decider = ToolChoiceDecider()
    guardrail = (
        ToolGuardrail(disabled_rules=config.guardrails.disabled_rules)
        if config.guardrails.enabled
        else None
    )

    return AgentLoop(
        llm=llm,
        tool_engine=tool_engine,
        hooks=hooks,
        max_retries=config.max_retries,
        phase_router=phase_router,
        context_manager=context_mgr,
        plan=plan,
        llm_factory=llm_factory,
        memory_mgr=memory_mgr,
        memory_enabled=config.memory.enabled,
        user_id=user_id,
        compression_events=compression_events,
        reflection=reflection,
        tool_choice_decider=tool_choice_decider,
        guardrail=guardrail,
        parallel_tool_execution=config.parallel_tool_execution,
        phase5_parallel_config=config.phase5_parallel,
        internal_task_events=internal_task_events,
    )
