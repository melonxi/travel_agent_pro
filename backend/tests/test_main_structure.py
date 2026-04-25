from pathlib import Path


def test_main_is_reduced_to_application_assembly():
    main_path = Path(__file__).resolve().parents[1] / "main.py"

    assert len(main_path.read_text(encoding="utf-8").splitlines()) < 350


def test_api_routes_package_owns_http_route_groups():
    routes_dir = Path(__file__).resolve().parents[1] / "api/routes"

    expected_route_modules = {
        "artifact_routes.py",
        "chat_routes.py",
        "internal_task_routes.py",
        "session_routes.py",
        "memory_routes.py",
    }

    assert expected_route_modules.issubset(
        {path.name for path in routes_dir.glob("*.py")}
    )


def test_api_orchestration_package_owns_agent_builder():
    agent_dir = Path(__file__).resolve().parents[1] / "api/orchestration/agent"

    assert (agent_dir / "builder.py").exists()


def test_api_orchestration_package_splits_agent_builder_details():
    agent_dir = Path(__file__).resolve().parents[1] / "api/orchestration/agent"
    agent_builder_path = agent_dir / "builder.py"

    expected_agent_modules = {
        "hooks.py",
        "tools.py",
    }

    assert expected_agent_modules.issubset(
        {path.name for path in agent_dir.glob("*.py")}
    )
    assert len(agent_builder_path.read_text(encoding="utf-8").splitlines()) < 400


def test_api_orchestration_package_owns_chat_stream():
    chat_dir = Path(__file__).resolve().parents[1] / "api/orchestration/chat"

    assert (chat_dir / "stream.py").exists()


def test_api_orchestration_package_splits_chat_stream_details():
    chat_dir = Path(__file__).resolve().parents[1] / "api/orchestration/chat"
    chat_stream_path = chat_dir / "stream.py"

    expected_chat_modules = {
        "events.py",
        "finalization.py",
    }

    assert expected_chat_modules.issubset(
        {path.name for path in chat_dir.glob("*.py")}
    )
    assert len(chat_stream_path.read_text(encoding="utf-8").splitlines()) < 400


def test_api_orchestration_package_owns_memory_turn():
    memory_dir = Path(__file__).resolve().parents[1] / "api/orchestration/memory"

    assert (memory_dir / "turn.py").exists()


def test_api_orchestration_package_splits_memory_details():
    memory_dir = Path(__file__).resolve().parents[1] / "api/orchestration/memory"
    memory_orchestration_path = memory_dir / "orchestration.py"

    expected_memory_modules = {
        "contracts.py",
        "recall_planning.py",
        "extraction.py",
        "tasks.py",
        "episodes.py",
    }

    assert expected_memory_modules.issubset(
        {path.name for path in memory_dir.glob("*.py")}
    )
    assert not (memory_dir / "recall.py").exists()
    assert memory_orchestration_path.exists()
    assert (
        len(memory_orchestration_path.read_text(encoding="utf-8").splitlines()) < 1000
    )


def test_memory_routes_do_not_own_internal_task_routes():
    memory_routes_path = (
        Path(__file__).resolve().parents[1] / "api/routes/memory_routes.py"
    )

    assert "/api/internal-tasks" not in memory_routes_path.read_text(encoding="utf-8")


def test_memory_contracts_own_shared_memory_dataclasses():
    contracts_path = (
        Path(__file__).resolve().parents[1] / "api/orchestration/memory/contracts.py"
    )
    recall_planning_path = (
        Path(__file__).resolve().parents[1]
        / "api/orchestration/memory/recall_planning.py"
    )

    contracts_text = contracts_path.read_text(encoding="utf-8")
    recall_planning_text = recall_planning_path.read_text(encoding="utf-8")

    expected_dataclasses = {
        "MemoryExtractionOutcome",
        "MemoryExtractionProgress",
        "MemoryRouteSaveProgress",
        "MemoryExtractionGateDecision",
        "MemorySchedulerRuntime",
        "MemoryRecallDecision",
        "RecallQueryPlanResult",
    }

    for class_name in expected_dataclasses:
        assert f"class {class_name}" in contracts_text
        assert f"class {class_name}" not in recall_planning_text


def test_api_orchestration_package_owns_remaining_main_helpers():
    session_dir = Path(__file__).resolve().parents[1] / "api/orchestration/session"
    common_dir = Path(__file__).resolve().parents[1] / "api/orchestration/common"

    expected_session_modules = {
        "backtrack.py",
        "deliverables.py",
        "message_fallbacks.py",
        "pending_notes.py",
        "persistence.py",
    }
    expected_common_modules = {
        "llm_errors.py",
        "telemetry_helpers.py",
    }

    assert expected_session_modules.issubset(
        {path.name for path in session_dir.glob("*.py")}
    )
    assert expected_common_modules.issubset(
        {path.name for path in common_dir.glob("*.py")}
    )


def test_api_root_does_not_own_orchestration_modules():
    api_dir = Path(__file__).resolve().parents[1] / "api"

    orchestration_module_names = {
        "memory_contracts.py",
        "memory_recall_planning.py",
        "memory_orchestration.py",
        "message_fallbacks.py",
        "pending_notes.py",
        "telemetry_helpers.py",
    }

    assert orchestration_module_names.isdisjoint(
        {path.name for path in api_dir.glob("*.py")}
    )


def test_api_orchestration_root_only_owns_group_packages():
    orchestration_dir = Path(__file__).resolve().parents[1] / "api/orchestration"

    expected_groups = {
        "agent",
        "chat",
        "common",
        "memory",
        "session",
    }
    old_flat_modules = {
        "agent_builder.py",
        "agent_hooks.py",
        "agent_tools.py",
        "backtrack.py",
        "chat_events.py",
        "chat_finalization.py",
        "chat_stream.py",
        "deliverables.py",
        "llm_errors.py",
        "memory_contracts.py",
        "memory_episodes.py",
        "memory_extraction.py",
        "memory_orchestration.py",
        "memory_recall_planning.py",
        "memory_tasks.py",
        "memory_turn.py",
        "message_fallbacks.py",
        "pending_notes.py",
        "session_persistence.py",
        "telemetry_helpers.py",
    }

    assert expected_groups.issubset(
        {path.name for path in orchestration_dir.iterdir() if path.is_dir()}
    )
    assert old_flat_modules.isdisjoint(
        {path.name for path in orchestration_dir.glob("*.py")}
    )


def test_memory_orchestration_does_not_own_backtrack_patterns():
    memory_orchestration_path = (
        Path(__file__).resolve().parents[1] / "api/orchestration/memory/orchestration.py"
    )

    assert "_BACKTRACK_PATTERNS" not in memory_orchestration_path.read_text(
        encoding="utf-8"
    )


def test_main_preserves_legacy_helper_exports():
    import main

    expected_exports = [
        "ChatRequest",
        "BacktrackRequest",
        "MemoryExtractionOutcome",
        "MemoryRecallDecision",
        "_build_recall_query_tool",
        "_collect_forced_tool_call_arguments",
        "_apply_message_fallbacks",
        "_record_tool_result_stats",
        "_record_llm_usage_stats",
        "push_pending_system_note",
        "flush_pending_system_notes",
    ]

    for export_name in expected_exports:
        assert hasattr(main, export_name)
