from agent.compaction import compact_messages_for_prompt, estimate_messages_tokens
from agent.types import Message, Role
from api.orchestration.agent.hooks import (
    _PROMPT_TOKEN_ANCHORS_KEY,
    _estimate_prompt_tokens_with_anchor,
    _prompt_token_phase_key,
    _record_prompt_token_anchor_candidate,
    _refresh_prompt_token_anchor_from_usage,
)


class _FakeCall:
    def __init__(self, input_tokens: int):
        self.input_tokens = input_tokens


class _FakeStats:
    def __init__(self):
        self.llm_calls: list[_FakeCall] = []


def _msgs(n_chars: int) -> list[Message]:
    return [Message(role=Role.USER, content="a" * n_chars)]


def _phase_key(tools: list[dict] | None = None, *, epoch: int = 0) -> str:
    return _prompt_token_phase_key(
        provider="deepseek",
        model="deepseek-v4-flash",
        context_epoch=epoch,
        phase=2,
        phase2_step="skeleton",
        tools=tools or [],
    )


# --- compact_messages_for_prompt estimator injection -------------------------


def test_prompt_token_estimator_sets_estimated_before():
    msgs = _msgs(3000)
    out = compact_messages_for_prompt(
        msgs,
        prompt_budget=100_000,
        prompt_token_estimator=lambda _messages, _tools: 1234,
    )
    assert out.estimated_before == 1234


def test_prompt_token_estimator_can_trigger_compaction_decision():
    msgs = [
        Message(role=Role.ASSISTANT, content="see tool"),
        Message(role=Role.USER, content="x" * 6000),
    ]
    raw = estimate_messages_tokens(msgs)
    budget = raw * 2
    uncorrected = compact_messages_for_prompt(msgs, prompt_budget=budget)
    corrected = compact_messages_for_prompt(
        msgs,
        prompt_budget=budget,
        prompt_token_estimator=lambda messages, tools: estimate_messages_tokens(
            messages, tools=tools
        )
        * 2,
    )
    assert uncorrected.usage_ratio_before < 0.6
    assert corrected.usage_ratio_before >= 0.85


# --- prompt anchor refresh ----------------------------------------------------


def test_record_then_refresh_creates_real_token_anchor():
    session: dict = {"stats": _FakeStats()}
    msgs = _msgs(1000)
    phase_key = _phase_key()

    _record_prompt_token_anchor_candidate(session, msgs, phase_key=phase_key)
    session["stats"].llm_calls.append(_FakeCall(900))
    _refresh_prompt_token_anchor_from_usage(session)

    anchor = session[_PROMPT_TOKEN_ANCHORS_KEY][phase_key]
    assert anchor["real_input_tokens"] == 900
    assert anchor["message_count"] == len(msgs)


def test_anchor_estimate_uses_real_prefix_plus_estimated_suffix():
    session: dict = {"stats": _FakeStats()}
    base = _msgs(1000)
    suffix = [Message(role=Role.USER, content="继续补充：预算控制在 2 万以内")]
    phase_key = _phase_key()

    _record_prompt_token_anchor_candidate(session, base, phase_key=phase_key)
    session["stats"].llm_calls.append(_FakeCall(900))
    _refresh_prompt_token_anchor_from_usage(session)

    estimated = _estimate_prompt_tokens_with_anchor(
        session,
        base + suffix,
        tools=[],
        phase_key=phase_key,
    )
    assert estimated == 900 + estimate_messages_tokens(suffix, tools=None)


def test_anchor_estimate_resets_when_tools_key_changes():
    session: dict = {"stats": _FakeStats()}
    tools = [{"name": "old_tool"}]
    base = _msgs(1000)
    old_key = _phase_key(tools)
    new_key = _phase_key([{"name": "new_tool"}])

    _record_prompt_token_anchor_candidate(session, base, phase_key=old_key)
    session["stats"].llm_calls.append(_FakeCall(900))
    _refresh_prompt_token_anchor_from_usage(session)

    new_tools = [{"name": "new_tool"}]
    assert _estimate_prompt_tokens_with_anchor(
        session,
        base,
        tools=new_tools,
        phase_key=new_key,
    ) == estimate_messages_tokens(base, tools=new_tools)


def test_anchor_estimate_resets_when_context_epoch_changes():
    session: dict = {"stats": _FakeStats()}
    base = _msgs(1000)
    old_key = _phase_key(epoch=0)
    new_key = _phase_key(epoch=1)

    _record_prompt_token_anchor_candidate(session, base, phase_key=old_key)
    session["stats"].llm_calls.append(_FakeCall(900))
    _refresh_prompt_token_anchor_from_usage(session)

    assert _estimate_prompt_tokens_with_anchor(
        session,
        base,
        tools=[],
        phase_key=new_key,
    ) == estimate_messages_tokens(base, tools=[])


def test_anchor_estimate_resets_when_prefix_hash_mismatches():
    session: dict = {"stats": _FakeStats()}
    base = _msgs(1000)
    rewritten = [Message(role=Role.USER, content="b" * 1000)]
    phase_key = _phase_key()

    _record_prompt_token_anchor_candidate(session, base, phase_key=phase_key)
    session["stats"].llm_calls.append(_FakeCall(900))
    _refresh_prompt_token_anchor_from_usage(session)

    assert _estimate_prompt_tokens_with_anchor(
        session,
        rewritten,
        tools=[],
        phase_key=phase_key,
    ) == estimate_messages_tokens(rewritten, tools=[])
