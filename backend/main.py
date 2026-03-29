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
from harness.validator import validate_hard_constraints
from llm.factory import create_llm_provider
from memory.manager import MemoryManager
from phase.router import PhaseRouter
from state.manager import StateManager
from tools.engine import ToolEngine
from tools.update_plan_state import make_update_plan_state_tool


class ChatRequest(BaseModel):
    message: str
    user_id: str = "default_user"


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
        # Additional tools would be registered here

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

        hooks.register("after_tool_call", on_tool_call)
        hooks.register("after_tool_call", on_validate)

        return AgentLoop(
            llm=llm,
            tool_engine=tool_engine,
            hooks=hooks,
            max_retries=config.max_retries,
        )

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

    @app.post("/api/chat/{session_id}")
    async def chat(session_id: str, req: ChatRequest):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        plan = session["plan"]
        messages = session["messages"]
        agent = session["agent"]

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
                event_data = {"type": chunk.type.value}
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
