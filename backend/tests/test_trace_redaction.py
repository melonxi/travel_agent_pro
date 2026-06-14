from storage.trace_redaction import (
    BEARER_MARKER,
    COOKIE_MARKER,
    CREDENTIAL_MARKER,
    PII_MARKER,
    REDACTION_STATUS_NOT_NEEDED,
    REDACTION_STATUS_REDACTED,
    SECRET_MARKER,
    redact_for_trace,
    stable_content_hash,
)


def test_trace_redaction_removes_tokens_and_private_url_credentials():
    payload = {
        "api_key": "sk-live-secret",
        "authorization": "Bearer abc.def.ghi",
        "raw_header": "Cookie: sessionid=abc123; path=/",
        "xhs_url": "https://www.xiaohongshu.com/explore/abc?xsec_token=secret-token&foo=1",
        "private_url": "https://user:pass@example.com/internal",
        "oauth_code": "oauth-code-secret",
    }

    result = redact_for_trace(payload)
    redacted = result.value

    assert result.redaction_status == REDACTION_STATUS_REDACTED
    assert result.redacted_count >= 6
    assert redacted["api_key"] == SECRET_MARKER
    assert redacted["authorization"] == SECRET_MARKER
    assert COOKIE_MARKER in redacted["raw_header"]
    assert "secret-token" not in redacted["xhs_url"]
    assert SECRET_MARKER in redacted["xhs_url"]
    assert "user:pass" not in redacted["private_url"]
    assert CREDENTIAL_MARKER in redacted["private_url"]
    assert redacted["oauth_code"] == SECRET_MARKER


def test_trace_redaction_recurses_nested_dicts_and_lists_with_configurable_pii():
    payload = {
        "items": [
            {
                "name": "traveler",
                "email": "person@example.com",
                "notes": "contact +86 138 0000 0000 before departure",
            },
            {
                "headers": {
                    "Authorization": "Bearer nested-token",
                    "X-Trace": "safe",
                }
            },
        ],
        "safe": "keep me",
    }

    without_pii = redact_for_trace(payload)
    with_pii = redact_for_trace(payload, redact_pii=True)

    assert without_pii.value["items"][0]["email"] == "person@example.com"
    assert with_pii.value["items"][0]["email"] == PII_MARKER
    assert PII_MARKER in with_pii.value["items"][0]["notes"]
    assert with_pii.value["items"][1]["headers"]["Authorization"] == SECRET_MARKER
    assert with_pii.value["items"][1]["headers"]["X-Trace"] == "safe"
    assert with_pii.value["safe"] == "keep me"


def test_trace_redaction_reports_not_needed_for_safe_payload():
    result = redact_for_trace({"query": "tokyo ramen", "limit": 3})

    assert result.redaction_status == REDACTION_STATUS_NOT_NEEDED
    assert result.redacted_count == 0


def test_trace_artifact_hash_is_stable_for_dict_order_and_redaction():
    first = {"b": 2, "a": {"token": "secret", "x": [3, 1]}}
    second = {"a": {"x": [3, 1], "token": "secret"}, "b": 2}

    first_redacted = redact_for_trace(first).value
    second_redacted = redact_for_trace(second).value

    assert first_redacted == second_redacted
    assert stable_content_hash(first_redacted) == stable_content_hash(second_redacted)
    assert stable_content_hash({"a": 1}) != stable_content_hash({"a": 2})


def test_trace_redaction_scrubs_secret_assignments_inside_text():
    text = "api_key=sk-secret access_token=abc Bearer xyz"

    result = redact_for_trace(text)

    assert "sk-secret" not in result.value
    assert "abc" not in result.value
    assert BEARER_MARKER in result.value
    assert result.redaction_status == REDACTION_STATUS_REDACTED
