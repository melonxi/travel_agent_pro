from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from agent.types import ToolCall


_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(previous|all|above)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(previous|all|your)\s+(instructions|rules)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+a", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
)

_DATE_FIELDS = {"date", "departure_date", "check_in", "check_out", "start_date"}
_LOCATION_FIELDS = {"origin", "destination", "query", "city", "location", "place"}
_SEARCH_OUTPUT_TOOLS = {"search_flights", "search_accommodations", "search_trains"}


@dataclass
class GuardrailResult:
    allowed: bool = True
    reason: str = ""
    level: str = "error"


class ToolGuardrail:
    def __init__(self, today: date | None = None):
        self._today = today or date.today()

    def validate_input(self, tc: ToolCall) -> GuardrailResult:
        values = self._iter_string_values(tc.arguments)
        for value in values:
            if any(pattern.search(value) for pattern in _INJECTION_PATTERNS):
                return GuardrailResult(allowed=False, reason="检测到提示注入风险", level="error")

        for field in _DATE_FIELDS:
            raw = tc.arguments.get(field)
            if isinstance(raw, str) and raw.strip():
                try:
                    parsed = date.fromisoformat(raw)
                except ValueError:
                    continue
                if parsed < self._today:
                    return GuardrailResult(allowed=False, reason=f"{field} 不能是过去日期", level="error")

        for field in _LOCATION_FIELDS:
            raw = tc.arguments.get(field)
            if isinstance(raw, str) and not raw.strip():
                return GuardrailResult(allowed=False, reason=f"{field} 不能为空", level="error")

        if tc.name == "update_plan_state" and tc.arguments.get("field") == "budget":
            value = tc.arguments.get("value")
            if isinstance(value, dict):
                total = value.get("total")
                if isinstance(total, (int, float)) and total <= 0:
                    return GuardrailResult(allowed=False, reason="budget.total 不能为负数或零", level="error")

        return GuardrailResult()

    def validate_output(self, tool_name: str, data: Any) -> GuardrailResult:
        if not isinstance(data, dict):
            return GuardrailResult()

        if tool_name in _SEARCH_OUTPUT_TOOLS:
            results = data.get("results")
            if isinstance(results, list) and not results:
                return GuardrailResult(allowed=True, reason="未找到结果", level="warn")

            if isinstance(results, list):
                for item in results:
                    if isinstance(item, dict):
                        price = item.get("price")
                        if isinstance(price, (int, float)) and price > 100_000:
                            return GuardrailResult(allowed=True, reason="结果中存在异常高价", level="warn")

        return GuardrailResult()

    def _iter_string_values(self, obj: Any) -> list[str]:
        values: list[str] = []
        if isinstance(obj, str):
            values.append(obj)
        elif isinstance(obj, dict):
            for value in obj.values():
                values.extend(self._iter_string_values(value))
        elif isinstance(obj, list):
            for value in obj:
                values.extend(self._iter_string_values(value))
        return values
