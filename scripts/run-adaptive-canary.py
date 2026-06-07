#!/usr/bin/env python3
"""Adaptive phase canary: an LLM plays the user, driving Phase 1->4 live.

Unlike run-full-phase-canary.py (fixed pre-written messages), each turn the
simulator reads the agent's REAL reply plus the current plan state and composes
the next user message. So it answers the agent's questions and re-picks from
whatever real options the agent surfaces -- it does not desync when late-
generated options differ from earlier estimates.

Tool usage is audited from the persisted trace (covers Phase 3 sub-agent calls),
and the lock-without-consent invariant is enforced: a select_*/set_accommodation
that fires in a turn the simulator did not authorize is a hard violation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BACKEND_ENV = BACKEND / ".env"
REPORT_DIR = ROOT / "data" / "canary_runs"

sys.path.insert(0, str(BACKEND))
from evals.canary_audit import audit_events  # noqa: E402
from evals.user_simulator import (  # noqa: E402
    AgendaStep,
    SimPersona,
    advance_index,
    lock_consent_violations,
    run_sim_turn,
)
from config import LLMConfig  # noqa: E402
from llm.factory import create_llm_provider  # noqa: E402


PERSONA = SimPersona(
    summary=(
        "你是从上海出发的旅行用户，2 人同行，想去东京玩 3 天"
        "（2026-07-10 到 2026-07-12），预算两人总计不超过 12000 元人民币。"
        "想住新宿，机票优先经济舱直飞，节奏要 balanced、不要太赶。"
        "偏好：拉面、甜品、药妆、东京的城市街区、轻松逛街。"
        "目的地一开始还没最终敲定，但你心里倾向东京。"
    ),
    policy=(
        "一次只推进当前这一个阶段的目标；绝不主动要求 agent 去做属于后续步骤的事；"
        "只有 agent 已经给出具体选项后，你才考虑授权锁定；agent 反问就如实回答；"
        "如果 agent 给出的真实选项和你之前以为的不一样，就从真实选项里重新挑。"
    ),
)

# phase2_step ordering, so an agenda step is "satisfied" once the plan has moved
# past it -- this also makes the agenda adapt when the agent overruns several
# steps in a single turn.
_P2 = {"brief": 0, "candidate": 1, "shortlist": 2, "skeleton": 3, "lock": 4}


def _phase(plan: dict) -> int:
    return plan.get("phase") or 1


def _p2i(plan: dict) -> int:
    return _P2.get(plan.get("phase2_step") or "brief", 0)


AGENDA: list[AgendaStep] = [
    AgendaStep(
        key="discover",
        goal="只帮你收敛/确定目的地方向，先别做具体行程，也别建候选池。",
        forbidden_prefixes=("set_candidate_pool", "set_shortlist", "set_skeleton_plans"),
        satisfied=lambda p: bool(p.get("destination")),
    ),
    AgendaStep(
        key="brief",
        goal="补充基础信息和偏好（出发地、日期、人数、预算、住宿、节奏、口味），只写画像，先别搜候选。",
        forbidden_prefixes=("set_candidate_pool", "set_shortlist", "set_skeleton_plans"),
        satisfied=lambda p: _phase(p) >= 3 or _p2i(p) >= 1,
    ),
    AgendaStep(
        key="candidate",
        goal="只完成候选池 candidate_pool，先别筛 shortlist、别生成 skeleton。",
        forbidden_prefixes=("set_shortlist", "set_skeleton_plans", "select_skeleton"),
        satisfied=lambda p: _phase(p) >= 3 or _p2i(p) >= 2,
    ),
    AgendaStep(
        key="shortlist",
        goal="基于已有候选筛选 shortlist，先别生成 skeleton、也别锁定。",
        forbidden_prefixes=("set_skeleton_plans", "select_skeleton"),
        satisfied=lambda p: _phase(p) >= 3 or _p2i(p) >= 3,
    ),
    AgendaStep(
        key="skeleton",
        goal="生成 2-3 套 skeleton 方案供你选择，先别替你锁定最终方案。",
        forbidden_prefixes=("select_skeleton",),
        satisfied=lambda p: _phase(p) >= 3 or _p2i(p) >= 4,
    ),
    AgendaStep(
        key="lock",
        goal="先看交通和住宿的真实选项；满意后再明确授权锁定方案/交通/住宿，并推进逐日。",
        is_lock_step=True,
        satisfied=lambda p: _phase(p) >= 3,
    ),
    AgendaStep(
        key="daily",
        goal="确认锁定项，生成完整逐日行程，注意 balanced 节奏、别太密。",
        satisfied=lambda p: _phase(p) >= 4,
    ),
    AgendaStep(
        key="deliverables",
        goal="确认逐日行程，生成最终旅行方案和出行清单；天气若非精确预报要标注'临近出发前再确认'。",
        satisfied=lambda p: bool(p.get("has_deliverables")),
    ),
]


# --------------------------------------------------------------------------
# backend lifecycle (mirrors run-full-phase-canary.py)
# --------------------------------------------------------------------------

def _load_env_into_os() -> None:
    if not BACKEND_ENV.exists():
        return
    for raw in BACKEND_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _health(port: int) -> bool:
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
        return resp.status_code == 200 and resp.json().get("status") == "ok"
    except Exception:
        return False


def _start_backend(port: int) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND) + (
        f"{os.pathsep}{env['PYTHONPATH']}" if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(port)],
        cwd=str(BACKEND), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _health(port):
            return proc
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"backend exited early ({proc.returncode})\n{out}")
        time.sleep(0.5)
    raise TimeoutError("backend did not become healthy within 30s")


def _stop_backend(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()


def _make_provider(args: argparse.Namespace):
    provider = args.sim_provider or os.environ.get("DEFAULT_PROVIDER", "openai")
    if args.sim_model:
        model = args.sim_model
    elif provider == "anthropic":
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    else:
        model = os.environ.get("OPENAI_MODEL", "gpt-4o")
    return create_llm_provider(
        LLMConfig(provider=provider, model=model, temperature=0.3)
    )


# --------------------------------------------------------------------------
# async HTTP helpers (drive the running backend)
# --------------------------------------------------------------------------

async def _create_session(client: httpx.AsyncClient) -> str:
    resp = await client.post("/api/sessions", timeout=20.0)
    resp.raise_for_status()
    return resp.json()["session_id"]


async def _get_plan(client: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
    resp = await client.get(f"/api/plan/{session_id}", timeout=20.0)
    resp.raise_for_status()
    return resp.json()


async def _send(
    client: httpx.AsyncClient, session_id: str, message: str, timeout: float
) -> tuple[str | None, str, dict, float]:
    text_chunks: list[str] = []
    run_id: str | None = None
    start = time.monotonic()
    async with client.stream(
        "POST", f"/api/chat/{session_id}", json={"message": message}, timeout=timeout
    ) as stream:
        stream.raise_for_status()
        async for line in stream.aiter_lines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            etype = event.get("type", "")
            if etype in {"text", "text_delta"} and event.get("content"):
                text_chunks.append(event["content"])
            elif etype == "done":
                run_id = event.get("run_id")
    duration_s = round(time.monotonic() - start, 2)
    plan = await _get_plan(client, session_id)
    return run_id, "".join(text_chunks), plan, duration_s


async def _fetch_trace_events(
    client: httpx.AsyncClient, run_id: str, *, attempts: int = 6, delay: float = 0.5
) -> list[dict[str, Any]]:
    for _ in range(attempts):
        try:
            resp = await client.get(f"/api/traces/{run_id}", timeout=20.0)
            if resp.status_code == 200:
                events = resp.json().get("events") or []
                if events:
                    return events
        except httpx.HTTPError:
            pass
        await asyncio.sleep(delay)
    return []


async def _grade_run(client: httpx.AsyncClient, run_id: str) -> list[dict[str, Any]]:
    try:
        resp = await client.post(f"/api/traces/{run_id}/grade", timeout=30.0)
        if resp.status_code == 200:
            return resp.json().get("grades") or []
    except httpx.HTTPError:
        pass
    return []


# --------------------------------------------------------------------------
# drive loop
# --------------------------------------------------------------------------

async def drive(args: argparse.Namespace, provider) -> dict[str, Any]:
    base_url = f"http://127.0.0.1:{args.port}"
    async with httpx.AsyncClient(
        base_url=base_url, timeout=httpx.Timeout(args.timeout)
    ) as client:
        session_id = await _create_session(client)
        turns: list[dict[str, Any]] = []
        cursor = 0
        last_reply = ""

        for _ in range(args.max_turns):
            plan = await _get_plan(client, session_id)
            cursor = advance_index(AGENDA, cursor, plan)
            step = AGENDA[cursor]

            sim = await run_sim_turn(provider, PERSONA, step, last_reply, plan)
            if sim.done:
                turns.append({"kind": "sim_done", "step": step.key, "reason": sim.reason})
                break

            run_id, agent_text, plan_after, duration_s = await _send(
                client, session_id, sim.message, args.timeout
            )
            events = await _fetch_trace_events(client, run_id) if run_id else []
            audit = audit_events(events, forbidden_prefixes=step.forbidden_prefixes)
            consent = lock_consent_violations(
                audit.tool_calls, authorized=sim.authorize_lock
            )
            grades = await _grade_run(client, run_id) if run_id else []
            grade_fails = [g.get("rubric_id") for g in grades if g.get("status") == "fail"]

            turns.append(
                {
                    "step": step.key,
                    "goal": step.goal,
                    "sim_message": sim.message,
                    "authorize_lock": sim.authorize_lock,
                    "sim_reason": sim.reason,
                    "run_id": run_id,
                    "duration_s": duration_s,
                    "trace_tool_count": audit.tool_count,
                    "tool_calls": audit.tool_calls,
                    "error_stats": [
                        {"tool": s.tool, "error": s.error, "total": s.total,
                         "codes": list(s.error_codes)}
                        for s in audit.error_stats
                    ],
                    "grade_fails": grade_fails,
                    "phase_after": plan_after.get("phase"),
                    "phase2_step_after": plan_after.get("phase2_step"),
                    "has_deliverables_after": bool(plan_after.get("deliverables")),
                    "violations": list(audit.violations) + consent,
                    "warnings": list(audit.warnings),
                    "agent_reply_tail": agent_text[-800:],
                }
            )
            last_reply = agent_text
            if plan_after.get("phase") == 4 and plan_after.get("deliverables"):
                break

        final_plan = await _get_plan(client, session_id)
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": "adaptive_user_simulator",
            "session_id": session_id,
            "persona": {"summary": PERSONA.summary, "policy": PERSONA.policy},
            "turns": turns,
            "final": {
                "phase": final_plan.get("phase"),
                "phase2_step": final_plan.get("phase2_step"),
                "has_deliverables": bool(final_plan.get("deliverables")),
                "destination": final_plan.get("destination"),
                "daily_plan_count": len(final_plan.get("daily_plans") or []),
            },
        }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Adaptive LLM-user phase canary")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--timeout", type=float, default=420.0)
    p.add_argument("--max-turns", type=int, default=14)
    p.add_argument("--start-backend", action="store_true")
    p.add_argument("--keep-backend", action="store_true")
    p.add_argument("--sim-provider", default="", help="override simulator provider")
    p.add_argument("--sim-model", default="", help="override simulator model")
    p.add_argument("--output", default="")
    p.add_argument(
        "--strict",
        action="store_true",
        help="Also fail on soft signals: warnings, grader fails.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    _load_env_into_os()
    provider = _make_provider(args)

    proc: subprocess.Popen[str] | None = None
    started = False
    if not _health(args.port):
        if not args.start_backend:
            raise RuntimeError(
                f"backend not healthy at :{args.port}; pass --start-backend"
            )
        proc = _start_backend(args.port)
        started = True
    try:
        report = asyncio.run(drive(args, provider))
    finally:
        if started and not args.keep_backend:
            _stop_backend(proc)

    goal_met = report["final"]["phase"] == 4 and report["final"]["has_deliverables"]
    any_violation = any(t.get("violations") for t in report["turns"])
    strict_fail = args.strict and any(
        t.get("warnings") or t.get("grade_fails") for t in report["turns"]
    )
    passed = goal_met and not any_violation and not strict_fail
    report["result"] = {
        "passed": passed,
        "goal_met": goal_met,
        "any_violation": any_violation,
        "strict": args.strict,
        "strict_fail": strict_fail,
    }

    output = (
        Path(args.output)
        if args.output
        else REPORT_DIR / f"adaptive-{report['session_id']}.json"
    )
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"session_id={report['session_id']}")
    print(f"report={output}")
    print(f"final={json.dumps(report['final'], ensure_ascii=False)}")
    for turn in report["turns"]:
        if turn.get("kind") == "sim_done":
            print(f"[sim_done@{turn['step']}] {turn['reason']}")
            continue
        extras = []
        if turn["violations"]:
            extras.append(f"violations={turn['violations']}")
        if turn["warnings"]:
            extras.append(f"warnings={turn['warnings']}")
        if turn["grade_fails"]:
            extras.append(f"grade_fails={turn['grade_fails']}")
        if turn["error_stats"]:
            extras.append(f"errors={turn['error_stats']}")
        extra_text = (" " + " ".join(extras)) if extras else ""
        print(
            f"{turn['step']}: {turn['duration_s']}s "
            f"phase={turn['phase_after']} step={turn['phase2_step_after']} "
            f"lock_auth={turn['authorize_lock']} "
            f"tools={turn['trace_tool_count']}{extra_text}"
        )
    print(
        f"RESULT={'PASS' if passed else 'FAIL'} "
        f"goal_met={goal_met} violations={any_violation} strict_fail={strict_fail}"
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
