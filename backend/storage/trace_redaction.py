from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTION_STATUS_NOT_NEEDED = "not_needed"
REDACTION_STATUS_REDACTED = "redacted"
REDACTION_STATUS_HASH_ONLY = "hash_only"
REDACTION_STATUS_DISABLED = "disabled"

SECRET_MARKER = "[REDACTED:secret]"
BEARER_MARKER = "[REDACTED:bearer_token]"
COOKIE_MARKER = "[REDACTED:cookie]"
CREDENTIAL_MARKER = "[REDACTED:credentials]"
PII_MARKER = "[REDACTED:pii]"

_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "auth_token",
    "xsec_token",
    "bearer",
    "authorization",
    "cookie",
    "set_cookie",
    "client_secret",
    "secret",
    "password",
    "passwd",
    "oauth_code",
)

_TOKEN_QUERY_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "auth_token",
    "xsec_token",
    "oauth_token",
    "oauth_code",
    "code",
    "token",
    "key",
    "secret",
    "signature",
    "sig",
}

_PII_KEY_FRAGMENTS = (
    "email",
    "phone",
    "passport",
    "id_number",
    "identity_number",
    "address",
)

_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_COOKIE_HEADER_RE = re.compile(r"(?i)\b(cookie|set-cookie)\s*:\s*[^;\n\r]+(?:;[^\n\r]*)?")
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|xsec[_-]?token|"
    r"oauth[_-]?code|client[_-]?secret|secret|password)\s*[:=]\s*"
    r"([^\s,;&]+)"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d -]{8,}\d)(?!\d)")
_PASSPORT_OR_ID_RE = re.compile(r"\b[A-Z]\d{8}\b|\b\d{15,18}[0-9Xx]\b")


@dataclass(frozen=True)
class RedactionResult:
    value: Any
    redaction_status: str
    redacted_count: int = 0


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def stable_content_hash(value: Any) -> str:
    data = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def redact_for_trace(value: Any, *, redact_pii: bool = False) -> RedactionResult:
    redacted, count = _redact_value(value, redact_pii=redact_pii, key_hint=None)
    return RedactionResult(
        value=redacted,
        redaction_status=(
            REDACTION_STATUS_REDACTED if count else REDACTION_STATUS_NOT_NEEDED
        ),
        redacted_count=count,
    )


def _normalize_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def _is_secret_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    return any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS)


def _is_pii_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    return any(fragment in normalized for fragment in _PII_KEY_FRAGMENTS)


def _redact_value(
    value: Any,
    *,
    redact_pii: bool,
    key_hint: Any,
) -> tuple[Any, int]:
    if key_hint is not None and _is_secret_key(key_hint):
        return SECRET_MARKER, 1
    if redact_pii and key_hint is not None and _is_pii_key(key_hint):
        return PII_MARKER, 1

    if isinstance(value, dict):
        total = 0
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            new_item, count = _redact_value(
                item,
                redact_pii=redact_pii,
                key_hint=key,
            )
            redacted[key] = new_item
            total += count
        return redacted, total

    if isinstance(value, list):
        total = 0
        items = []
        for item in value:
            new_item, count = _redact_value(
                item,
                redact_pii=redact_pii,
                key_hint=None,
            )
            items.append(new_item)
            total += count
        return items, total

    if isinstance(value, tuple):
        new_list, count = _redact_value(
            list(value),
            redact_pii=redact_pii,
            key_hint=None,
        )
        return tuple(new_list), count

    if isinstance(value, str):
        return _redact_string(value, redact_pii=redact_pii)

    return value, 0


def _redact_string(value: str, *, redact_pii: bool) -> tuple[str, int]:
    text = value
    count = 0

    text, url_count = _scrub_url_tokens(text)
    count += url_count

    text, bearer_count = _BEARER_RE.subn("Bearer " + BEARER_MARKER, text)
    count += bearer_count

    text, cookie_count = _COOKIE_HEADER_RE.subn(lambda m: f"{m.group(1)}: {COOKIE_MARKER}", text)
    count += cookie_count

    def _secret_assignment(match: re.Match[str]) -> str:
        return f"{match.group(1)}={SECRET_MARKER}"

    text, assignment_count = _ASSIGNMENT_SECRET_RE.subn(_secret_assignment, text)
    count += assignment_count

    if redact_pii:
        for regex in (_EMAIL_RE, _PHONE_RE, _PASSPORT_OR_ID_RE):
            text, pii_count = regex.subn(PII_MARKER, text)
            count += pii_count

    return text, count


def _scrub_url_tokens(text: str) -> tuple[str, int]:
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return text, 0

    count = 0
    netloc = parts.netloc
    if "@" in netloc:
        host = netloc.rsplit("@", 1)[1]
        netloc = f"{CREDENTIAL_MARKER}@{host}"
        count += 1

    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    scrubbed_pairs: list[tuple[str, str]] = []
    for key, value in query_pairs:
        if key.lower() in _TOKEN_QUERY_KEYS:
            scrubbed_pairs.append((key, SECRET_MARKER))
            count += 1
        else:
            scrubbed_pairs.append((key, value))

    if count == 0:
        return text, 0

    return (
        urlunsplit(
            (
                parts.scheme,
                netloc,
                parts.path,
                urlencode(scrubbed_pairs, doseq=True),
                parts.fragment,
            )
        ),
        count,
    )
