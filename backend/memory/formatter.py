from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from memory.retrieval_candidates import RecallCandidate
from memory.v3_models import EpisodeSlice, WorkingMemoryItem


_MAX_VALUE_LENGTH = 160
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class MemoryRecallTelemetry:
    sources: dict[str, int] = field(
        default_factory=lambda: {
            "query_profile": 0,
            "working_memory": 0,
            "episode_slice": 0,
        }
    )
    profile_ids: list[str] = field(default_factory=list)
    working_memory_ids: list[str] = field(default_factory=list)
    slice_ids: list[str] = field(default_factory=list)
    matched_reasons: list[str] = field(default_factory=list)
    stage0_decision: str = "undecided"
    stage0_reason: str = ""
    gate_needs_recall: bool | None = None
    gate_intent_type: str = ""
    gate_confidence: float | None = None
    gate_reason: str = ""
    final_recall_decision: str = ""
    fallback_used: str = "none"
    query_plan: dict[str, Any] = field(default_factory=dict)
    query_plan_fallback: str = "none"
    candidate_count: int = 0
    reranker_selected_ids: list[str] = field(default_factory=list)
    reranker_final_reason: str = ""
    reranker_fallback: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": dict(self.sources),
            "profile_ids": list(self.profile_ids),
            "working_memory_ids": list(self.working_memory_ids),
            "slice_ids": list(self.slice_ids),
            "matched_reasons": list(self.matched_reasons),
            "stage0_decision": self.stage0_decision,
            "stage0_reason": self.stage0_reason,
            "gate_needs_recall": self.gate_needs_recall,
            "gate_intent_type": self.gate_intent_type,
            "gate_confidence": self.gate_confidence,
            "gate_reason": self.gate_reason,
            "final_recall_decision": self.final_recall_decision,
            "fallback_used": self.fallback_used,
            "query_plan": dict(self.query_plan),
            "query_plan_fallback": self.query_plan_fallback,
            "candidate_count": self.candidate_count,
            "reranker_selected_ids": list(self.reranker_selected_ids),
            "reranker_final_reason": self.reranker_final_reason,
            "reranker_fallback": self.reranker_fallback,
        }


def format_v3_memory_context(
    working_items: list[WorkingMemoryItem],
    recall_candidates: list[RecallCandidate],
) -> str:
    sections: list[str] = []

    if working_items:
        lines = ["## 当前会话工作记忆"]
        for item in working_items:
            lines.append(_format_v3_working_memory_item(item))
        sections.append("\n".join(lines))

    history_lines = [_format_recall_candidate(candidate) for candidate in recall_candidates]
    if history_lines:
        sections.append("\n".join(["## 本轮请求命中的历史记忆", *history_lines]))

    return "\n\n".join(sections) if sections else "暂无相关用户记忆"


def _format_v3_working_memory_item(item: WorkingMemoryItem) -> str:
    details = _format_details(
        source="working_memory",
        bucket=item.kind,
        matched_reason=item.reason,
    )
    domain_text = ",".join(_sanitize_text(domain) for domain in item.domains if domain)
    domain_prefix = f"[{domain_text}] " if domain_text else ""
    return (
        f"- {_sanitize_text(details)} "
        f"{domain_prefix}content: {_format_value(item.content)}"
    )


def _format_v3_slice(slice_: EpisodeSlice, matched_reason: str | None = None) -> str:
    details = _format_details(
        source="episode_slice",
        bucket=slice_.slice_type,
        matched_reason=matched_reason,
        applicability=slice_.applicability,
    )
    domain_text = ",".join(_sanitize_text(domain) for domain in slice_.domains if domain)
    domain_prefix = f"[{domain_text}] " if domain_text else ""
    return (
        f"- {_sanitize_text(details)} "
        f"{domain_prefix}content: {_format_value(slice_.content)}"
    )


def _format_recall_candidate(candidate: RecallCandidate) -> str:
    details = _format_details(
        source=candidate.source,
        bucket=candidate.bucket,
        matched_reason="；".join(candidate.matched_reason),
        applicability=candidate.applicability,
    )
    if candidate.source == "profile":
        return _format_profile_recall_candidate(details, candidate)
    return _format_slice_recall_candidate(details, candidate)


def _format_profile_recall_candidate(details: str, candidate: RecallCandidate) -> str:
    domain, key, value = _parse_profile_candidate_summary(candidate)
    return (
        f"- {_sanitize_text(details)} "
        f"[{_sanitize_text(domain)}] {_sanitize_text(key)}: {_format_value(value)}"
    )


def _format_slice_recall_candidate(details: str, candidate: RecallCandidate) -> str:
    domain_text = ",".join(_sanitize_text(domain) for domain in candidate.domains if domain)
    domain_prefix = f"[{domain_text}] " if domain_text else ""
    return (
        f"- {_sanitize_text(details)} "
        f"{domain_prefix}content: {_format_value(candidate.content_summary)}"
    )


def _parse_profile_candidate_summary(candidate: RecallCandidate) -> tuple[str, str, str]:
    summary = candidate.content_summary or ""
    domain = candidate.domains[0] if candidate.domains else ""
    key = summary
    value = ""
    if ":" in summary:
        summary_domain, remainder = summary.split(":", 1)
        if summary_domain:
            domain = summary_domain
        key = remainder
    if "=" in key:
        key, value = key.split("=", 1)
    return domain, key, value


def _format_details(
    *,
    source: str,
    bucket: str | None = None,
    matched_reason: str | None = None,
    applicability: str | None = None,
) -> str:
    parts = [f"source={source}"]
    if bucket:
        parts.append(f"bucket={bucket}")
    if matched_reason:
        parts.append(f"matched reason={matched_reason}")
    if applicability:
        parts.append(f"applicability={applicability}")
    return " ".join(_sanitize_text(part) for part in parts if part)


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
