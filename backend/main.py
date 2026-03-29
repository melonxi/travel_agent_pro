# backend/main.py
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role
from config import load_config
from context.manager import ContextManager
from harness.judge import build_judge_prompt, parse_judge_response
from harness.validator import validate_hard_constraints
from llm.factory import create_llm_provider
from memory.manager import MemoryManager
from phase.router import PhaseRouter
from state.intake import apply_trip_facts
from state.models import TravelPlanState
from state.manager import StateManager
from tools.engine import ToolEngine
from tools.assemble_day_plan import make_assemble_day_plan_tool
from tools.calculate_route import make_calculate_route_tool
from tools.check_availability import make_check_availability_tool
from tools.check_feasibility import make_check_feasibility_tool
from tools.check_weather import make_check_weather_tool
from tools.generate_summary import make_generate_summary_tool
from tools.get_poi_info import make_get_poi_info_tool
from tools.search_accommodations import make_search_accommodations_tool
from tools.search_destinations import make_search_destinations_tool
from tools.search_flights import make_search_flights_tool
from tools.update_plan_state import make_update_plan_state_tool


class ChatRequest(BaseModel):
    message: str
    user_id: str = "default_user"


class BacktrackRequest(BaseModel):
    to_phase: int
    reason: str = ""


def create_app(config_path: str = "config.yaml") -> FastAPI:
    config = load_config(config_path)
    state_mgr = StateManager(data_dir=config.data_dir)
    memory_mgr = MemoryManager(data_dir=config.data_dir)
    phase_router = PhaseRouter()
    context_mgr = ContextManager()

    # Session-level caches
    sessions: dict[str, dict] = {}  # session_id → {plan, messages, agent}

    app = FastAPI(title="Travel Agent Pro")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _build_agent(plan):
        llm = create_llm_provider(config.llm)
        tool_engine = ToolEngine()
        tool_engine.register(make_update_plan_state_tool(plan))
        tool_engine.register(make_search_destinations_tool(config.api_keys))
        tool_engine.register(make_check_feasibility_tool(config.api_keys))
        tool_engine.register(make_search_flights_tool(config.api_keys))
        tool_engine.register(make_search_accommodations_tool(config.api_keys))
        tool_engine.register(make_get_poi_info_tool(config.api_keys))
        tool_engine.register(make_calculate_route_tool(config.api_keys))
        tool_engine.register(make_assemble_day_plan_tool())
        tool_engine.register(make_check_availability_tool(config.api_keys))
        tool_engine.register(make_check_weather_tool(config.api_keys))
        tool_engine.register(make_generate_summary_tool())

        hooks = HookManager()

        async def on_tool_call(**kwargs):
            if kwargs.get("tool_name") == "update_plan_state":
                phase_router.check_and_apply_transition(plan)

        async def on_validate(**kwargs):
            if kwargs.get("tool_name") == "update_plan_state":
                errors = validate_hard_constraints(plan)
                if errors:
                    session = sessions.get(plan.session_id)
                    if session:
                        session["messages"].append(
                            Message(
                                role=Role.SYSTEM,
                                content=f"⚠️ 硬约束冲突，必须修正：\n"
                                + "\n".join(f"- {e}" for e in errors),
                            )
                        )

        async def on_before_llm(**kwargs):
            msgs = kwargs.get("messages")
            if not msgs:
                return
            threshold = int(config.llm.max_tokens * config.context_compression_threshold)
            if not context_mgr.should_compress(msgs, threshold):
                return
            must_keep, compressible = context_mgr.classify_messages(msgs)
            if len(compressible) <= 2:
                return
            # Summarize compressible messages into one
            summary_parts = []
            for m in compressible:
                if m.content and m.role in (Role.USER, Role.ASSISTANT):
                    label = "用户" if m.role == Role.USER else "助手"
                    summary_parts.append(f"{label}: {m.content[:200]}")
            summary = Message(
                role=Role.SYSTEM,
                content=f"[对话摘要]\n" + "\n".join(summary_parts[-10:]),
            )
            # Rebuild: system msg + must_keep + summary + last 4 messages
            sys_msg = msgs[0] if msgs[0].role == Role.SYSTEM else None
            recent = msgs[-4:]
            msgs.clear()
            if sys_msg:
                msgs.append(sys_msg)
            for m in must_keep:
                if m not in msgs:
                    msgs.append(m)
            msgs.append(summary)
            for m in recent:
                if m not in msgs:
                    msgs.append(m)

        hooks.register("before_llm_call", on_before_llm)

        async def on_soft_judge(**kwargs):
            tool_name = kwargs.get("tool_name")
            if tool_name not in ("assemble_day_plan", "generate_summary"):
                return
            if not plan.daily_plans:
                return
            session = sessions.get(plan.session_id)
            if not session:
                return
            prefs = {p.key: p.value for p in plan.preferences}
            prompt_text = build_judge_prompt(plan.to_dict(), prefs)
            judge_llm = create_llm_provider(config.llm)
            judge_msgs = [
                Message(role=Role.SYSTEM, content="你是旅行行程质量评估专家。"),
                Message(role=Role.USER, content=prompt_text),
            ]
            result_parts: list[str] = []
            async for chunk in judge_llm.chat(judge_msgs, tools=[], stream=True):
                if chunk.content:
                    result_parts.append(chunk.content)
            score = parse_judge_response("".join(result_parts))
            if score.suggestions:
                suggestion_text = "\n".join(f"- {s}" for s in score.suggestions)
                session["messages"].append(
                    Message(
                        role=Role.SYSTEM,
                        content=f"💡 行程质量评估（{score.overall:.1f}/5）：\n{suggestion_text}",
                    )
                )

        hooks.register("after_tool_call", on_tool_call)
        hooks.register("after_tool_call", on_validate)
        hooks.register("after_tool_call", on_soft_judge)

        return AgentLoop(
            llm=llm,
            tool_engine=tool_engine,
            hooks=hooks,
            max_retries=config.max_retries,
        )

    # Backtrack detection patterns
    _BACKTRACK_PATTERNS: dict[int, list[str]] = {
        1: ["重新开始", "从头来", "换个需求"],
        2: ["换个目的地", "不想去这里", "不去了", "换地方"],
        3: ["改日期", "换时间", "日期不对"],
        4: ["换住宿", "不住这", "换个区域"],
    }

    def _detect_backtrack(message: str, plan: TravelPlanState) -> int | None:
        for target_phase, patterns in _BACKTRACK_PATTERNS.items():
            if target_phase >= plan.phase:
                continue
            if any(p in message for p in patterns):
                return target_phase
        return None

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/sessions")
    async def create_session():
        plan = await state_mgr.create_session()
        agent = _build_agent(plan)
        sessions[plan.session_id] = {
            "plan": plan,
            "messages": [],
            "agent": agent,
        }
        return {"session_id": plan.session_id, "phase": plan.phase}

    @app.get("/api/plan/{session_id}")
    async def get_plan(session_id: str):
        session = sessions.get(session_id)
        if not session:
            try:
                plan = await state_mgr.load(session_id)
                return plan.to_dict()
            except (FileNotFoundError, ValueError):
                raise HTTPException(status_code=404, detail="Session not found")
        return session["plan"].to_dict()

    @app.post("/api/backtrack/{session_id}")
    async def backtrack(session_id: str, req: BacktrackRequest):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        plan = session["plan"]
        if req.to_phase >= plan.phase:
            raise HTTPException(
                status_code=400, detail="只能回退到更早的阶段"
            )
        snapshot_path = await state_mgr.save_snapshot(plan)
        phase_router.prepare_backtrack(
            plan, req.to_phase, req.reason or "用户主动回退", snapshot_path
        )
        await state_mgr.save(plan)
        session["agent"] = _build_agent(plan)
        return {"phase": plan.phase, "plan": plan.to_dict()}

    @app.post("/api/chat/{session_id}")
    async def chat(session_id: str, req: ChatRequest):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        plan = session["plan"]
        messages = session["messages"]
        agent = session["agent"]

        # Detect implicit backtrack from user message
        backtrack_target = _detect_backtrack(req.message, plan)
        if backtrack_target is not None:
            snapshot_path = await state_mgr.save_snapshot(plan)
            phase_router.prepare_backtrack(
                plan, backtrack_target, f"用户意图回退：{req.message[:50]}", snapshot_path
            )
            session["agent"] = _build_agent(plan)
            agent = session["agent"]
        else:
            # Only extract trip facts when NOT backtracking
            updated_fields = apply_trip_facts(plan, req.message)
            if updated_fields:
                phase_router.check_and_apply_transition(plan)

        # Build system message
        phase_prompt = phase_router.get_prompt(plan.phase)
        memory = await memory_mgr.load(req.user_id)
        user_summary = memory_mgr.generate_summary(memory)
        sys_msg = context_mgr.build_system_message(plan, phase_prompt, user_summary)

        # Prepend system message (replace previous one)
        if messages and messages[0].role == Role.SYSTEM:
            messages[0] = sys_msg
        else:
            messages.insert(0, sys_msg)

        # Add user message
        messages.append(Message(role=Role.USER, content=req.message))

        async def event_stream():
            async for chunk in agent.run(messages, phase=plan.phase):
                event_type = (
                    "tool_call"
                    if chunk.tool_call and chunk.type.value == "tool_call_start"
                    else chunk.type.value
                )
                event_data = {"type": event_type}
                if chunk.content:
                    event_data["content"] = chunk.content
                if chunk.tool_call:
                    event_data["tool_call"] = {
                        "name": chunk.tool_call.name,
                        "arguments": chunk.tool_call.arguments,
                    }
                yield json.dumps(event_data, ensure_ascii=False)

            # After agent completes, save state and send final plan update
            await state_mgr.save(plan)
            yield json.dumps(
                {
                    "type": "state_update",
                    "plan": plan.to_dict(),
                },
                ensure_ascii=False,
            )

        return EventSourceResponse(event_stream())

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
