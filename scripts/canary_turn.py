#!/usr/bin/env python3
"""Single-turn canary driver.

Send ONE user message to the live backend and report the agent's behavior
(text reply, ordered tool calls, tool results, internal tasks, plan state).

Unlike run-full-phase-canary.py this sends NO fixed script -- a human (or an
LLM playing the user) reads the agent's actual reply before composing the next
turn. This is the manual / adaptive-user canary driver.

Usage:
  python scripts/canary_turn.py --new            # create a session, print its id
  python scripts/canary_turn.py --session <id> --message "..."   # one turn
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from typing import Any

import httpx


def create_session(client: httpx.Client) -> str:
    resp = client.post("/api/sessions", timeout=20.0)
    resp.raise_for_status()
    return resp.json()["session_id"]


def get_plan(client: httpx.Client, session_id: str) -> dict[str, Any]:
    resp = client.get(f"/api/plan/{session_id}", timeout=20.0)
    resp.raise_for_status()
    return resp.json()


def send(client: httpx.Client, session_id: str, message: str, timeout: float) -> dict[str, Any]:
    counters: Counter[str] = Counter()
    tools: list[dict[str, Any]] = []
    internal: list[dict[str, Any]] = []
    text: list[str] = []
    id_to_name: dict[str, str] = {}
    start = time.monotonic()
    with client.stream(
        "POST", f"/api/chat/{session_id}", json={"message": message}, timeout=timeout
    ) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            etype = ev.get("type", "")
            counters[etype] += 1
            if etype in {"text", "text_delta"} and ev.get("content"):
                text.append(ev["content"])
            elif etype == "tool_call":
                tc = ev.get("tool_call") or {}
                if tc.get("id") and tc.get("name"):
                    id_to_name[tc["id"]] = tc["name"]
                tools.append({"k": "call", "name": tc.get("name"), "label": tc.get("human_label")})
            elif etype == "tool_result":
                res = ev.get("tool_result") or {}
                tools.append(
                    {
                        "k": "result",
                        "name": id_to_name.get(res.get("tool_call_id")),
                        "status": res.get("status"),
                        "err": res.get("error_code"),
                    }
                )
            elif etype == "internal_task":
                p = ev.get("task") or {}
                internal.append({"kind": p.get("kind"), "status": p.get("status")})
    duration_s = round(time.monotonic() - start, 2)
    plan = get_plan(client, session_id)
    call_names = [t["name"] for t in tools if t["k"] == "call" and t.get("name")]
    return {
        "duration_s": duration_s,
        "event_types": dict(counters),
        "tool_calls": call_names,
        "tool_results": [t for t in tools if t["k"] == "result"],
        "internal_tasks": internal,
        "phase": plan.get("phase"),
        "phase2_step": plan.get("phase2_step"),
        "has_deliverables": bool(plan.get("deliverables")),
        "destination": plan.get("destination"),
        "daily_plan_count": len(plan.get("daily_plans") or []),
        "text": "".join(text),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--session", default="")
    ap.add_argument("--message", default="")
    ap.add_argument("--timeout", type=float, default=420.0)
    ap.add_argument("--new", action="store_true", help="create a session and exit")
    args = ap.parse_args()

    client = httpx.Client(base_url=f"http://127.0.0.1:{args.port}")
    try:
        session_id = args.session or create_session(client)
        if args.new and not args.message:
            print(json.dumps({"session_id": session_id}, ensure_ascii=False))
            return 0
        result = send(client, session_id, args.message, args.timeout)
        result["session_id"] = session_id
        text = result.pop("text")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("\n===== AGENT TEXT =====\n")
        print(text)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
