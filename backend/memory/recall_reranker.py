from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memory.retrieval_candidates import RecallCandidate


@dataclass
class RecallRerankResult:
    selected_item_ids: list[str]
    final_reason: str
    per_item_reason: dict[str, str]
    fallback_used: str = "none"


@dataclass
class RecallRerankPath:
    should_call_llm: bool
    selected_candidates: list[RecallCandidate]
    fallback_used: str


def build_recall_reranker_tool() -> dict[str, Any]:
    return {
        "name": "select_recall_candidates",
        "description": "Select the most relevant recall candidates for the current user request.",
        "parameters": {
            "type": "object",
            "properties": {
                "selected_item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "final_reason": {"type": "string"},
                "per_item_reason": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["selected_item_ids", "final_reason", "per_item_reason"],
            "additionalProperties": False,
        },
    }


def choose_reranker_path(
    candidates: list[RecallCandidate],
    rerank_threshold: int = 3,
    fallback_top_n: int = 3,
) -> RecallRerankPath:
    if len(candidates) <= rerank_threshold:
        return RecallRerankPath(
            should_call_llm=False,
            selected_candidates=list(candidates),
            fallback_used="skipped_small_candidate_set",
        )
    return RecallRerankPath(
        should_call_llm=True,
        selected_candidates=list(candidates[:fallback_top_n]),
        fallback_used="none",
    )


def parse_recall_reranker_arguments(
    payload: dict[str, Any] | None,
) -> RecallRerankResult:
    if not isinstance(payload, dict):
        return RecallRerankResult(
            selected_item_ids=[],
            final_reason="invalid_reranker_payload",
            per_item_reason={},
            fallback_used="invalid_reranker_payload",
        )

    selected_item_ids = payload.get("selected_item_ids")
    final_reason = payload.get("final_reason")
    per_item_reason = payload.get("per_item_reason")
    if (
        isinstance(selected_item_ids, list)
        and all(isinstance(item, str) for item in selected_item_ids)
        and isinstance(final_reason, str)
        and isinstance(per_item_reason, dict)
        and all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in per_item_reason.items()
        )
    ):
        return RecallRerankResult(
            selected_item_ids=list(selected_item_ids),
            final_reason=final_reason,
            per_item_reason=dict(per_item_reason),
            fallback_used="none",
        )

    return RecallRerankResult(
        selected_item_ids=[],
        final_reason="invalid_reranker_payload",
        per_item_reason={},
        fallback_used="invalid_reranker_payload",
    )
