from __future__ import annotations

from agent.hooks import HookManager
from agent.loop import AgentLoop, AgentLoopConfig, AgentLoopDeps
from tools.engine import ToolEngine


def test_agent_loop_accepts_grouped_deps_and_config():
    llm = object()
    engine = ToolEngine()
    hooks = HookManager()

    agent = AgentLoop(
        deps=AgentLoopDeps(
            llm=llm,
            tool_engine=engine,
            hooks=hooks,
        ),
        config=AgentLoopConfig(
            max_iterations=4,
            max_llm_errors=2,
            user_id="u-grouped",
            memory_enabled=False,
            parallel_tool_execution=False,
        ),
    )

    assert agent.llm is llm
    assert agent.tool_engine is engine
    assert agent.hooks is hooks
    assert agent.max_iterations == 4
    assert agent.limits.max_llm_errors == 2
    assert agent.user_id == "u-grouped"
    assert agent.memory_enabled is False
    assert agent.parallel_tool_execution is False


def test_agent_loop_legacy_constructor_still_works():
    llm = object()
    engine = ToolEngine()
    hooks = HookManager()

    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=hooks,
        max_retries=6,
        user_id="legacy",
    )

    assert agent.llm is llm
    assert agent.tool_engine is engine
    assert agent.hooks is hooks
    assert agent.max_iterations == 6
    assert agent.user_id == "legacy"
