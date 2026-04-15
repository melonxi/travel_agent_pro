# backend/tests/test_classify_opaque_api_error.py
import pytest
from llm.errors import LLMErrorCode, classify_opaque_api_error


class _FakeAPIError(Exception):
    """模拟裸 openai.APIError，不依赖 openai SDK。"""

    def __init__(self, message: str, *, body=None, status_code=None):
        super().__init__(message)
        self.body = body
        self.status_code = status_code


# ── 结构化状态码 ──


def test_status_code_attribute_400():
    exc = _FakeAPIError("bad", status_code=400)
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


def test_status_code_attribute_429():
    exc = _FakeAPIError("rate limited", status_code=429)
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True


def test_status_code_attribute_500():
    exc = _FakeAPIError("internal", status_code=500)
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_status_code_in_body_dict():
    exc = _FakeAPIError("error", body={"status_code": 503})
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_status_code_regex_from_text():
    exc = _FakeAPIError(
        "Xunfei request failed with status code: 400, param validation error"
    )
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


def test_status_code_500_regex():
    exc = _FakeAPIError("upstream status code: 502 Bad Gateway")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


# ── Gateway 特定码 ──


def test_xunfei_gateway_code_10012():
    exc = _FakeAPIError(
        "Xunfei request failed with code: 10012, "
        "message: EngineInternalError:The system is busy"
    )
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


# ── 关键词匹配（英文） ──


def test_keyword_system_is_busy():
    exc = _FakeAPIError(
        "EngineInternalError:The system is busy, please try again later."
    )
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_keyword_overloaded():
    exc = _FakeAPIError("model is overloaded, try later")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_keyword_temporarily_unavailable():
    exc = _FakeAPIError("service temporarily unavailable")
    err = classify_opaque_api_error(
        exc, provider="anthropic", model="claude-sonnet-4-20250514"
    )
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_keyword_rate_limit():
    exc = _FakeAPIError("rate limit exceeded, please slow down")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True


def test_keyword_too_many_requests():
    exc = _FakeAPIError("too many requests")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True


def test_keyword_quota_exceeded():
    exc = _FakeAPIError("quota exceeded for this model")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True


def test_keyword_invalid_request():
    exc = _FakeAPIError("invalid request: missing required field 'model'")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


def test_keyword_param_validation():
    exc = _FakeAPIError("param validation error, Value error")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.BAD_REQUEST
    assert err.retryable is False


# ── 关键词匹配（中文） ──


def test_cn_keyword_繁忙():
    exc = _FakeAPIError("服务繁忙，请稍后再试")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_cn_keyword_请稍后():
    exc = _FakeAPIError("请稍后重试")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


# ── 兜底 ──


def test_unknown_falls_back_to_transient_non_retryable():
    exc = _FakeAPIError("something completely unexpected happened")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is False


def test_empty_message_falls_back():
    exc = _FakeAPIError("")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is False


# ── 字段透传 ──


def test_failure_phase_preserved():
    exc = _FakeAPIError("unknown")
    err = classify_opaque_api_error(
        exc, provider="openai", model="gpt-4o", failure_phase="streaming"
    )
    assert err.failure_phase == "streaming"


def test_provider_and_model_set():
    exc = _FakeAPIError("unknown")
    err = classify_opaque_api_error(
        exc, provider="anthropic", model="claude-sonnet-4-20250514"
    )
    assert err.provider == "anthropic"
    assert err.model == "claude-sonnet-4-20250514"


def test_raw_error_populated():
    exc = _FakeAPIError("some error")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.raw_error == repr(exc)


def test_never_returns_protocol_error():
    for msg in ["xyz", "!!!", "", "unknown format", "???", "\x00\x01"]:
        exc = _FakeAPIError(msg)
        err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
        assert err.code != LLMErrorCode.PROTOCOL_ERROR, f"PROTOCOL_ERROR for: {msg!r}"


# ── body 提取 ──


def test_body_bytes_extracted():
    exc = _FakeAPIError("error", body=b"system is busy")
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.TRANSIENT
    assert err.retryable is True


def test_body_dict_message_extracted():
    exc = _FakeAPIError("error", body={"message": "rate limit exceeded"})
    err = classify_opaque_api_error(exc, provider="openai", model="gpt-4o")
    assert err.code == LLMErrorCode.RATE_LIMITED
    assert err.retryable is True


# ── Fixture-driven regression tests ──

import json
from pathlib import Path

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "opaque_api_errors"


def _load_fixtures():
    if not _FIXTURE_DIR.is_dir():
        return []
    items = []
    for p in sorted(_FIXTURE_DIR.glob("*.json")):
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        items.append(pytest.param(data, id=p.stem))
    return items


@pytest.mark.parametrize("fixture", _load_fixtures())
def test_fixture_regression(fixture):
    exc = _FakeAPIError(fixture["error_message"])
    err = classify_opaque_api_error(exc, provider="openai", model="test-model")
    assert err.code.value == fixture["expected_code"], (
        f"Expected {fixture['expected_code']}, got {err.code.value} "
        f"for: {fixture['description']}"
    )
    assert err.retryable is fixture["expected_retryable"], (
        f"Expected retryable={fixture['expected_retryable']}, "
        f"got {err.retryable} for: {fixture['description']}"
    )
