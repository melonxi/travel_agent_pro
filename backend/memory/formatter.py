from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from memory.models import MemoryItem


@dataclass
class RetrievedMemory:
    core_profile: list[MemoryItem] = field(default_factory=list)
    trip_memory: list[MemoryItem] = field(default_factory=list)
    phase_relevant: list[MemoryItem] = field(default_factory=list)


def format_memory_context(memory: RetrievedMemory) -> str:
    sections: list[str] = []

    if memory.core_profile:
        sections.append(_format_section("核心用户画像", memory.core_profile))
    if memory.trip_memory:
        sections.append(_format_section("本次旅行记忆", memory.trip_memory))
    if memory.phase_relevant:
        sections.append(_format_section("当前阶段相关历史", memory.phase_relevant))

    return "\n\n".join(sections) if sections else "暂无相关用户记忆"


def _format_section(title: str, items: list[MemoryItem]) -> str:
    lines = [f"## {title}"]
    for item in items:
        lines.append(f"- {item.domain}.{item.key}: {_format_value(item.value)}")
    return "\n".join(lines)


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = [f"{key}={_format_value(value[key])}" for key in sorted(value)]
        return "；".join(parts)
    if isinstance(value, (list, tuple, set)):
        return "、".join(_format_value(item) for item in value)
    return str(value)
