# backend/main.py
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role
from config import load_config
from telemetry import setup_telemetry
from context.manager import ContextManager
from harness.judge import build_judge_prompt, parse_judge_response
from harness.validator import validate_hard_constraints
from llm.factory import create_llm_provider
from llm.types import ChunkType
from memory.manager import MemoryManager
from phase.router import PhaseRouter
from state.intake import extract_trip_facts
from state.models import TravelPlanState
from state.manager import StateManager
from tools.engine import ToolEngine
from tools.assemble_day_plan import make_assemble_day_plan_tool
from tools.calculate_route import make_calculate_route_tool
from tools.check_availability import make_check_availability_tool
from tools.check_weather import make_check_weather_tool
from tools.generate_summary import make_generate_summary_tool
from tools.get_poi_info import make_get_poi_info_tool
from tools.search_accommodations import make_search_accommodations_tool
from tools.search_flights import make_search_flights_tool
from tools.search_trains import make_search_trains_tool
from tools.ai_travel_search import make_ai_travel_search_tool
from tools.update_plan_state import make_update_plan_state_tool
from tools.quick_travel_search import make_quick_travel_search_tool
from tools.search_travel_services import make_search_travel_services_tool
from tools.web_search import make_web_search_tool
from tools.xiaohongshu_search import make_xiaohongshu_search_tool


class ChatRequest(BaseModel):
    message: str
    user_id: str = "default_user"


class BacktrackRequest(BaseModel):
    to_phase: int
    reason: str = ""


def _should_replace_dates_with_message_dates(
    current_dates,
    message_dates,
    *,
    today: date,
) -> bool:
    if message_dates is None:
        return False
    if current_dates is None:
        return True

    try:
        current_start = date.fromisoformat(current_dates.start)
        message_start = date.fromisoformat(message_dates.start)
    except ValueError:
        return False

    return current_start < today <= message_start


def _apply_message_fallbacks(
    plan: TravelPlanState,
    message: str,
    phase_router: PhaseRouter,
    *,
    today: date | None = None,
) -> None:
    today = today or date.today()
    facts = extract_trip_facts(message, today=today)
    changed = False

    destination = facts.get("destination")
    if destination and not plan.destination:
        plan.destination = destination
        changed = True

    budget = facts.get("budget")
    if budget and not plan.budget:
        plan.budget = budget
        changed = True

    travelers = facts.get("travelers")
    if travelers and not plan.travelers:
        plan.travelers = travelers
        changed = True

    message_dates = facts.get("dates")
    if _should_replace_dates_with_message_dates(
        plan.dates,
        message_dates,
        today=today,
    ):
        plan.dates = message_dates
        changed = True

    if changed:
        phase_router.check_and_apply_transition(plan)


