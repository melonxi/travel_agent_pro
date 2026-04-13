from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class IterationProgress(str, Enum):
    NO_OUTPUT = "no_output"
    PARTIAL_TEXT = "partial_text"
    PARTIAL_TOOL_CALL = "partial_tool_call"
    TOOLS_READ_ONLY = "tools_read_only"
    TOOLS_WITH_WRITES = "tools_with_writes"


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    error_code: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    can_continue: bool = False
    continuation_context: dict[str, Any] | None = None
