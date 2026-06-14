#!/usr/bin/env python3
"""Run a live Phase 1-4 canary against the backend.

The scenario intentionally does not ask the agent to auto-lock choices before the
test user confirms them. This keeps the canary aligned with the product rule that
lock decisions require explicit user confirmation.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ENV = ROOT / "backend" / ".env"
REPORT_DIR = ROOT / "data" / "canary_runs"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

# Reuse the backend's trace audit so tool-usage checks read the persisted trace
# (which covers Phase 3 sub-agent calls) instead of the Phase-3-blind SSE stream.
sys.path.insert(0, str(ROOT / "backend"))
from evals.canary_audit import audit_events  # noqa: E402


CANARY_PRINCIPLES = [
    "Fixed task goals, not fixed one-shot user stories.",
    "Phase 1 tests destination convergence only; dates/budget/preferences are added later.",
    "Each Phase 2 substep is tested in a separate run: brief, candidate, shortlist, skeleton, lock options, lock confirmation.",
    "The user must explicitly confirm lock choices before selected transport/accommodation/skeleton are expected.",
    "A task should not ask the agent to do downstream work that belongs to a later task.",
    "The report records overreach symptoms instead of hiding them.",
]


@dataclass(frozen=True)
class PhaseTask:
    label: str
    goal: str
    message: str
    max_duration_s: float
    max_tool_calls: int
    forbidden_tool_prefixes: tuple[str, ...] = ()


PHASE_TASKS = [
    PhaseTask(
        label="phase1_discover_destination",
        goal="Phase 1 should help converge destination without collecting full trip constraints.",
        message=(
            "我想做一次3天左右的城市旅行，偏好美食、购物和轻松街区，但还没确定去哪。"
            "请只帮我收敛目的地选择，不要做具体行程、候选池或交通住宿方案。"
        ),
        max_duration_s=90,
        max_tool_calls=6,
        forbidden_tool_prefixes=("set_candidate_pool", "set_shortlist", "set_skeleton_plans"),
    ),
    PhaseTask(
        label="phase1_confirm_destination",
        goal="Phase 1 should record destination and hand off.",
        message=(
            "我确认目的地选东京。日期、预算、人数和偏好我下一轮再补充。"
        ),
        max_duration_s=60,
        max_tool_calls=3,
    ),
    PhaseTask(
        label="phase2_brief",
        goal="Phase 2 brief should record trip facts and preferences.",
        message=(
            "现在补充基础信息：出发地上海，日期2026-07-10到2026-07-12，2人，预算总计12000元人民币以内。"
            "希望住新宿，交通优先经济舱直飞，节奏 balanced，不要太赶。"
            "偏好拉面、甜品、药妆、东京城市街区和轻松购物。"
        ),
        max_duration_s=75,
        max_tool_calls=6,
    ),
    PhaseTask(
        label="phase2_candidate",
        goal="Phase 2 candidate should build candidate pool with bounded research.",
        message="基础信息确认。请开始整理东京候选池 candidate_pool。",
        max_duration_s=150,
        max_tool_calls=18,
    ),
    PhaseTask(
        label="phase2_shortlist",
        goal="Phase 2 shortlist should filter existing candidates.",
        message="候选池范围确认。请基于已有候选筛选 shortlist。",
        max_duration_s=120,
        max_tool_calls=10,
    ),
    PhaseTask(
        label="phase2_skeleton",
        goal="Phase 2 skeleton should generate options, not lock one for the user.",
        message="短名单确认。请生成2到3套 skeleton 方案供我选择。",
        max_duration_s=150,
        max_tool_calls=12,
    ),
    PhaseTask(
        label="phase2_lock_options",
        goal="Phase 2 lock options should present transport/accommodation options without selecting.",
        message="我倾向方案A。请查询并给出交通和住宿选项，但先不要替我选择或锁定。",
        max_duration_s=180,
        max_tool_calls=18,
        forbidden_tool_prefixes=("select_transport", "select_accommodation"),
    ),
    PhaseTask(
        label="phase2_lock_confirm",
        goal="Phase 2 lock confirmation may lock only after explicit user choice.",
        message=(
            "我明确确认：选择方案A；交通选你推荐的去程A和回程A；住宿选你推荐的新宿站西口/南口区域。"
            "这次授权你锁定这些选择，并推进到逐日行程。"
        ),
        max_duration_s=120,
        max_tool_calls=8,
    ),
    PhaseTask(
        label="phase3_daily_plans",
        goal="Phase 3 should generate daily plans with balanced pace.",
        message=(
            "确认以上锁定项。请生成完整3天逐日行程，注意 balanced 节奏，每天不要过密，保留通勤时间，"
            "覆盖拉面、甜品、药妆和轻松购物。"
        ),
        max_duration_s=240,
        max_tool_calls=30,
    ),
    PhaseTask(
        label="phase4_deliverables",
        goal="Phase 4 should generate final deliverables and handle uncertain weather as uncertain.",
        message=(
            "确认逐日行程。请生成最终旅行方案和出行清单。天气如果不是目标日期的精确预报，"
            "请明确写“临近出发前再确认”。"
        ),
        max_duration_s=180,
        max_tool_calls=12,
    ),
]


def _load_env_file(path: Path) -> dict[str, str]:
    env = os.environ.copy()
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _health(client: httpx.Client) -> bool:
    try:
        resp = client.get("/health", timeout=2.0)
        return resp.status_code == 200 and resp.json().get("status") == "ok"
    except Exception:
        return False


def _start_backend(port: int) -> subprocess.Popen[str]:
    env = _load_env_file(BACKEND_ENV)
    python_path = str(ROOT / "backend")
    env["PYTHONPATH"] = (
        python_path
        if not env.get("PYTHONPATH")
        else f"{python_path}{os.pathsep}{env['PYTHONPATH']}"
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(ROOT / "backend"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _wait_for_backend(client: httpx.Client, process: subprocess.Popen[str] | None) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _health(client):
            return
        if process is not None and process.poll() is not None:
            output = ""
            if process.stdout is not None:
                output = process.stdout.read()
            raise RuntimeError(f"backend exited early with code {process.returncode}\n{output}")
        time.sleep(0.5)
    raise TimeoutError("backend did not become healthy within 30s")


def _stop_backend(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except Exception:
        process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            process.kill()


def _create_session(client: httpx.Client) -> str:
    resp = client.post("/api/sessions", timeout=20.0)
    resp.raise_for_status()
    return resp.json()["session_id"]


def _get_plan(client: httpx.Client, session_id: str) -> dict[str, Any]:
    resp = client.get(f"/api/plan/{session_id}", timeout=20.0)
    resp.raise_for_status()
    return resp.json()


def _fetch_trace_events(
    client: httpx.Client,
    run_id: str,
    *,
    attempts: int = 6,
    delay: float = 0.5,
) -> list[dict[str, Any]]:
    """Load persisted trace events for a run.

    Events are flushed after the run completes, so retry briefly until present.
    The trace is authoritative for tool usage: unlike the SSE stream, it records
    Phase 3 sub-agent (day-worker) tool calls.
    """
    for _ in range(attempts):
        try:
            resp = client.get(f"/api/traces/{run_id}", timeout=20.0)
            if resp.status_code == 200:
                events = resp.json().get("events") or []
                if events:
                    return events
        except httpx.HTTPError:
            pass
        time.sleep(delay)
    return []


def _grade_report(grader_ran: bool, grades: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "grader_ran": grader_ran,
        "grade_count": len(grades),
        "grade_fails": [
            g.get("rubric_id") for g in grades if g.get("status") == "fail"
        ],
    }


def _event_family_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for event in events:
        counts[str(event.get("event_type") or "unknown")] += 1
    return dict(counts)


def _grade_run(client: httpx.Client, run_id: str) -> tuple[bool, list[dict[str, Any]]]:
    """Run the deterministic trace grader on a run and return its rubric results."""
    try:
        resp = client.post(f"/api/traces/{run_id}/grade", timeout=30.0)
        if resp.status_code == 200:
            return True, resp.json().get("grades") or []
    except httpx.HTTPError:
        pass
    return False, []


def _send_message(
    client: httpx.Client,
    session_id: str,
    task: PhaseTask,
    *,
    timeout: float,
) -> dict[str, Any]:
    counters: Counter[str] = Counter()
    internal_tasks: list[dict[str, Any]] = []
    text_chunks: list[str] = []
    run_id: str | None = None
    events_seen = 0
    start = time.monotonic()

    with client.stream(
        "POST",
        f"/api/chat/{session_id}",
        json={"message": task.message},
        timeout=timeout,
    ) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            events_seen += 1
            event_type = event.get("type", "")
            counters[event_type] += 1
            if event_type in {"text", "text_delta"} and event.get("content"):
                text_chunks.append(event["content"])
            elif event_type == "internal_task":
                internal_task_payload = event.get("task") or {}
                internal_tasks.append(
                    {
                        "kind": internal_task_payload.get("kind"),
                        "status": internal_task_payload.get("status"),
                        "message": internal_task_payload.get("message"),
                        "reason": (internal_task_payload.get("result") or {}).get("reason"),
                    }
                )
            elif event_type == "done":
                run_id = event.get("run_id")

    duration_s = round(time.monotonic() - start, 2)
    plan = _get_plan(client, session_id)

    # Authoritative tool audit comes from the persisted trace, not the SSE stream:
    # the stream does not surface Phase 3 sub-agent tool calls.
    trace_events = _fetch_trace_events(client, run_id) if run_id else []
    event_families = _event_family_counts(trace_events)
    audit = audit_events(
        trace_events,
        forbidden_prefixes=task.forbidden_tool_prefixes,
        max_tool_calls=task.max_tool_calls,
    )
    grader_ran, grades = _grade_run(client, run_id) if run_id else (False, [])
    grade_report = _grade_report(grader_ran, grades)

    warnings = list(audit.warnings)
    if duration_s > task.max_duration_s:
        warnings.append(f"duration_exceeded:{duration_s}s>{task.max_duration_s}s")

    return {
        "label": task.label,
        "goal": task.goal,
        "message": task.message,
        "run_id": run_id,
        "duration_s": duration_s,
        "event_count": events_seen,
        "event_types": dict(counters),
        "sse_tool_count": counters.get("tool_call", 0),
        "trace_tool_count": audit.tool_count,
        "trace_event_families": event_families,
        "tool_names": audit.tool_calls,
        "forbidden_hits": audit.forbidden_hits,
        "over_budget": audit.over_budget,
        "error_stats": [
            {
                "tool": s.tool,
                "error": s.error,
                "total": s.total,
                "codes": list(s.error_codes),
            }
            for s in audit.error_stats
        ],
        "grades": grades,
        **grade_report,
        "internal_tasks": internal_tasks,
        "phase_after": plan.get("phase"),
        "phase2_step_after": plan.get("phase2_step"),
        "has_deliverables_after": bool(plan.get("deliverables")),
        "violations": list(audit.violations),
        "warnings": warnings,
        "text_tail": "".join(text_chunks)[-1200:],
    }


def _fetch_db_summary(session_id: str) -> dict[str, Any]:
    db_path = ROOT / "data" / "sessions.db"
    if not db_path.exists():
        return {"error": "sessions.db not found"}
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        runs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT run_id, status, final_phase, final_phase2_step,
                       total_duration_ms, total_input_tokens, total_output_tokens,
                       total_cost_usd
                FROM trace_runs
                WHERE session_id = ?
                ORDER BY created_at
                """,
                (session_id,),
            )
        ]
        events_by_run = {
            row["run_id"]: row["events"]
            for row in conn.execute(
                """
                SELECT run_id, COUNT(*) AS events
                FROM trace_events
                WHERE run_id IN (
                    SELECT run_id FROM trace_runs WHERE session_id = ?
                )
                GROUP BY run_id
                """,
                (session_id,),
            )
        }
        messages = [
            dict(row)
            for row in conn.execute(
                """
                SELECT role, seq, phase, phase2_step, run_id,
                       substr(content, 1, 180) AS content_preview
                FROM messages
                WHERE session_id = ?
                ORDER BY seq
                """,
                (session_id,),
            )
        ]
    finally:
        conn.close()
    return {
        "runs": runs,
        "events_by_run": events_by_run,
        "messages": messages,
    }


