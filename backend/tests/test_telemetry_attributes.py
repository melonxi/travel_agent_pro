from telemetry.attributes import (
    AGENT_SESSION_ID,
    AGENT_PHASE,
    AGENT_ITERATION,
    TOOL_NAME,
    TOOL_STATUS,
    TOOL_ERROR_CODE,
    LLM_PROVIDER,
    LLM_MODEL,
    LLM_TOKENS_IN,
    LLM_TOKENS_OUT,
    PHASE_FROM,
    PHASE_TO,
    CONTEXT_TOKENS_BEFORE,
    CONTEXT_TOKENS_AFTER,
)


def test_attributes_are_strings():
    attrs = [
        AGENT_SESSION_ID, AGENT_PHASE, AGENT_ITERATION,
        TOOL_NAME, TOOL_STATUS, TOOL_ERROR_CODE,
        LLM_PROVIDER, LLM_MODEL, LLM_TOKENS_IN, LLM_TOKENS_OUT,
        PHASE_FROM, PHASE_TO,
        CONTEXT_TOKENS_BEFORE, CONTEXT_TOKENS_AFTER,
    ]
    for attr in attrs:
        assert isinstance(attr, str)
        assert "." in attr, f"{attr} should use dotted notation"


def test_attributes_unique():
    from telemetry import attributes
    values = [v for k, v in vars(attributes).items() if not k.startswith("_")]
    assert len(values) == len(set(values)), "Attribute values must be unique"
