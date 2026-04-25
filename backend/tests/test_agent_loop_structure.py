from __future__ import annotations

import ast
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def line_count(relative_path: str) -> int:
    return len((BACKEND_DIR / relative_path).read_text(encoding="utf-8").splitlines())


def module_defines(relative_path: str, name: str) -> bool:
    tree = ast.parse((BACKEND_DIR / relative_path).read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.name == name
        for node in tree.body
    )


def test_agent_execution_and_phase5_packages_exist():
    agent_dir = BACKEND_DIR / "agent"
    execution_dir = agent_dir / "execution"
    phase5_dir = agent_dir / "phase5"

    expected_execution_modules = {
        "__init__.py",
        "limits.py",
        "loop_config.py",
        "llm_turn.py",
        "message_rebuild.py",
        "phase_transition.py",
        "repair_hints.py",
        "tool_invocation.py",
        "tool_batches.py",
    }
    expected_phase5_modules = {
        "__init__.py",
        "parallel.py",
        "orchestrator.py",
        "day_worker.py",
        "worker_prompt.py",
    }

    assert expected_execution_modules.issubset(
        {path.name for path in execution_dir.glob("*.py")}
    )
    assert expected_phase5_modules.issubset(
        {path.name for path in phase5_dir.glob("*.py")}
    )


def test_agent_execution_modules_expose_expected_names():
    assert module_defines(
        "agent/execution/limits.py",
        "AgentLoopLimits",
    )
    assert module_defines(
        "agent/execution/loop_config.py",
        "AgentLoopDeps",
    )
    assert module_defines(
        "agent/execution/loop_config.py",
        "AgentLoopConfig",
    )
    assert module_defines(
        "agent/execution/llm_turn.py",
        "run_llm_turn",
    )
    assert module_defines(
        "agent/execution/llm_turn.py",
        "LlmTurnOutcome",
    )
    assert module_defines(
        "agent/execution/repair_hints.py",
        "build_phase3_state_repair_message",
    )
    assert module_defines(
        "agent/execution/repair_hints.py",
        "build_phase5_state_repair_message",
    )
    assert module_defines(
        "agent/execution/message_rebuild.py",
        "rebuild_messages_for_phase_change",
    )
    assert module_defines(
        "agent/execution/phase_transition.py",
        "detect_phase_transition",
    )
    assert module_defines(
        "agent/execution/phase_transition.py",
        "PhaseTransitionRequest",
    )
    assert module_defines(
        "agent/execution/tool_invocation.py",
        "SearchHistoryTracker",
    )
    assert module_defines(
        "agent/execution/tool_batches.py",
        "execute_tool_batch",
    )


def test_agent_loop_public_surface_and_size_guard():
    loop_text = (BACKEND_DIR / "agent/loop.py").read_text(encoding="utf-8")

    assert "class AgentLoop" in loop_text
    assert "async def run(" in loop_text
    # Coarse regression guard: keep AgentLoop from drifting back into a god file
    # while compatibility wrappers are still present.
    assert line_count("agent/loop.py") < 640


def test_agent_loop_compatibility_methods_remain():
    from agent.loop import AgentLoop

    for method_name in (
        "should_use_parallel_phase5",
        "_rebuild_messages_for_phase_change",
        "_rebuild_messages_for_phase3_step_change",
        "_pre_execution_skip_result",
        "_validate_tool_output",
        "_is_parallel_read_call",
        "_should_skip_redundant_update",
    ):
        assert hasattr(AgentLoop, method_name)


def test_phase5_has_no_root_level_duplicate_modules():
    agent_dir = BACKEND_DIR / "agent"

    assert not (agent_dir / "orchestrator.py").exists()
    assert not (agent_dir / "day_worker.py").exists()
    assert not (agent_dir / "worker_prompt.py").exists()


def test_phase5_imports_use_phase5_package():
    from agent.phase5.day_worker import run_day_worker
    from agent.phase5.orchestrator import Phase5Orchestrator
    from agent.phase5.worker_prompt import DayTask

    assert Phase5Orchestrator is not None
    assert run_day_worker is not None
    assert DayTask is not None
