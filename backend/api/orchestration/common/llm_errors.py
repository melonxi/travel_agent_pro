from __future__ import annotations

from llm.errors import LLMError, LLMErrorCode

LLM_ERROR_MESSAGES: dict[LLMErrorCode, str] = {
    LLMErrorCode.TRANSIENT: "模型服务暂时繁忙，本轮回复已中断。请稍后重试。",
    LLMErrorCode.RATE_LIMITED: "请求过于频繁，请稍后再试。",
    LLMErrorCode.BAD_REQUEST: "请求参数异常，请缩短对话长度后重试。",
    LLMErrorCode.STREAM_INTERRUPTED: "模型回复过程中连接中断。请重试。",
    LLMErrorCode.PROTOCOL_ERROR: "模型返回格式异常，请重试或切换模型。",
}


def user_friendly_message(exc: LLMError) -> str:
    return LLM_ERROR_MESSAGES.get(exc.code, "系统内部错误，请稍后重试。")
