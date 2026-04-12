from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from memory.models import MemoryItem


_MAX_VALUE_LENGTH = 160
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class RetrievedMemory:
    core: list[MemoryItem] = field(default_factory=list)
    trip: list[MemoryItem] = field(default_factory=list)
    phase: list[MemoryItem] = field(default_factory=list)


def format_memory_context(memory: RetrievedMemory) -> str:
    sections: list[str] = []

    if memory.core:
        sections.append(_format_section("核心用户画像", memory.core))
    if memory.trip:
        sections.append(_format_section("本次旅行记忆", memory.trip))
    if memory.phase:
        sections.append(_format_section("当前阶段相关历史", memory.phase))

    return "\n\n".join(sections) if sections else "暂无相关用户记忆"


def _format_section(title: str, items: list[MemoryItem]) -> str:
    lines = [f"## {_sanitize_text(title)}"]
    for item in items:
        annotation = _format_annotation(item)
        lines.append(
            f"- [{_sanitize_text(item.domain)}] {_sanitize_text(item.key)}: "
            f"{_format_value(item.value)}{annotation}"
        )
    return "\n".join(lines)


def _format_annotation(item: MemoryItem) -> str:
    parts: list[str] = []
    if item.scope and item.scope != "global":
        parts.append(item.scope)
    elif item.scope == "global":
        parts.append("global")
    if item.source and item.source.kind == "message" and item.status == "active":
        parts.append(f"confidence {item.confidence:.2f}")
    elif item.source and item.source.kind == "migration":
        parts.append("migrated")
    else:
        parts.append(f"confidence {item.confidence:.2f}")
    return f" ({', '.join(parts)})" if parts else ""


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return _sanitize_text(str(value).lower())
    if isinstance(value, (int, float)):
        return _sanitize_text(str(value))
    if isinstance(value, str):
        return _truncate_text(_sanitize_text(value))
    if isinstance(value, dict):
        parts = [
            f"{_sanitize_text(str(key))}={_format_value(value[key])}"
            for key in sorted(value, key=str)
        ]
        return _truncate_text(_sanitize_text("；".join(parts)))
    if isinstance(value, (list, tuple)):
        return _truncate_text(
            _sanitize_text("、".join(_format_value(item) for item in value))
        )
    if isinstance(value, set):
        parts = sorted((_format_value(item) for item in value), key=str)
        return _truncate_text(_sanitize_text("、".join(parts)))
    return _truncate_text(_sanitize_text(str(value)))


def _sanitize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    lines = []
    for raw_line in text.splitlines() or [text]:
        line = raw_line.replace("\t", " ").strip()
        line = _WHITESPACE_RE.sub(" ", line)
        if line.startswith("- ") or line.startswith("* "):
            line = line[2:].lstrip()
        lines.append(line)
    text = " ".join(line for line in lines if line)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = text.replace("#", "＃")
    return text


def _truncate_text(text: str) -> str:
    if len(text) <= _MAX_VALUE_LENGTH:
        return text
    return f"{text[: _MAX_VALUE_LENGTH - 3]}..."
