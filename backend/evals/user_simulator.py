# backend/evals/user_simulator.py
"""LLM user-simulator for the adaptive phase canary.

The simulator plays a real user: each turn it reads the agent's actual reply and
the current plan state, then emits the next user message. Unlike a fixed script
it can answer the agent's questions and re-pick from whatever real options the
agent surfaces, so it does not desync when late-generated options differ from
earlier estimates.

This module holds the pure, unit-testable pieces. The LLM call and the
drive-loop that talk to the running backend live in the canary script.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# Tools that lock an irreversible choice. They must only appear AFTER the
# simulated user explicitly authorizes a lock. Note: *_options tools merely
# present choices and are intentionally excluded.
LOCK_TOOLS = frozenset(
    {"select_skeleton", "select_transport", "select_accommodation", "set_accommodation"}
)

_SIM_TOOL_NAME = "emit_user_turn"


@dataclass
class SimPersona:
    summary: str  # trip facts + preferences, as the user would hold them
    policy: str  # behaviour rules (one step at a time, lock only on options...)


@dataclass
class AgendaStep:
    key: str
    goal: str
    forbidden_prefixes: tuple[str, ...] = ()
    is_lock_step: bool = False
    satisfied: Callable[[dict], bool] = field(default=lambda plan: False)


@dataclass
class SimTurn:
    message: str
    authorize_lock: bool = False
    done: bool = False
    reason: str = ""


def advance_index(agenda: list[AgendaStep], idx: int, plan_state: dict) -> int:
    """Advance the agenda cursor past every step already satisfied by the plan.

    The cursor is monotonic and clamps at the final step.
    """
    while idx < len(agenda) - 1 and agenda[idx].satisfied(plan_state):
        idx += 1
    return idx


def parse_sim_output(args: dict[str, Any]) -> SimTurn:
    message = (args or {}).get("message")
    if not message or not str(message).strip():
        raise ValueError("simulator output missing required 'message'")
    return SimTurn(
        message=str(message),
        authorize_lock=bool(args.get("authorize_lock", False)),
        done=bool(args.get("done", False)),
        reason=str(args.get("reason", "")),
    )


def build_sim_tool() -> dict[str, Any]:
    return {
        "name": _SIM_TOOL_NAME,
        "description": "作为用户发出下一句话，并标注是否授权锁定、是否结束。",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "你作为用户这一轮要对 agent 说的话（自然中文）。",
                },
                "authorize_lock": {
                    "type": "boolean",
                    "description": "这句话是否在明确授权 agent 锁定方案/交通/住宿。只有 agent 已经给出具体选项、且你决定从中选定时才为 true。",
                },
                "done": {
                    "type": "boolean",
                    "description": "你认为目标已达成、可以结束对话。",
                },
                "reason": {
                    "type": "string",
                    "description": "一句话说明你这轮为什么这么回应。",
                },
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    }


def sim_tool_name() -> str:
    return _SIM_TOOL_NAME


def _plan_summary(plan_state: dict) -> str:
    keys = ("phase", "phase2_step", "destination", "has_deliverables")
    return ", ".join(f"{k}={plan_state.get(k)}" for k in keys)


def build_sim_prompt(
    persona: SimPersona,
    step: AgendaStep,
    agent_reply: str,
    plan_state: dict,
) -> str:
    return f"""你在扮演一个真实的旅行用户，正在和一个旅行规划 agent 对话。

## 你的人设（始终不变）
{persona.summary}

## 你的行为策略
{persona.policy}

## 本轮你要推进的目标
{step.goal}

## agent 刚才的回复
{agent_reply or "（这是对话开始，agent 还没有回复）"}

## 当前规划状态（只读）
{_plan_summary(plan_state)}

## 输出要求
读完 agent 的回复后，用 `{_SIM_TOOL_NAME}` 工具发出你下一句话。
- 只推进上面这一个目标，不要主动要求 agent 去做属于后续步骤的事。
- 如果 agent 在反问你，就如实回答它的问题。
- 关于锁定：只有当 agent 已经给出了**具体选项/方案**、并且你决定从中选定时，
  才把 `authorize_lock` 置为 true；在 agent 给出选项之前绝不授权锁定。
- 如果 agent 给出的真实选项和你之前以为的不一样，就从真实选项里重新挑，不要坚持引用不存在的选项。
- 目标全部达成（已拿到最终行程与交付物）时把 `done` 置为 true。"""


def lock_consent_violations(tool_calls: list[str], *, authorized: bool) -> list[str]:
    """Return a violation for every lock tool used without prior authorization."""
    if authorized:
        return []
    return [
        f"lock_without_consent:{name}"
        for name in tool_calls
        if name in LOCK_TOOLS
    ]


async def run_sim_turn(
    llm,
    persona: SimPersona,
    step: AgendaStep,
    agent_reply: str,
    plan_state: dict,
) -> SimTurn:
    """Ask the simulator LLM for the next user turn via a forced structured tool."""
    from agent.types import Message, Role
    from llm.errors import LLMError, LLMErrorCode
    from llm.types import ChunkType

    tool = build_sim_tool()
    messages = [
        Message(
            role=Role.USER,
            content=build_sim_prompt(persona, step, agent_reply, plan_state),
        )
    ]
    forced = {"type": "function", "function": {"name": tool["name"]}}

    def _unsupported_tool_choice(exc: LLMError) -> bool:
        if exc.code != LLMErrorCode.BAD_REQUEST:
            return False
        text = f"{exc} {exc.raw_error}".lower()
        return "tool_choice" in text and "not support" in text

    async def _collect(tool_choice) -> dict | None:
        async for chunk in llm.chat(
            messages, tools=[tool], stream=True, tool_choice=tool_choice
        ):
            if (
                chunk.type == ChunkType.TOOL_CALL_START
                and chunk.tool_call
                and chunk.tool_call.name == tool["name"]
            ):
                return chunk.tool_call.arguments
        return None

    try:
        args = await _collect(forced)
    except LLMError as exc:
        # Some providers (e.g. DeepSeek thinking mode) reject forced tool_choice.
        if not _unsupported_tool_choice(exc):
            raise
        args = None
    if args is None:
        # Retry letting the model choose the tool itself.
        args = await _collect(None)
    if args is None:
        raise ValueError("simulator did not emit a tool call")
    return parse_sim_output(args)
