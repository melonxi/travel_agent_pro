from __future__ import annotations

import copy
import re
from typing import Any

from memory.models import MemoryCandidate, MemoryItem, MemorySource, generate_memory_id
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

    def classify(self, candidate: MemoryCandidate) -> str:
        if candidate.domain in _DENIED_DOMAINS:
            return "drop"
        if self._candidate_contains_forbidden_pii(candidate):
            return "drop"
        if candidate.risk == "high":
            return "pending"
        if candidate.risk == "low":
            if candidate.confidence >= 0.7 and self.auto_save_low_risk:
                return "auto_save"
            return "pending"
        if candidate.risk == "medium":
            if candidate.confidence >= 0.8 and self.auto_save_medium_risk:
                return "auto_save"
            return "pending"
        return "pending"

    def to_item(
        self,
        candidate: MemoryCandidate,
        user_id: str,
        session_id: str,
        now: str,
        trip_id: str | None = None,
    ) -> MemoryItem:
        status = self.classify(candidate)
        if status == "drop":
            raise ValueError("drop candidates cannot be converted to MemoryItem")
        persistent_status = "active" if status == "auto_save" else status
        item_trip_id = trip_id if candidate.scope == "trip" else None
        safe_value = self._redact_for_storage(candidate.value)
        safe_attributes = self._redact_for_storage(candidate.attributes)
        safe_evidence = self._redact_text(candidate.evidence)[:120]
        return MemoryItem(
            id=generate_memory_id(
                user_id=user_id,
                type=candidate.type,
                domain=candidate.domain,
                key=candidate.key,
                scope=candidate.scope,
                trip_id=item_trip_id,
                value=safe_value,
            ),
            user_id=user_id,
            type=candidate.type,
            domain=candidate.domain,
            key=candidate.key,
            value=safe_value,
            scope=candidate.scope,
            polarity=candidate.polarity,
            confidence=candidate.confidence,
            status=persistent_status,
            source=MemorySource(
                kind="message",
                session_id=session_id,
                quote=safe_evidence,
            ),
            created_at=now,
            updated_at=now,
            trip_id=item_trip_id,
            attributes=safe_attributes,
        )

    def _candidate_contains_forbidden_pii(self, candidate: MemoryCandidate) -> bool:
        return any(
            self._contains_forbidden_pii(value)
            for value in (
                candidate.domain,
                candidate.key,
                candidate.value,
                candidate.evidence,
                candidate.reason,
                candidate.attributes,
            )
        )

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


class MemoryMerger:
    def merge(
        self,
        existing_items: list[MemoryItem],
        incoming: MemoryItem,
    ) -> list[MemoryItem]:
        merged = [copy.deepcopy(item) for item in existing_items]
        matches = [index for index, item in enumerate(merged) if item.id == incoming.id]

        if not matches:
            merged.append(copy.deepcopy(incoming))
            return merged

        first_index = matches[0]
        current = merged[first_index]
        same_id_items = [merged[index] for index in matches]

        if self._same_value(current.value, incoming.value):
            current.confidence = max(
                *(item.confidence for item in same_id_items),
                incoming.confidence,
            )
            current.updated_at = incoming.updated_at
            return self._keep_primary_and_drop_duplicates(merged, matches)

        if isinstance(current.value, list) and isinstance(incoming.value, list):
            current.value = self._union_lists(
                [value for item in same_id_items for value in item.value],
                incoming.value,
            )
            current.confidence = max(
                *(item.confidence for item in same_id_items),
                incoming.confidence,
            )
            current.updated_at = incoming.updated_at
            return self._keep_primary_and_drop_duplicates(merged, matches)

        for index in matches:
            merged[index].status = "obsolete"

        conflict = copy.deepcopy(incoming)
        conflict.status = "pending_conflict"
        merged.append(conflict)
        return merged

    def _same_value(self, left: Any, right: Any) -> bool:
        return left == right

    def _union_lists(self, left: list[Any], right: list[Any]) -> list[Any]:
        merged: list[Any] = []
        for value in list(left) + list(right):
            if value not in merged:
                merged.append(copy.deepcopy(value))
        return merged

    def _keep_primary_and_drop_duplicates(
        self, merged: list[MemoryItem], matches: list[int]
    ) -> list[MemoryItem]:
        primary_index = matches[0]
        return [
            item
            for index, item in enumerate(merged)
            if index == primary_index or index not in matches
        ]