def create_app(config_path: str = "config.yaml") -> FastAPI:
    config = load_config(config_path)
    state_mgr = StateManager(data_dir=config.data_dir)
    memory_mgr = MemoryManager(data_dir=config.data_dir)
    phase_router = PhaseRouter()
    context_mgr = ContextManager()

    # Resolved context window — will be updated at startup via model query
    resolved_context_window: dict[str, int] = {"value": config.llm.context_window}

    # Session-level caches
    sessions: dict[str, dict] = {}  # session_id → {plan, messages, agent}

    async def _probe_context_window() -> None:
        """Query model API for actual context window, fallback to config default."""
        llm = create_llm_provider(config.llm)
        try:
            queried = await llm.get_context_window()
            if queried and queried > 0:
                resolved_context_window["value"] = queried
                import logging
                logging.getLogger("travel-agent-pro").info(
                    f"Context window from model API: {queried}"
                )
        except Exception:
            pass  # keep config default

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await _probe_context_window()
        yield

    app = FastAPI(title="Travel Agent Pro", lifespan=lifespan)
    setup_telemetry(app, config.telemetry)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _build_agent(plan, user_id: str, compression_events: list[dict] | None = None):
        llm = create_llm_provider(config.llm)
        llm_factory = lambda: create_llm_provider(config.llm)
        tool_engine = ToolEngine()

        # Create FlyAI client if enabled
        flyai_client = None
        if config.flyai.enabled:
            from tools.flyai_client import FlyAIClient

            flyai_client = FlyAIClient(
                timeout=config.flyai.cli_timeout,
                api_key=config.flyai.api_key,
            )

        tool_engine.register(make_update_plan_state_tool(plan))
        tool_engine.register(make_search_flights_tool(config.api_keys, flyai_client))
        tool_engine.register(make_search_trains_tool(flyai_client))
        tool_engine.register(make_ai_travel_search_tool(flyai_client))
        tool_engine.register(
            make_search_accommodations_tool(config.api_keys, flyai_client)
        )
        tool_engine.register(make_get_poi_info_tool(config.api_keys, flyai_client))
        tool_engine.register(make_calculate_route_tool(config.api_keys))
        tool_engine.register(make_assemble_day_plan_tool())
        tool_engine.register(make_check_availability_tool(config.api_keys))
        tool_engine.register(make_check_weather_tool(config.api_keys))
        tool_engine.register(make_generate_summary_tool())
        tool_engine.register(make_quick_travel_search_tool(flyai_client))
        tool_engine.register(make_search_travel_services_tool(flyai_client))
        tool_engine.register(make_web_search_tool(config.api_keys))
        tool_engine.register(make_xiaohongshu_search_tool(config.xhs))

        hooks = HookManager()

        async def on_tool_call(**kwargs):
            if kwargs.get("tool_name") == "update_plan_state":
                result = kwargs.get("result")
                if result and result.data and result.data.get("backtracked"):
                    session = sessions.get(plan.session_id)
                    if session:
                        session["needs_rebuild"] = True
                return

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
            threshold = int(
                resolved_context_window["value"] * config.context_compression_threshold
            )
            if not context_mgr.should_compress(msgs, threshold):
                return
            must_keep, compressible = context_mgr.classify_messages(msgs)
            if len(compressible) <= 2:
                return

            message_count_before = len(msgs)
            estimated_tokens_before = sum(len(m.content or "") // 3 for m in msgs)

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

            message_count_after = len(msgs)
            if compression_events is not None:
                compression_events.append({
                    "message_count_before": message_count_before,
                    "message_count_after": message_count_after,
                    "must_keep_count": len(must_keep),
                    "compressed_count": len(compressible),
                    "estimated_tokens_before": estimated_tokens_before,
                    "reason": f"对话消息数 {message_count_before} 条，"
                              f"估算 token {estimated_tokens_before} 超过阈值 {threshold}",
                })

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
            phase_router=phase_router,
            context_manager=context_mgr,
            plan=plan,
            llm_factory=llm_factory,
            memory_mgr=memory_mgr,
            user_id=user_id,
            compression_events=compression_events,
        )

    # Backtrack detection patterns
    _BACKTRACK_PATTERNS: dict[int, list[str]] = {
        1: [
            "重新开始",
            "从头来",
            "换个需求",
            "换个目的地",
            "不想去这里",
            "不去了",
            "换地方",
        ],
        3: ["改日期", "换时间", "日期不对", "换住宿", "不住这", "换个区域"],
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
        compression_events: list[dict] = []
        agent = _build_agent(plan, "default_user", compression_events=compression_events)
        sessions[plan.session_id] = {
            "plan": plan,
            "messages": [],
            "agent": agent,
            "needs_rebuild": False,
            "user_id": "default_user",
            "compression_events": compression_events,
        }
        return {"session_id": plan.session_id, "phase": plan.phase}

    @app.get("/api/plan/{session_id}")
    async def get_plan(session_id: str):
        session = sessions.get(session_id)
        if not session:
            try:
                plan = await state_mgr.load(session_id)
                phase_router.sync_phase_state(plan)
                return plan.to_dict()
            except (FileNotFoundError, ValueError):
                raise HTTPException(status_code=404, detail="Session not found")
        phase_router.sync_phase_state(session["plan"])
        return session["plan"].to_dict()

    @app.post("/api/backtrack/{session_id}")
    async def backtrack(session_id: str, req: BacktrackRequest):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        plan = session["plan"]
        if req.to_phase == 2:
            req.to_phase = 1
        if req.to_phase >= plan.phase:
            raise HTTPException(status_code=400, detail="只能回退到更早的阶段")
        snapshot_path = await state_mgr.save_snapshot(plan)
        phase_router.prepare_backtrack(
            plan, req.to_phase, req.reason or "用户主动回退", snapshot_path
        )
        await state_mgr.save(plan)
        session["agent"] = _build_agent(plan, session.get("user_id", "default_user"), compression_events=session.get("compression_events"))
        session["needs_rebuild"] = False
        return {"phase": plan.phase, "plan": plan.to_dict()}

    @app.post("/api/chat/{session_id}")
    async def chat(session_id: str, req: ChatRequest):
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        plan = session["plan"]
        messages = session["messages"]
        session["user_id"] = req.user_id

        # 检查是否需要重建 agent（上一轮回退导致）
        if session.get("needs_rebuild"):
            session["agent"] = _build_agent(plan, session["user_id"], compression_events=session.get("compression_events"))
            session["needs_rebuild"] = False

        agent = session["agent"]
        agent.user_id = session["user_id"]

        # Build system message
        phase_router.sync_phase_state(plan)
        phase_prompt = phase_router.get_prompt(plan.phase)
        available_tools = [
            tool["name"]
            for tool in agent.tool_engine.get_tools_for_phase(plan.phase, plan)
        ]
        memory = await memory_mgr.load(req.user_id)
        user_summary = memory_mgr.generate_summary(memory)
        sys_msg = context_mgr.build_system_message(
            plan,
            phase_prompt,
            user_summary,
            available_tools=available_tools,
        )

        # Prepend system message
        if messages and messages[0].role == Role.SYSTEM:
            messages[0] = sys_msg
        else:
            messages.insert(0, sys_msg)

        messages.append(Message(role=Role.USER, content=req.message))

        # 记录 agent.run 之前的 phase，用于判断是否发生了回退
        phase_before_run = plan.phase

        async def event_stream():
            tool_call_names: dict[str, str] = {}
            async for chunk in agent.run(messages, phase=plan.phase):
                if chunk.type.value == "keepalive":
                    yield {"comment": "ping"}
                    continue
                if chunk.type == ChunkType.CONTEXT_COMPRESSION:
                    yield json.dumps({
                        "type": "context_compression",
                        "compression_info": chunk.compression_info,
                    }, ensure_ascii=False)
                    continue
                event_type = (
                    "tool_call"
                    if chunk.tool_call and chunk.type.value == "tool_call_start"
                    else "tool_result"
                    if chunk.tool_result and chunk.type.value == "tool_result"
                    else chunk.type.value
                )
                event_data = {"type": event_type}
                if chunk.content:
                    event_data["content"] = chunk.content
                if chunk.tool_call:
                    tool_call_names[chunk.tool_call.id] = chunk.tool_call.name
                    event_data["tool_call"] = {
                        "id": chunk.tool_call.id,
                        "name": chunk.tool_call.name,
                        "arguments": chunk.tool_call.arguments,
                    }
                if chunk.tool_result:
                    event_data["tool_result"] = {
                        "tool_call_id": chunk.tool_result.tool_call_id,
                        "status": chunk.tool_result.status,
                        "data": chunk.tool_result.data,
                        "error": chunk.tool_result.error,
                        "error_code": chunk.tool_result.error_code,
                        "suggestion": chunk.tool_result.suggestion,
                    }
                yield json.dumps(event_data, ensure_ascii=False)
                if (
                    chunk.tool_result
                    and chunk.tool_result.status == "success"
                    and tool_call_names.get(chunk.tool_result.tool_call_id)
                    == "update_plan_state"
                ):
                    yield json.dumps(
                        {"type": "state_update", "plan": plan.to_dict()},
                        ensure_ascii=False,
                    )

            # Fallback：如果本轮 agent 没触发 backtrack，检查关键词 fallback
            if plan.phase == phase_before_run:
                backtrack_target = _detect_backtrack(req.message, plan)
                if backtrack_target is not None:
                    reason = f"fallback回退：{req.message[:50]}"
                    tool_call_id = f"fallback.update_plan_state:{plan.version}"
                    yield json.dumps(
                        {
                            "type": "tool_call",
                            "tool_call": {
                                "id": tool_call_id,
                                "name": "update_plan_state",
                                "arguments": {
                                    "field": "backtrack",
                                    "value": {
                                        "to_phase": backtrack_target,
                                        "reason": reason,
                                    },
                                },
                            },
                        },
                        ensure_ascii=False,
                    )
                    snapshot_path = await state_mgr.save_snapshot(plan)
                    from_phase = plan.phase
                    phase_router.prepare_backtrack(
                        plan,
                        backtrack_target,
                        reason,
                        snapshot_path,
                    )
                    session["needs_rebuild"] = True
                    yield json.dumps(
                        {
                            "type": "tool_result",
                            "tool_result": {
                                "tool_call_id": tool_call_id,
                                "status": "success",
                                "data": {
                                    "backtracked": True,
                                    "from_phase": from_phase,
                                    "to_phase": backtrack_target,
                                    "reason": reason,
                                    "next_action": "请向用户确认回退结果，不要继续调用其他工具",
                                },
                                "error": None,
                                "error_code": None,
                                "suggestion": None,
                            },
                        },
                        ensure_ascii=False,
                    )

            await state_mgr.save(plan)
            yield json.dumps(
                {"type": "state_update", "plan": plan.to_dict()},
                ensure_ascii=False,
            )

        return EventSourceResponse(event_stream())

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
