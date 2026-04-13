# backend/tests/test_llm_errors.py
import pytest
from llm.errors import LLMError, LLMErrorCode, classify_by_http_status


def test_llm_error_code_values():
    assert LLMErrorCode.TRANSIENT.value == "LLM_TRANSIENT_ERROR"
    assert LLMErrorCode.RATE_LIMITED.value == "LLM_RATE_LIMITED"
    assert LLMErrorCode.BAD_REQUEST.value == "LLM_BAD_REQUEST"
    assert LLMErrorCode.STREAM_INTERRUPTED.value == "LLM_STREAM_INTERRUPTED"
    assert LLMErrorCode.PROTOCOL_ERROR.value == "LLM_PROTOCOL_ERROR"


def test_llm_error_attributes():
    err = LLMError(
        code=LLMErrorCode.TRANSIENT,
        message="service unavailable",
        retryable=True,
        provider="openai",
        model="gpt-4o",
        failure_phase="connection",
        http_status=503,
        raw_error="503 Service Unavailable",
    )
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True
    assert err.provider == "openai"
    assert err.model == "gpt-4o"
    assert err.failure_phase == "connection"
    assert err.http_status == 503
    assert err.partial_output is False
    assert str(err) == "service unavailable"


def test_llm_error_defaults():
    err = LLMError(
        code=LLMErrorCode.PROTOCOL_ERROR,
        message="unknown",
        retryable=False,
        provider="anthropic",
        model="claude-sonnet-4-20250514",
    )
    assert err.failure_phase is None
    assert err.partial_output is False
    assert err.http_status is None
    assert err.retry_after is None
    assert err.raw_error == ""


def test_classify_by_http_status_429():
    err = classify_by_http_status(
        429, provider="openai", model="gpt-4o", raw_error="rate limited"
    )
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True


def test_classify_by_http_status_503():
    err = classify_by_http_status(
        503, provider="openai", model="gpt-4o", raw_error="unavailable"
    )
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_classify_by_http_status_500():
    err = classify_by_http_status(
        500, provider="openai", model="gpt-4o", raw_error="internal error"
    )
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_classify_by_http_status_400():
    err = classify_by_http_status(
        400, provider="openai", model="gpt-4o", raw_error="bad request"
    )
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


def test_classify_by_http_status_422():
    err = classify_by_http_status(
        422, provider="openai", model="gpt-4o", raw_error="unprocessable"
    )
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


def test_classify_by_http_status_unknown():
    err = classify_by_http_status(
        418, provider="openai", model="gpt-4o", raw_error="teapot"
    )
    assert err.code == LLMErrorCode.PROTOCOL_ERROR
    assert err.retryable is False
