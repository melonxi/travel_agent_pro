#!/usr/bin/env python3
"""Run pass@k stability evaluation for golden cases.

Usage:
    python scripts/eval-stability.py --k 3 --base-url http://127.0.0.1:8000
    python scripts/eval-stability.py --cases easy-001-tokyo-basic --k 5
    python scripts/eval-stability.py --difficulty easy,medium --k 3
    python scripts/eval-stability.py --mock --cases easy-001-tokyo-basic --k 2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from evals.models import AssertionType, EvalExecution, GoldenCase
from evals.runner import load_golden_cases
from evals.stability import run_stability_suite, save_stability_report

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
GOLDEN_CASES_DIR = BACKEND_DIR / "evals" / "golden_cases"


def create_session(client: httpx.Client) -> str:
    response = client.post("/api/sessions")
    response.raise_for_status()
    return response.json()["session_id"]


def send_message(client: httpx.Client, session_id: str, message: str) -> list[str]:
    """Send a chat message over SSE and collect text chunks."""
    responses: list[str] = []
    with client.stream(
        "POST",
        f"/api/chat/{session_id}",
        json={"message": message},
        timeout=180.0,
    ) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if event.get("type") in {"text", "text_delta"} and event.get("content"):
                responses.append(event["content"])
    return responses


def get_plan_state(client: httpx.Client, session_id: str) -> dict[str, Any]:
    response = client.get(f"/api/plan/{session_id}")
    response.raise_for_status()
    return response.json()


def get_session_stats(client: httpx.Client, session_id: str) -> dict[str, Any]:
    response = client.get(f"/api/sessions/{session_id}/stats")
    response.raise_for_status()
    return response.json()


def get_messages(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/messages/{session_id}")
    response.raise_for_status()
    return response.json()


def extract_tool_calls(messages: list[dict[str, Any]]) -> list[str]:
    """Extract unique tool names from stored messages."""
    tool_names: list[str] = []
    for message in messages:
        for tool_call in message.get("tool_calls") or []:
            name = tool_call.get("function", {}).get("name") or tool_call.get("name", "")
            if name and name not in tool_names:
                tool_names.append(name)
    return tool_names


def make_live_executor(client: httpx.Client) -> Callable[[GoldenCase], EvalExecution]:
    """Build an executor that runs one golden case against the live backend."""

    def executor(case: GoldenCase) -> EvalExecution:
        session_id = create_session(client)
        responses: list[str] = []
        for message in case.messages:
            if message.get("role") == "user":
                responses.extend(send_message(client, session_id, message["content"]))

        state = get_plan_state(client, session_id)
        try:
            stats = get_session_stats(client, session_id)
        except Exception:
            stats = {}
        try:
            messages = get_messages(client, session_id)
        except Exception:
            messages = []

        full_response = " ".join(responses)
        return EvalExecution(
            state=state,
            tool_calls=extract_tool_calls(messages),
            responses=[full_response] if full_response else [],
            stats=stats,
        )

    return executor


def make_mock_executor() -> Callable[[GoldenCase], EvalExecution]:
    """Build a deterministic executor for report-generation smoke checks."""

    def executor(case: GoldenCase) -> EvalExecution:
        state: dict[str, Any] = {"phase": 4, "budget_total": 1000, "total_cost": 900}
        tool_calls: list[str] = []
        responses: list[str] = []

        for assertion in case.assertions:
            if assertion.type == AssertionType.STATE_FIELD_SET:
                state[assertion.target] = assertion.value if assertion.value is not None else "mock"
            elif assertion.type == AssertionType.TOOL_CALLED:
                if assertion.target not in tool_calls:
                    tool_calls.append(assertion.target)
            elif assertion.type == AssertionType.CONTAINS_TEXT:
                responses.append(assertion.target)

        return EvalExecution(
            state=state,
            tool_calls=tool_calls,
            responses=responses or ["mock response"],
            stats={
                "estimated_cost_usd": 0.01,
                "total_input_tokens": 10,
                "total_output_tokens": 20,
            },
        )

    return executor


def ensure_backend_ready(client: httpx.Client, base_url: str) -> None:
    try:
        response = client.get("/health")
        response.raise_for_status()
        if response.json().get("status") != "ok":
            raise RuntimeError("health endpoint did not return status=ok")
    except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError) as exc:
        print(f"ERROR: backend is not ready at {base_url}", file=sys.stderr)
        print(f"Reason: {exc}", file=sys.stderr)
        print("Start it with: scripts/dev.sh", file=sys.stderr)
        raise SystemExit(1) from exc


def parse_csv(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def filter_cases(
    cases: list[GoldenCase],
    case_ids: str,
    difficulties: str,
) -> list[GoldenCase]:
    selected = cases
    if case_ids:
        ids = parse_csv(case_ids)
        selected = [case for case in selected if case.id in ids]
    if difficulties:
        allowed = parse_csv(difficulties)
        selected = [case for case in selected if case.difficulty in allowed]
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="pass@k stability evaluation for golden cases"
    )
    parser.add_argument("--k", type=int, default=3, help="runs per case (default: 3)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="backend URL")
    parser.add_argument("--cases", default="", help="comma-separated case IDs")
    parser.add_argument("--difficulty", default="", help="comma-separated difficulties")
    parser.add_argument(
        "--output",
        default="docs/eval-stability-report",
        help="output path prefix (default: docs/eval-stability-report)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="use deterministic mock execution instead of a live backend",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.k < 1:
        parser.error("--k must be >= 1")

    all_cases = load_golden_cases(GOLDEN_CASES_DIR)
    cases = filter_cases(all_cases, args.cases, args.difficulty)
    if not cases:
        print("ERROR: no golden cases match the given filters", file=sys.stderr)
        return 1

    print(f"Loaded {len(cases)} golden case(s)")
    print(f"Running pass@{args.k} stability evaluation")

    if args.mock:
        executor = make_mock_executor()
        suite = run_stability_suite(cases, executor, k=args.k)
    else:
        with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
            ensure_backend_ready(client, args.base_url)
            suite = run_stability_suite(cases, make_live_executor(client), k=args.k)

    json_path, md_path = save_stability_report(suite, args.output)

    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    print(f"Overall pass rate: {suite.overall_pass_rate:.2f}")
    print(f"Unstable cases: {len(suite.unstable_cases)}")
    print(f"Highly unstable cases: {len(suite.highly_unstable_cases)}")

    for metrics in suite.results:
        passed = int(metrics.pass_rate * metrics.k + 0.5)
        print(
            f"{metrics.case_id}: {passed}/{metrics.k} "
            f"(tool overlap {metrics.tool_overlap_ratio:.2f}, "
            f"mean cost ${metrics.cost_stats.get('mean', 0.0):.2f})"
        )

    return 1 if suite.highly_unstable_cases else 0


if __name__ == "__main__":
    raise SystemExit(main())
