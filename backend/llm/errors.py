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


import re as _re


_STATUS_CODE_RE = _re.compile(r"status code[:\s]+(\d{3})", _re.IGNORECASE)

_TRANSIENT_KEYWORDS = (
    "busy",
    "繁忙",
    "try again",
    "请稍后",
    "temporarily unavailable",
    "engine internal",
    "system is busy",
    "overloaded",
)
_RATE_LIMITED_KEYWORDS = ("rate limit", "too many requests", "quota exceeded")
_BAD_REQUEST_KEYWORDS = ("invalid request", "malformed", "param validation")

_GATEWAY_TRANSIENT_CODES = ("10012",)


def _extract_status_code(exc: Exception) -> int | None:
    sc = getattr(exc, "status_code", None)
    if isinstance(sc, int):
        return sc
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        for key in ("status_code", "statusCode"):
            val = body.get(key)
            if isinstance(val, int):
                return val
    m = _STATUS_CODE_RE.search(str(exc))
    if m:
        return int(m.group(1))
    return None


def _extract_body_text(exc: Exception) -> str:
    body = getattr(exc, "body", None)
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, bytes):
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(body, dict):
        try:
            import json

            return json.dumps(body, ensure_ascii=False)
        except Exception:
            return str(body)
    return str(body)


def classify_opaque_api_error(
    exc: Exception,
    *,
    provider: str,
    model: str,
    failure_phase: str = "connection",
) -> LLMError:
    text = str(exc).lower()
    body_text = _extract_body_text(exc).lower()
    haystack = f"{text} {body_text}"

    status = _extract_status_code(exc)
    if status is not None:
        return classify_by_http_status(
            status,
            provider=provider,
            model=model,
            raw_error=repr(exc),
        )

    for code in _GATEWAY_TRANSIENT_CODES:
        if code in haystack:
            return LLMError(
                code=LLMErrorCode.TRANSIENT,
                message=str(exc),
                retryable=True,
                provider=provider,
                model=model,
                failure_phase=failure_phase,
                raw_error=repr(exc),
            )

    if any(kw in haystack for kw in _TRANSIENT_KEYWORDS):
        return LLMError(
            code=LLMErrorCode.TRANSIENT,
            message=str(exc),
            retryable=True,
            provider=provider,
            model=model,
            failure_phase=failure_phase,
            raw_error=repr(exc),
        )
    if any(kw in haystack for kw in _RATE_LIMITED_KEYWORDS):
        return LLMError(
            code=LLMErrorCode.RATE_LIMITED,
            message=str(exc),
            retryable=True,
            provider=provider,
            model=model,
            failure_phase=failure_phase,
            raw_error=repr(exc),
        )
    if any(kw in haystack for kw in _BAD_REQUEST_KEYWORDS):
        return LLMError(
            code=LLMErrorCode.BAD_REQUEST,
            message=str(exc),
            retryable=False,
            provider=provider,
            model=model,
            failure_phase=failure_phase,
            raw_error=repr(exc),
        )

    return LLMError(
        code=LLMErrorCode.TRANSIENT,
        message=str(exc),
        retryable=False,
        provider=provider,
        model=model,
        failure_phase=failure_phase,
        raw_error=repr(exc),
    )
