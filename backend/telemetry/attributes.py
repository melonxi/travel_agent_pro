AGENT_SESSION_ID = "agent.session_id"
AGENT_PHASE = "agent.phase"
AGENT_ITERATION = "agent.iteration"
TOOL_NAME = "tool.name"
TOOL_STATUS = "tool.status"
TOOL_ERROR_CODE = "tool.error_code"
LLM_PROVIDER = "llm.provider"
LLM_MODEL = "llm.model"
LLM_TOKENS_IN = "llm.tokens.input"
LLM_TOKENS_OUT = "llm.tokens.output"
PHASE_FROM = "phase.from"
PHASE_TO = "phase.to"
CONTEXT_TOKENS_BEFORE = "context.tokens.before"
CONTEXT_TOKENS_AFTER = "context.tokens.after"


# --- Phase B: Span Event Names ---

EVENT_TOOL_INPUT = "tool.input"
EVENT_TOOL_OUTPUT = "tool.output"
EVENT_LLM_REQUEST = "llm.request"
EVENT_LLM_RESPONSE = "llm.response"
EVENT_PHASE_PLAN_SNAPSHOT = "phase.plan_snapshot"
EVENT_CONTEXT_COMPRESSION = "context.compression"


def truncate(value: str, max_len: int = 512) -> str:
    if len(value) <= max_len:
        return value
    return value[:max_len] + "...(truncated)"