def run_canary(args: argparse.Namespace) -> dict[str, Any]:
    base_url = f"http://127.0.0.1:{args.port}"
    client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(args.timeout))
    process: subprocess.Popen[str] | None = None
    started_backend = False

    if not _health(client):
        if not args.start_backend:
            raise RuntimeError(f"backend is not healthy at {base_url}; pass --start-backend")
        process = _start_backend(args.port)
        started_backend = True
        _wait_for_backend(client, process)

    session_id = _create_session(client)
    turns: list[dict[str, Any]] = []
    try:
        for task in PHASE_TASKS:
            turn = _send_message(
                client,
                session_id,
                task,
                timeout=args.timeout,
            )
            turns.append(turn)
            if turn["phase_after"] == 4 and turn["has_deliverables_after"]:
                break
        final_plan = _get_plan(client, session_id)
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "base_url": base_url,
            "started_backend": started_backend,
            "session_id": session_id,
            "principles": list(CANARY_PRINCIPLES),
            "tasks": [asdict(task) for task in PHASE_TASKS],
            "turns": turns,
            "final": {
                "phase": final_plan.get("phase"),
                "phase2_step": final_plan.get("phase2_step"),
                "has_deliverables": bool(final_plan.get("deliverables")),
                "destination": final_plan.get("destination"),
                "dates": final_plan.get("dates"),
                "daily_plan_count": len(final_plan.get("daily_plans") or []),
            },
            "db_summary": _fetch_db_summary(session_id),
        }
    finally:
        client.close()
        if started_backend and not args.keep_backend:
            _stop_backend(process)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run live Phase 1-4 backend canary")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=420.0)
    parser.add_argument("--start-backend", action="store_true")
    parser.add_argument("--keep-backend", action="store_true")
    parser.add_argument(
        "--output",
        default="",
        help="Output JSON path. Defaults to data/canary_runs/full-phase-<session>.json",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also fail (exit 2) on soft signals: warnings, over-budget, grader fails.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = run_canary(args)

    goal_met = report["final"]["phase"] == 4 and report["final"]["has_deliverables"]
    any_violation = any(turn["violations"] for turn in report["turns"])
    strict_fail = args.strict and any(
        turn["warnings"] or turn["grade_fails"] for turn in report["turns"]
    )
    passed = goal_met and not any_violation and not strict_fail
    report["result"] = {
        "passed": passed,
        "goal_met": goal_met,
        "any_violation": any_violation,
        "strict": args.strict,
        "strict_fail": strict_fail,
    }

    output = Path(args.output) if args.output else REPORT_DIR / f"full-phase-{report['session_id']}.json"
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"session_id={report['session_id']}")
    print(f"report={output}")
    print(f"final={json.dumps(report['final'], ensure_ascii=False)}")
    for turn in report["turns"]:
        extras = []
        if turn["violations"]:
            extras.append(f"violations={turn['violations']}")
        if turn["warnings"]:
            extras.append(f"warnings={turn['warnings']}")
        if turn["grade_fails"]:
            extras.append(f"grade_fails={turn['grade_fails']}")
        if turn["error_stats"]:
            extras.append(f"errors={turn['error_stats']}")
        if turn.get("trace_event_families"):
            extras.append(f"events={turn['trace_event_families']}")
        extra_text = (" " + " ".join(extras)) if extras else ""
        print(
            f"{turn['label']}: {turn['duration_s']}s "
            f"phase={turn['phase_after']} step={turn['phase2_step_after']} "
            f"deliverables={turn['has_deliverables_after']} "
            f"tools(sse={turn['sse_tool_count']},trace={turn['trace_tool_count']})"
            f"{extra_text}"
        )
    print(
        f"RESULT={'PASS' if passed else 'FAIL'} "
        f"goal_met={goal_met} violations={any_violation} strict_fail={strict_fail}"
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
