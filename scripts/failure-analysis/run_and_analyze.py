#!/usr/bin/env python3
"""Failure analysis runner — execute failure scenarios against live backend.

Usage:
    python scripts/failure-analysis/run_and_analyze.py [--base-url http://127.0.0.1:8000]

Requires: backend running on --base-url (default http://127.0.0.1:8000)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

# Add backend to sys.path for evals imports
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from evals.failure_report import ScenarioResult, save_failure_report
from evals.models import GoldenCase
from evals.runner import evaluate_assertion, load_golden_cases

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
GOLDEN_CASES_DIR = BACKEND_DIR / "evals" / "golden_cases"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def create_session(client: httpx.Client) -> str:
    resp = client.post("/api/sessions")
    resp.raise_for_status()
    return resp.json()["session_id"]


def send_message(client: httpx.Client, session_id: str, message: str) -> list[str]:
    """Send a chat message via SSE and collect response chunks."""
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
            etype = event.get("type", "")
            if etype in {"text", "text_delta"} and event.get("content"):
                responses.append(event["content"])
    return responses


def get_plan_state(client: httpx.Client, session_id: str) -> dict:
    resp = client.get(f"/api/plan/{session_id}")
    resp.raise_for_status()
    return resp.json()


def get_session_stats(client: httpx.Client, session_id: str) -> dict:
    resp = client.get(f"/api/sessions/{session_id}/stats")
    resp.raise_for_status()
    return resp.json()


def get_messages(client: httpx.Client, session_id: str) -> list[dict]:
    resp = client.get(f"/api/messages/{session_id}")
    resp.raise_for_status()
    return resp.json()


def extract_tool_calls_from_messages(messages: list[dict]) -> list[str]:
    """Extract tool names from stored messages."""
    tool_names: list[str] = []
    for msg in messages:
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                name = tc.get("function", {}).get("name") or tc.get("name", "")
                if name and name not in tool_names:
                    tool_names.append(name)
    return tool_names


def ensure_backend_ready(client: httpx.Client, base_url: str) -> None:
    """Verify the backend is reachable before running scenarios."""
    try:
        health = client.get("/health")
        health.raise_for_status()
        if health.json().get("status") != "ok":
            raise RuntimeError("health endpoint did not return status=ok")

        sessions = client.get("/api/sessions")
        sessions.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError) as exc:
        print(f"ERROR: Backend not ready at {base_url}")
        print(f"Reason: {exc}")
        print("Start with: scripts/dev.sh")
        sys.exit(1)


def run_scenario(client: httpx.Client, case: GoldenCase) -> ScenarioResult:
    """Execute a single failure scenario against the live backend."""
    print(f"\n{'=' * 60}")
    print(f"Running: {case.id} — {case.name}")
    print(f"{'=' * 60}")

    start = time.monotonic()

    session_id = create_session(client)
    print(f"  Session: {session_id}")

    all_responses: list[str] = []
    user_input = ""
    for msg in case.messages:
        if msg["role"] == "user":
            if not user_input:
                user_input = msg["content"]
            print(f"  Sending: {msg['content'][:80]}...")
            try:
                chunks = send_message(client, session_id, msg["content"])
                all_responses.extend(chunks)
                print(f"  Got {len(chunks)} response chunks")
            except Exception as exc:
                print(f"  ERROR sending message: {exc}")
                all_responses.append(f"ERROR: {exc}")

    plan_state = get_plan_state(client, session_id)
    stats = get_session_stats(client, session_id)
    messages = get_messages(client, session_id)
    tool_calls = extract_tool_calls_from_messages(messages)

    print(f"  Phase: {plan_state.get('phase', '?')}")
    print(f"  Tools called: {tool_calls}")

    full_response_text = " ".join(all_responses)
    passed = 0
    failures: list[str] = []
    for assertion in case.assertions:
        ok, reason = evaluate_assertion(
            assertion,
            plan_state,
            tool_calls,
            [full_response_text],
        )
        if ok:
            passed += 1
            print(f"  ✅ {assertion.type.value}: {assertion.target}")
        else:
            failures.append(f"[{assertion.type.value}] {reason}")
            print(f"  ❌ {assertion.type.value}: {reason}")

    elapsed = (time.monotonic() - start) * 1000

    result = ScenarioResult(
        scenario_id=case.id,
        name=case.name,
        user_input=user_input,
        passed_assertions=passed,
        total_assertions=len(case.assertions),
        failures=failures,
        tool_calls=tool_calls,
        responses=all_responses,
        duration_ms=elapsed,
        stats={
            "session_id": session_id,
            "plan_state": plan_state,
            "messages": messages,
            **stats,
        },
    )

    status = "PASS" if result.passed else "FAIL"
    print(f"\n  Result: {status} ({passed}/{len(case.assertions)} assertions)")
    print(f"  Duration: {elapsed:.0f}ms")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run failure analysis scenarios")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--output",
        default=str(BACKEND_DIR.parent / "docs" / "failure-analysis.md"),
    )
    args = parser.parse_args()

    all_cases = load_golden_cases(str(GOLDEN_CASES_DIR))
    failure_cases = [case for case in all_cases if case.id.startswith("failure-")]
    print(f"Loaded {len(failure_cases)} failure scenarios")

    if not failure_cases:
        print("ERROR: No failure-*.yaml cases found")
        sys.exit(1)

    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        ensure_backend_ready(client, args.base_url)

        results: list[ScenarioResult] = []
        for case in failure_cases:
            try:
                result = run_scenario(client, case)
                results.append(result)
            except Exception as exc:
                print(f"  FATAL ERROR in {case.id}: {exc}")
                results.append(
                    ScenarioResult(
                        scenario_id=case.id,
                        name=case.name,
                        user_input=case.messages[0]["content"] if case.messages else "",
                        passed_assertions=0,
                        total_assertions=len(case.assertions),
                        failures=[f"FATAL: {exc}"],
                    )
                )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results_json = RESULTS_DIR / "failure-results.json"
    results_json.write_text(
        json.dumps(
            [
                {
                    "scenario_id": result.scenario_id,
                    "name": result.name,
                    "user_input": result.user_input,
                    "passed": result.passed,
                    "passed_assertions": result.passed_assertions,
                    "total_assertions": result.total_assertions,
                    "failures": result.failures,
                    "tool_calls": result.tool_calls,
                    "responses": result.responses,
                    "duration_ms": result.duration_ms,
                    "session_id": result.stats.get("session_id", ""),
                    "plan_state": result.stats.get("plan_state", {}),
                    "messages": result.stats.get("messages", []),
                    "stats": {
                        key: value
                        for key, value in result.stats.items()
                        if key not in {"session_id", "plan_state", "messages"}
                    },
                }
                for result in results
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nResults JSON saved to: {results_json}")

    report_path = save_failure_report(results, output_path=args.output)
    print(f"Report saved to: {report_path}")

    total = len(results)
    passed = sum(1 for result in results if result.passed)
    print(f"\n{'=' * 60}")
    print(f"SUMMARY: {passed}/{total} scenarios passed all assertions")
    print(f"{'=' * 60}")
    for result in results:
        emoji = "✅" if result.passed else "❌"
        print(
            f"  {emoji} {result.scenario_id}: {result.name} "
            f"({result.passed_assertions}/{result.total_assertions})"
        )


if __name__ == "__main__":
    main()
