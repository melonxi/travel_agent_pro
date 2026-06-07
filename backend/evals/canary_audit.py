# backend/evals/canary_audit.py
"""Trace-based audit for the phase canary.

The phase canary used to count tool calls from the SSE stream. That stream does
NOT surface Phase 3 sub-agent (day-worker) tool calls, so SSE-based detection is
blind to the heaviest part of a run. This module audits the persisted
``trace_events`` instead, which records every tool call across all phases.

Input is a list of ``trace_events`` rows in the shape returned by
``TraceStore.load_events`` (``SELECT *``): a top-level ``tool_name`` / ``status``
and ``payload_json`` as a JSON string.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolErrorStat:
    tool: str
    total: int
    error: int
    error_codes: tuple[str, ...]

    @property
    def error_rate(self) -> float:
        return self.error / self.total if self.total else 0.0


@dataclass(frozen=True)
class RunAudit:
    tool_calls: list[str]
    tool_count: int
    forbidden_hits: dict[str, list[str]]
    over_budget: int | None
    error_stats: list[ToolErrorStat]
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A run is ok when it has no hard violations (forbidden tools)."""
        return not self.violations


def _error_code(event: dict[str, Any]) -> str | None:
    code = event.get("error_code")
    if code:
        return str(code)
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raw = event.get("payload_json")
        if isinstance(raw, str) and raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
        else:
            payload = {}
    code = payload.get("error_code")
    return str(code) if code else None


def audit_events(
    events: list[dict[str, Any]],
    *,
    forbidden_prefixes: tuple[str, ...] = (),
    max_tool_calls: int | None = None,
    error_rate_warn: float = 0.5,
    error_rate_min_calls: int = 3,
) -> RunAudit:
    tool_events = [e for e in events if e.get("event_type") == "tool_call"]
    tool_calls = [str(e["tool_name"]) for e in tool_events if e.get("tool_name")]

    forbidden_hits: dict[str, list[str]] = {}
    for prefix in forbidden_prefixes:
        matched = sorted({name for name in tool_calls if name.startswith(prefix)})
        if matched:
            forbidden_hits[prefix] = matched

    violations = [
        f"forbidden_tool:{prefix}:{names}" for prefix, names in forbidden_hits.items()
    ]

    warnings: list[str] = []
    over_budget: int | None = None
    if max_tool_calls is not None and len(tool_calls) > max_tool_calls:
        over_budget = len(tool_calls) - max_tool_calls
        warnings.append(f"tool_budget_exceeded:{len(tool_calls)}>{max_tool_calls}")

    by_tool: dict[str, dict[str, Any]] = {}
    for event in tool_events:
        name = event.get("tool_name")
        if not name:
            continue
        bucket = by_tool.setdefault(name, {"total": 0, "error": 0, "codes": set()})
        bucket["total"] += 1
        if event.get("status") == "error":
            bucket["error"] += 1
            code = _error_code(event)
            if code:
                bucket["codes"].add(code)

    error_stats: list[ToolErrorStat] = []
    for name, bucket in by_tool.items():
        if bucket["error"] == 0:
            continue
        stat = ToolErrorStat(
            tool=name,
            total=bucket["total"],
            error=bucket["error"],
            error_codes=tuple(sorted(bucket["codes"])),
        )
        error_stats.append(stat)
        if stat.total >= error_rate_min_calls and stat.error_rate >= error_rate_warn:
            warnings.append(
                f"high_error_rate:{name}:{stat.error}/{stat.total}:"
                f"{list(stat.error_codes)}"
            )
    error_stats.sort(key=lambda s: s.error, reverse=True)

    return RunAudit(
        tool_calls=tool_calls,
        tool_count=len(tool_calls),
        forbidden_hits=forbidden_hits,
        over_budget=over_budget,
        error_stats=error_stats,
        violations=violations,
        warnings=warnings,
    )
