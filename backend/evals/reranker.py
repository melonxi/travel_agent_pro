"""Deterministic reranker-only eval harness.

These cases intentionally freeze Stage 0/1/2 output and symbolic recall
candidates so failures are attributable to the reranker, not live gate/query
variance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from config import MemoryRerankerConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_reranker import choose_reranker_path
from memory.retrieval_candidates import RecallCandidate
from state.models import TravelPlanState, Travelers


@dataclass
class RerankerGoldenCase:
    id: str
    name: str
    description: str
    user_message: str
    plan: TravelPlanState
    retrieval_plan: RecallRetrievalPlan
    candidates: list[RecallCandidate]
    config: MemoryRerankerConfig
    expected_selected_item_ids: list[str]
    expected_final_reason: str | None = None
    expected_fallback_used: str | None = None
    expected_reason_contains: dict[str, str] = field(default_factory=dict)


@dataclass
class RerankerCaseResult:
    case_id: str
    passed: bool
    selected_item_ids: list[str] = field(default_factory=list)
    final_reason: str = ""
    fallback_used: str = ""
    candidate_count: int = 0
    failures: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class RerankerSuiteResult:
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    results: list[RerankerCaseResult] = field(default_factory=list)


def load_reranker_cases(directory: str | Path) -> list[RerankerGoldenCase]:
    dirpath = Path(directory)
    cases: list[RerankerGoldenCase] = []
    for filepath in sorted(dirpath.glob("*.y*ml")):
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not data:
            continue
        cases.append(_case_from_dict(data))
    return cases


def run_reranker_case(case: RerankerGoldenCase) -> RerankerCaseResult:
    try:
        path = choose_reranker_path(
            candidates=case.candidates,
            user_message=case.user_message,
            plan=case.plan,
            retrieval_plan=case.retrieval_plan,
            config=case.config,
        )
    except Exception as exc:
        return RerankerCaseResult(
            case_id=case.id,
            passed=False,
            candidate_count=len(case.candidates),
            error=f"{type(exc).__name__}: {exc}",
        )

    result = path.result
    selected_ids = list(result.selected_item_ids)
    failures: list[str] = []
    if selected_ids != case.expected_selected_item_ids:
        failures.append(
            f"selected_item_ids={selected_ids}, "
            f"expected {case.expected_selected_item_ids}"
        )
    if (
        case.expected_final_reason is not None
        and result.final_reason != case.expected_final_reason
    ):
        failures.append(
            f"final_reason={result.final_reason!r}, "
            f"expected {case.expected_final_reason!r}"
        )
    if (
        case.expected_fallback_used is not None
        and result.fallback_used != case.expected_fallback_used
    ):
        failures.append(
            f"fallback_used={result.fallback_used!r}, "
            f"expected {case.expected_fallback_used!r}"
        )
    for item_id, expected_text in case.expected_reason_contains.items():
        reason = result.per_item_reason.get(item_id, "")
        if expected_text not in reason:
            failures.append(
                f"per_item_reason[{item_id}]={reason!r}, "
                f"expected to contain {expected_text!r}"
            )

    return RerankerCaseResult(
        case_id=case.id,
        passed=not failures,
        selected_item_ids=selected_ids,
        final_reason=result.final_reason,
        fallback_used=result.fallback_used,
        candidate_count=len(case.candidates),
        failures=failures,
    )


def run_reranker_suite(cases: list[RerankerGoldenCase]) -> RerankerSuiteResult:
    suite = RerankerSuiteResult(total=len(cases))
    for case in cases:
        result = run_reranker_case(case)
        suite.results.append(result)
        if result.error:
            suite.errors += 1
        elif result.passed:
            suite.passed += 1
        else:
            suite.failed += 1
    return suite


def _case_from_dict(data: dict[str, Any]) -> RerankerGoldenCase:
    expected = data.get("expected", {})
    return RerankerGoldenCase(
        id=data["id"],
        name=data["name"],
        description=data.get("description", ""),
        user_message=data["user_message"],
        plan=_plan_from_dict(data.get("plan", {})),
        retrieval_plan=RecallRetrievalPlan(**data["retrieval_plan"]),
        candidates=[RecallCandidate(**candidate) for candidate in data["candidates"]],
        config=MemoryRerankerConfig(**data.get("config", {})),
        expected_selected_item_ids=list(expected.get("selected_item_ids", [])),
        expected_final_reason=expected.get("final_reason"),
        expected_fallback_used=expected.get("fallback_used"),
        expected_reason_contains=dict(expected.get("reason_contains", {})),
    )


def _plan_from_dict(data: dict[str, Any]) -> TravelPlanState:
    plan_data = dict(data)
    travelers_data = plan_data.get("travelers")
    if isinstance(travelers_data, dict):
        plan_data["travelers"] = Travelers(**travelers_data)
    return TravelPlanState(**plan_data)
