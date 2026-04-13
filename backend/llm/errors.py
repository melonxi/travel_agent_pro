# backend/llm/errors.py
from __future__ import annotations

from enum import Enum


class LLMErrorCode(str, Enum):
    TRANSIENT = "LLM_TRANSIENT_ERROR"
    RATE_LIMITED = "LLM_RATE_LIMITED"
    BAD_REQUEST = "LLM_BAD_REQUEST"
    STREAM_INTERRUPTED = "LLM_STREAM_INTERRUPTED"
    PROTOCOL_ERROR = "LLM_PROTOCOL_ERROR"


class LLMError(Exception):
    def __init__(
        self,
        code: LLMErrorCode,
        message: str,
        *,
        retryable: bool,
        provider: str,
        model: str,
        failure_phase: str | None = None,
        partial_output: bool = False,
        http_status: int | None = None,
        retry_after: float | None = None,
        raw_error: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.provider = provider
        self.model = model
        self.failure_phase = failure_phase
        self.partial_output = partial_output
        self.http_status = http_status
        self.retry_after = retry_after
        self.raw_error = raw_error


def classify_by_http_status(
    status_code: int,
    *,
    provider: str,
    model: str,
    raw_error: str = "",
) -> LLMError:
    if status_code == 429:
        return LLMError(
            code=LLMErrorCode.RATE_LIMITED,
            message="Rate limited by LLM provider",
            retryable=True,
            provider=provider,
            model=model,
            failure_phase="connection",
            http_status=status_code,
            raw_error=raw_error,
        )
    if 500 <= status_code < 600:
        return LLMError(
            code=LLMErrorCode.TRANSIENT,
            message="LLM provider returned server error",
            retryable=True,
            provider=provider,
            model=model,
            failure_phase="connection",
            http_status=status_code,
            raw_error=raw_error,
        )
    if status_code in (400, 422):
        return LLMError(
            code=LLMErrorCode.BAD_REQUEST,
            message="LLM provider rejected request",
            retryable=False,
            provider=provider,
            model=model,
            failure_phase="connection",
            http_status=status_code,
            raw_error=raw_error,
        )
    return LLMError(
        code=LLMErrorCode.PROTOCOL_ERROR,
        message=f"Unexpected HTTP {status_code} from LLM provider",
        retryable=False,
        provider=provider,
        model=model,
        failure_phase="connection",
        http_status=status_code,
        raw_error=raw_error,
    )
