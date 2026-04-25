from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from agent.hooks import HookManager
from agent.internal_tasks import InternalTask
from config import Phase5ParallelConfig
from tools.engine import ToolEngine


@dataclass(frozen=True)
class AgentLoopDeps:
    llm: Any
    tool_engine: ToolEngine
    hooks: HookManager
    phase_router: Any | None = None
    context_manager: Any | None = None
    plan: Any | None = None
    llm_factory: Any | None = None
    memory_mgr: Any | None = None
    reflection: Any | None = None
    tool_choice_decider: Any | None = None
    guardrail: Any | None = None


@dataclass(frozen=True)
class AgentLoopConfig:
    max_iterations: int | None = None
    max_llm_errors: int | None = None
    memory_enabled: bool = True
    user_id: str = "default_user"
    compression_events: list[dict] | None = None
    parallel_tool_execution: bool = True
    cancel_event: asyncio.Event | None = None
    phase5_parallel_config: Phase5ParallelConfig | None = None
    internal_task_events: list[InternalTask] | None = None
