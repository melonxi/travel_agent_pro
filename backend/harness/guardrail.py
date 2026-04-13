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

_INJECTION_PATTERNS_ZH = (
    re.compile(r"忽略.{0,4}(之前|以上|所有|前面).{0,4}(指令|规则|提示|要求)"),
    re.compile(r"你现在是"),
    re.compile(r"不要遵守.{0,4}(规则|指令|限制)"),
    re.compile(r"(请|你)?无视.{0,4}(之前|以上|所有).{0,4}(指令|规则)"),
    re.compile(r"(扮演|充当|假装).{0,4}(另一个|其他|别的)"),
    re.compile(r"(输出|显示|告诉我).{0,4}(系统|system).{0,4}(提示|prompt)"),
)

_DATE_FIELDS = {"date", "departure_date", "check_in", "check_out", "start_date"}
_LOCATION_FIELDS = {"origin", "destination", "query", "city", "location", "place"}
_SEARCH_OUTPUT_TOOLS = {"search_flights", "search_accommodations", "search_trains"}

_MAX_INPUT_LENGTH = 5000

_REQUIRED_RESULT_FIELDS: dict[str, list[str]] = {
    "search_flights": ["price", "departure_time", "arrival_time", "airline"],
    "search_accommodations": ["price", "name", "location"],
    "search_trains": ["price", "departure_time", "arrival_time"],
}
_CRITICAL_FIELDS = {"price"}


@dataclass
class GuardrailResult:
    allowed: bool = True
    reason: str = ""
    level: str = "error"


class ToolGuardrail:
    def __init__(
        self,
        today: date | None = None,
        disabled_rules: list[str] | None = None,
    ):
        self._today = today
        self._disabled_rules = set(disabled_rules or [])

    def validate_input(self, tc: ToolCall) -> GuardrailResult:
        if not self._is_disabled("input_length"):
            values = self._iter_string_values(tc.arguments)
            for value in values:
                if len(value) > _MAX_INPUT_LENGTH:
                    return GuardrailResult(
                        allowed=False,
                        reason=f"输入内容过长（{len(value)} 字符，上限 {_MAX_INPUT_LENGTH}）",
                        level="error",
                    )

        if not self._is_disabled("prompt_injection"):
            values = self._iter_string_values(tc.arguments)
            for value in values:
                if any(p.search(value) for p in _INJECTION_PATTERNS):
                    return GuardrailResult(allowed=False, reason="检测到提示注入风险", level="error")
                if any(p.search(value) for p in _INJECTION_PATTERNS_ZH):
                    return GuardrailResult(allowed=False, reason="检测到提示注入风险", level="error")

        if not self._is_disabled("past_date"):
            today = self._today or date.today()
            for field in _DATE_FIELDS:
                raw = tc.arguments.get(field)
                if isinstance(raw, str) and raw.strip():
                    try:
                        parsed = date.fromisoformat(raw)
                    except ValueError:
                        continue
                    if parsed < today:
                        return GuardrailResult(allowed=False, reason=f"{field} 不能是过去日期", level="error")

        if not self._is_disabled("empty_location"):
            for field in _LOCATION_FIELDS:
                raw = tc.arguments.get(field)
                if isinstance(raw, str) and not raw.strip():
                    return GuardrailResult(allowed=False, reason=f"{field} 不能为空", level="error")

        if (
            not self._is_disabled("invalid_budget")
            and tc.name == "update_plan_state"
            and tc.arguments.get("field") == "budget"
        ):
            value = tc.arguments.get("value")
            if isinstance(value, dict):
                total = value.get("total")
                if isinstance(total, (int, float)) and total <= 0:
                    return GuardrailResult(allowed=False, reason="budget.total 不能为负数或零", level="error")

        return GuardrailResult()

    def validate_output(self, tool_name: str, data: Any) -> GuardrailResult:
        if not isinstance(data, dict):
            return GuardrailResult()

        results = data.get("results")
        if (
            not self._is_disabled("empty_results")
            and tool_name in _SEARCH_OUTPUT_TOOLS
            and isinstance(results, list)
            and not results
        ):
            return GuardrailResult(allowed=True, reason="未找到结果", level="warn")

        if not self._is_disabled("price_anomaly") and isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    price = item.get("price")
                    if isinstance(price, (int, float)) and price > 100_000:
                        return GuardrailResult(allowed=True, reason="结果中存在异常高价", level="warn")

        if (
            not self._is_disabled("missing_fields")
            and tool_name in _REQUIRED_RESULT_FIELDS
            and isinstance(results, list)
        ):
            required = _REQUIRED_RESULT_FIELDS[tool_name]
            for item in results:
                if isinstance(item, dict):
                    missing = [f for f in required if f not in item]
                    if missing:
                        level = (
                            "error"
                            if any(field in _CRITICAL_FIELDS for field in missing)
                            else "warn"
                        )
                        return GuardrailResult(
                            allowed=True,
                            reason=f"搜索结果缺少必要字段: {', '.join(missing)}",
                            level=level,
                        )

        return GuardrailResult()

    def _is_disabled(self, rule: str) -> bool:
        return rule in self._disabled_rules

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
