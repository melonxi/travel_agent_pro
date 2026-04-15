# backend/tests/test_anthropic_provider_classify.py
from unittest.mock import MagicMock

import pytest

from llm.errors import LLMError, LLMErrorCode


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider(model="claude-sonnet-4-20250514")


def test_classify_error_unknown_exception(provider):
    exc = RuntimeError("something weird")
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is False


def test_classify_error_opaque_api_error_busy(provider):
    import anthropic

    exc = anthropic.APIError(
        message="service is busy, please try again later",
        request=MagicMock(),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_api_status_error_unchanged(provider):
    """强类型分支行为回归。"""
    import anthropic

    exc = anthropic.APIStatusError(
        message="overloaded",
        response=MagicMock(status_code=529),
        body=None,
    )
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_connection_error(provider):
    import anthropic

    exc = anthropic.APIConnectionError(request=MagicMock())
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.TRANSIENT
    assert result.retryable is True


def test_classify_error_json_decode_unchanged(provider):
    import json

    exc = json.JSONDecodeError("bad json", "", 0)
    result = provider._classify_error(exc)
    assert result.code == LLMErrorCode.PROTOCOL_ERROR
    assert result.failure_phase == "parsing"


def test_provider_name(provider):
    assert provider.provider_name == "anthropic"
