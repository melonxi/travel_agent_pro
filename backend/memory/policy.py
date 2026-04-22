from __future__ import annotations

import copy
import re
from typing import Any

from memory.v3_models import MemoryProfileItem, WorkingMemoryItem


_DENIED_DOMAINS = {"payment", "membership"}
_PENDING_DOMAINS = {"health", "family", "documents", "accessibility"}
_PII_SEQUENCE_RE = re.compile(r"\d{9,18}")
_PII_SEPARATED_DIGITS_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{7,}\d)(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PII_PHRASES = ("护照号", "身份证", "passport number", "id number")


class MemoryPolicy:
    def __init__(
        self,
        *,
        auto_save_low_risk: bool = True,
        auto_save_medium_risk: bool = False,
    ) -> None:
        self.auto_save_low_risk = auto_save_low_risk
        self.auto_save_medium_risk = auto_save_medium_risk

    def _contains_forbidden_pii(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return 9 <= len(str(abs(value))) <= 18
        if isinstance(value, float):
            return False
        if isinstance(value, str):
            lowered = value.lower()
            if any(phrase in lowered for phrase in _PII_PHRASES):
                return True
            if _EMAIL_RE.search(value):
                return True
            if _PII_SEQUENCE_RE.search(value):
                return True
            return any(
                9 <= len(re.sub(r"\D", "", match.group(0))) <= 18
                for match in _PII_SEPARATED_DIGITS_RE.finditer(value)
            )
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key) == "number":
                    return True
                if self._contains_forbidden_pii(nested):
                    return True
            return False
        if isinstance(value, (list, tuple, set)):
            return any(self._contains_forbidden_pii(item) for item in value)
        return False

    def _redact_for_storage(self, value: Any) -> Any:
        if value is None or isinstance(value, (bool, float)):
            return copy.deepcopy(value)
        if isinstance(value, int):
            return "[REDACTED]" if 9 <= len(str(abs(value))) <= 18 else value
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, dict):
            redacted: dict[Any, Any] = {}
            for key, nested in value.items():
                if str(key).lower() == "number":
                    redacted[key] = "[REDACTED]"
                else:
                    redacted[key] = self._redact_for_storage(nested)
            return redacted
        if isinstance(value, list):
            return [self._redact_for_storage(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_for_storage(item) for item in value)
        if isinstance(value, set):
            return {self._redact_for_storage(item) for item in value}
        return copy.deepcopy(value)

    def _redact_text(self, value: str) -> str:
        text = str(value)
        for phrase in _PII_PHRASES:
            text = re.sub(re.escape(phrase), "[REDACTED]", text, flags=re.IGNORECASE)
        text = _EMAIL_RE.sub("[REDACTED]", text)
        text = _PII_SEQUENCE_RE.sub("[REDACTED]", text)

        def redact_separated_digits(match: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", match.group(0))
            if 9 <= len(digits) <= 18:
                return "[REDACTED]"
            return match.group(0)

        return _PII_SEPARATED_DIGITS_RE.sub(redact_separated_digits, text)

    def classify_v3_profile_item(self, bucket: str, item: MemoryProfileItem) -> str:
        if item.domain in _DENIED_DOMAINS:
            return "drop"
        if self._profile_item_contains_pii(item):
            return "drop"
        if bucket == "preference_hypotheses":
            return "pending"
        if item.domain in _PENDING_DOMAINS:
            return "pending"
        if bucket in {"constraints", "rejections"}:
            if item.stability == "explicit_declared" and item.confidence >= 0.8:
                return "active"
            return "pending"
        if bucket == "stable_preferences":
            if item.stability in {"explicit_declared", "pattern_observed"} and item.confidence >= 0.8:
                return "active"
            return "pending"
        return "pending"

    def sanitize_v3_profile_item(self, item: MemoryProfileItem) -> MemoryProfileItem:
        return MemoryProfileItem(
            id=item.id,
            domain=item.domain,
            key=item.key,
            value=self._redact_for_storage(item.value),
            polarity=item.polarity,
            stability=item.stability,
            confidence=item.confidence,
            status=item.status,
            context=self._redact_for_storage(item.context),
            applicability=self._redact_text(item.applicability),
            recall_hints=self._redact_for_storage(item.recall_hints),
            source_refs=self._redact_for_storage(item.source_refs),
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    def sanitize_working_memory_item(
        self, item: WorkingMemoryItem
    ) -> WorkingMemoryItem:
        return WorkingMemoryItem(
            id=item.id,
            phase=item.phase,
            kind=item.kind,
            domains=list(item.domains),
            content=self._redact_text(item.content),
            reason=self._redact_text(item.reason),
            status=item.status,
            expires=dict(item.expires),
            created_at=item.created_at,
        )

    def _profile_item_contains_pii(self, item: MemoryProfileItem) -> bool:
        return any(
            self._contains_forbidden_pii(value)
            for value in (
                item.domain,
                item.key,
                item.value,
                item.applicability,
                item.context,
                item.recall_hints,
                item.source_refs,
            )
        )
