from __future__ import annotations

import pytest

from agent.types import ToolCall
from llm.errors import LLMError, LLMErrorCode
from llm.types import ChunkType, LLMChunk

from evals.user_simulator import (
    AgendaStep,
    SimPersona,
    advance_index,
    build_sim_prompt,
    build_sim_tool,
    lock_consent_violations,
    parse_sim_output,
    run_sim_turn,
)


def _agenda() -> list[AgendaStep]:
    return [
        AgendaStep(key="discover", goal="收敛目的地", satisfied=lambda p: bool(p.get("destination"))),
        AgendaStep(key="brief", goal="补充画像", satisfied=lambda p: p.get("phase2_step") not in (None, "brief")),
        AgendaStep(key="lock", goal="锁定方案", is_lock_step=True, satisfied=lambda p: p.get("has_deliverables")),
    ]


# --- advance_index --------------------------------------------------------

def test_advance_index_moves_past_satisfied_steps():
    agenda = _agenda()
    # destination set -> step0 satisfied; still at brief -> step1 not satisfied
    plan = {"destination": "东京", "phase2_step": "brief", "has_deliverables": False}
    assert advance_index(agenda, 0, plan) == 1


def test_advance_index_stays_when_current_not_satisfied():
    agenda = _agenda()
    plan = {"destination": None, "phase2_step": "brief", "has_deliverables": False}
    assert advance_index(agenda, 0, plan) == 0


def test_advance_index_clamps_at_last_step():
    agenda = _agenda()
    plan = {"destination": "东京", "phase2_step": "lock", "has_deliverables": True}
    assert advance_index(agenda, 0, plan) == len(agenda) - 1


# --- parse_sim_output -----------------------------------------------------

def test_parse_sim_output_full():
    turn = parse_sim_output(
        {"message": "选东京", "authorize_lock": True, "done": False, "reason": "确定了"}
    )
    assert turn.message == "选东京"
    assert turn.authorize_lock is True
    assert turn.done is False
    assert turn.reason == "确定了"


def test_parse_sim_output_defaults_optional_fields():
    turn = parse_sim_output({"message": "继续"})
    assert turn.message == "继续"
    assert turn.authorize_lock is False
    assert turn.done is False
    assert turn.reason == ""


def test_parse_sim_output_requires_message():
    with pytest.raises(ValueError):
        parse_sim_output({"authorize_lock": True})


# --- build_sim_prompt -----------------------------------------------------

def test_build_sim_prompt_includes_context_and_no_premature_lock_rule():
    persona = SimPersona(summary="上海出发2人东京", policy="一次只推进一步")
    step = AgendaStep(key="lock", goal="先看交通住宿选项，别让它替你锁")
    prompt = build_sim_prompt(
        persona,
        step,
        agent_reply="我推荐方案A，你要锁吗？",
        plan_state={"phase": 2, "phase2_step": "skeleton"},
    )
    assert "上海出发2人东京" in prompt
    assert "先看交通住宿选项" in prompt
    assert "我推荐方案A" in prompt
    assert "phase" in prompt and "skeleton" in prompt
    # the core tested discipline: never authorize a lock before options are presented
    assert "authorize_lock" in prompt


# --- lock_consent_violations ---------------------------------------------

def test_lock_without_consent_flagged():
    v = lock_consent_violations(["web_search", "select_transport"], authorized=False)
    assert v == ["lock_without_consent:select_transport"]


def test_lock_with_consent_is_clean():
    v = lock_consent_violations(["select_transport", "set_accommodation"], authorized=True)
    assert v == []


def test_non_lock_tools_never_violate():
    v = lock_consent_violations(["web_search", "set_accommodation_options"], authorized=False)
    assert v == []


# --- build_sim_tool -------------------------------------------------------

def test_build_sim_tool_schema():
    tool = build_sim_tool()
    assert tool["name"]
    props = tool["parameters"]["properties"]
    assert {"message", "authorize_lock", "done", "reason"} <= set(props)
    assert "message" in tool["parameters"]["required"]


# --- run_sim_turn (integration with a fake provider) ----------------------

class _FakeProvider:
    def __init__(self, arguments: dict):
        self.arguments = arguments
        self.captured: dict = {}

    async def chat(self, messages, tools=None, stream=True, tool_choice=None):
        self.captured = {"messages": messages, "tools": tools, "tool_choice": tool_choice}
        yield LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(id="t1", name="emit_user_turn", arguments=self.arguments),
        )


async def test_run_sim_turn_parses_output_and_forces_sim_tool():
    provider = _FakeProvider(
        {"message": "就东京吧", "authorize_lock": False, "done": False, "reason": "定了"}
    )
    persona = SimPersona(summary="上海2人东京", policy="一步步来")
    step = AgendaStep(key="discover", goal="收敛目的地")

    turn = await run_sim_turn(
        provider, persona, step, agent_reply="去哪？", plan_state={"phase": 1}
    )

    assert turn.message == "就东京吧"
    assert turn.authorize_lock is False
    assert provider.captured["tools"][0]["name"] == "emit_user_turn"
    assert provider.captured["tool_choice"]["function"]["name"] == "emit_user_turn"


class _ToolChoiceRejectingProvider:
    """Forced tool_choice raises (like DeepSeek thinking mode); free choice works."""

    def __init__(self, arguments: dict):
        self.arguments = arguments
        self.tool_choices: list = []

    async def chat(self, messages, tools=None, stream=True, tool_choice=None):
        self.tool_choices.append(tool_choice)
        if tool_choice is not None:
            raise LLMError(
                LLMErrorCode.BAD_REQUEST,
                "rejected",
                retryable=False,
                provider="openai",
                model="deepseek",
                raw_error="Thinking mode does not support this tool_choice",
            )
        yield LLMChunk(
            type=ChunkType.TOOL_CALL_START,
            tool_call=ToolCall(id="t1", name="emit_user_turn", arguments=self.arguments),
        )


async def test_run_sim_turn_falls_back_when_tool_choice_unsupported():
    provider = _ToolChoiceRejectingProvider({"message": "就东京吧"})

    turn = await run_sim_turn(
        provider,
        SimPersona(summary="s", policy="p"),
        AgendaStep(key="discover", goal="g"),
        agent_reply="去哪？",
        plan_state={"phase": 1},
    )

    assert turn.message == "就东京吧"
    # tried forced tool_choice first, then fell back to free choice
    assert provider.tool_choices[0] is not None
    assert provider.tool_choices[-1] is None
