from __future__ import annotations

from pathlib import Path

from evals.reranker import (
    load_reranker_cases,
    run_reranker_case,
    run_reranker_suite,
)


RERANKER_CASES_DIR = Path(__file__).resolve().parents[1] / "evals/reranker_cases"


def test_loads_deterministic_reranker_cases():
    cases = load_reranker_cases(RERANKER_CASES_DIR)

    assert {case.id for case in cases} >= {
        "reranker-only-001-hybrid-stay-profile-slice",
        "reranker-only-002-conflicting-profile-drop",
        "reranker-only-003-slice-intent-no-profile-leak",
        "reranker-only-004-profile-dedup-preserves_polarity",
        "reranker-only-005-applicability-family-context",
        "reranker-only-006-recency-half-life-prioritizes-recent",
        "reranker-only-007-negated-preference-drops-profile",
        "reranker-only-008-rejected-slice-conflicts-with-positive-intent",
        "reranker-only-009-natural-too-much-hassle",
        "reranker-only-010-natural-change-style",
        "reranker-only-011-natural-follow-last-comfortable",
        "reranker-only-012-natural-dont-ruin-next-day",
        "reranker-only-013-natural-quiet-not-commercial",
        "reranker-only-014-natural-parents-not-tiring",
        "reranker-only-015-implicit-change-away-from-old-style",
        "reranker-only-016-all-weak-candidates-drop",
        "reranker-only-017-redundant-slices-keep-best",
        "reranker-only-018-budget-tight-but-not-bad-hotel",
    }
    assert all(case.candidates for case in cases)


def test_run_reranker_case_returns_deterministic_pass_result():
    case = next(
        case
        for case in load_reranker_cases(RERANKER_CASES_DIR)
        if case.id == "reranker-only-001-hybrid-stay-profile-slice"
    )

    result = run_reranker_case(case)

    assert result.passed, result.failures
    assert result.selected_item_ids == case.expected_selected_item_ids
    assert result.candidate_count == len(case.candidates)
    assert "source-aware weighted rerank" in result.final_reason


def test_run_reranker_suite_aggregates_failures_without_live_dependencies():
    cases = load_reranker_cases(RERANKER_CASES_DIR)

    suite = run_reranker_suite(cases)

    assert suite.total >= 4
    assert suite.errors == 0
    assert suite.failed == 0
    assert suite.passed == suite.total
