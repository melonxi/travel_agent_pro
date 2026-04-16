from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from state.models import Budget, DateRange, Travelers, TravelPlanState


def parse_dates_value(value: Any, *, today: date | None = None) -> DateRange | None:
    if isinstance(value, dict):
        start = value.get("start") or value.get("start_date")
        end = value.get("end") or value.get("end_date")
        if start and end:
            return DateRange(start=str(start), end=str(end))
        duration = (
            value.get("duration")
            or value.get("duration_days")
            or value.get("travel_days")
            or value.get("total_days")
            or value.get("days")
        )
        time_window = (
            value.get("time_window")
            or value.get("period")
            or value.get("holiday")
            or value.get("window")
        )
        if duration and time_window:
            duration_text = (
                f"{duration}天" if isinstance(duration, int | float) else str(duration)
            )
            return parse_dates_value(f"{time_window}，{duration_text}", today=today)
        return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    # Handle stringified JSON: '{"start":"2026-05-01","end":"2026-05-05"}'
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parse_dates_value(parsed, today=today)
        except (json.JSONDecodeError, TypeError):
            pass

    today = today or date.today()
    iso_dates = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text)
    if len(iso_dates) >= 2:
        start = date.fromisoformat(iso_dates[0].replace("/", "-"))
        end = date.fromisoformat(iso_dates[1].replace("/", "-"))
        return DateRange(start=start.isoformat(), end=end.isoformat())

    # Chinese date format: X月X号/日
    cn_dates = re.findall(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]", text)
    if len(cn_dates) >= 2:
        m1, d1 = int(cn_dates[0][0]), int(cn_dates[0][1])
        m2, d2 = int(cn_dates[1][0]), int(cn_dates[1][1])
        year = today.year
        start = date(year, m1, d1)
        end = date(year, m2, d2)
        if start < today:
            start = date(year + 1, m1, d1)
            end = date(year + 1, m2, d2)
        return DateRange(start=start.isoformat(), end=end.isoformat())
    if len(cn_dates) == 1:
        # Single date + duration: strip the date part to avoid matching "X日" as duration
        m, d = int(cn_dates[0][0]), int(cn_dates[0][1])
        date_end = text.index(cn_dates[0][1]) + len(cn_dates[0][1])
        remainder = text[date_end:]
        duration_match = re.search(r"(\d+)\s*[天日]", remainder)
        if duration_match:
            year = today.year
            start = date(year, m, d)
            if start < today:
                start = date(year + 1, m, d)
            end = start + timedelta(days=int(duration_match.group(1)) - 1)
            return DateRange(start=start.isoformat(), end=end.isoformat())

    duration_match = re.search(r"(\d+)\s*[天日]", text)
    duration_days = int(duration_match.group(1)) if duration_match else None

    holiday_map = {
        "五一": (5, 1),
        "劳动节": (5, 1),
        "国庆": (10, 1),
        "元旦": (1, 1),
    }
    for keyword, (month, day) in holiday_map.items():
        if keyword not in text or duration_days is None:
            continue

        start = date(today.year, month, day)
        if start < today:
            start = date(today.year + 1, month, day)
        end = start + timedelta(days=duration_days)
        return DateRange(start=start.isoformat(), end=end.isoformat())

    return None


def parse_budget_value(value: Any) -> Budget | None:
    if isinstance(value, dict):
        return Budget.from_dict(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Budget(total=float(value))
    if not isinstance(value, str):
        return None

    text = value.strip().lower().replace(",", "")
    if not text:
        return None

    # Handle stringified JSON: '{"total": 10000, "currency": "CNY"}'
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return Budget.from_dict(parsed)
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    amount_match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not amount_match:
        return None

    amount = float(amount_match.group(1))
    if "万" in text:
        amount *= 10000
    elif "千" in text:
        amount *= 1000
    elif re.search(r"\bk\b", text):
        amount *= 1000

    currency = "CNY"
    if any(token in text for token in ("usd", "us$", "$", "美元")):
        currency = "USD"
    elif any(token in text for token in ("eur", "€", "欧元")):
        currency = "EUR"
    elif any(token in text for token in ("jpy", "日元", "yen")):
        currency = "JPY"

    return Budget(total=amount, currency=currency)


def parse_travelers_value(value: Any) -> Travelers | None:
    if isinstance(value, dict):
        return Travelers.from_dict(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return Travelers(adults=value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    # Handle stringified JSON: '{"adults": 2}' or '{"adults":2,"children":1}'
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return Travelers.from_dict(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

    text_lower = text.lower()

    adults_match = re.search(
        r"(\d+)\s*(?:个)?\s*(?:大人|成人|adults?)",
        text_lower,
        re.IGNORECASE,
    )
    children_match = re.search(
        r"(\d+)\s*(?:个)?\s*(?:小孩|儿童|孩子|child|children)",
        text_lower,
        re.IGNORECASE,
    )
    total_match = re.search(r"(\d+)\s*(?:位|人)", text_lower)

    adults = int(adults_match.group(1)) if adults_match else None
    children = int(children_match.group(1)) if children_match else 0

    if adults is None and total_match:
        adults = int(total_match.group(1))

    # Fallback: pure digit string like "2" → treat as adults count
    if adults is None:
        digit_match = re.fullmatch(r"\d+", text)
        if digit_match:
            adults = int(digit_match.group())

    if adults is None:
        return None

    return Travelers(adults=adults, children=children)


def extract_trip_facts(message: str, *, today: date | None = None) -> dict[str, Any]:
    """Deprecated: legacy regex intake path kept only for compatibility/tests."""
    updates: dict[str, Any] = {}

    destination = _extract_destination(message)
    if destination:
        updates["destination"] = destination

    dates = parse_dates_value(message, today=today)
    if dates:
        updates["dates"] = dates

    budget_text = _extract_budget_text(message)
    budget = parse_budget_value(budget_text) if budget_text else None
    if budget:
        updates["budget"] = budget

    travelers = parse_travelers_value(message)
    if travelers:
        updates["travelers"] = travelers

    return updates


def apply_trip_facts(
    plan: TravelPlanState,
    message: str,
    *,
    today: date | None = None,
) -> set[str]:
    """Deprecated: chat entrypoint no longer uses regex pre-extraction."""
    updates = extract_trip_facts(message, today=today)
    for field, value in updates.items():
        setattr(plan, field, value)
    return set(updates)


def _extract_destination(message: str) -> str | None:
    """Legacy helper for deprecated regex intake."""
    invalid_candidates = {
        "这里",
        "这里了",
        "那里",
        "那里了",
        "这边",
        "那边",
        "这儿",
        "那儿",
    }
    patterns = (
        r"(?:改成|改为|改去|改到|换成|换到)([A-Za-z\u4e00-\u9fff·]{2,20})",
        r"(?:去|到|飞往|前往)([A-Za-z\u4e00-\u9fff·]{2,20})",
        r"(?:目的地[是为：:]?)([A-Za-z\u4e00-\u9fff·]{2,20})",
    )
    for idx, pattern in enumerate(patterns):
        match = re.search(pattern, message)
        if not match:
            continue

        candidate = match.group(1)
        candidate = re.sub(
            r"(玩|旅游|旅行|出差|待|住|看|逛|过|度假|呆).*$", "", candidate
        )
        candidate = re.sub(r"(预算|大概|大约|准备|打算).*$", "", candidate)
        candidate = re.sub(r"\d.*$", "", candidate)
        candidate = candidate.strip(" ，。,.;；:：")
        if candidate in invalid_candidates:
            return None
        if any(token in candidate for token in ("或者", "或", "和", "、", "/")):
            return None
        if idx == 1 and _is_negated_destination_message(message, candidate):
            return None
        if len(candidate) >= 2:
            return candidate
    return None


def _is_negated_destination_message(message: str, candidate: str) -> bool:
    candidate_pattern = re.escape(candidate)
    negation_patterns = (
        rf"不想去{candidate_pattern}",
        rf"不去{candidate_pattern}",
        rf"不要去{candidate_pattern}",
        rf"别去{candidate_pattern}",
        rf"不想去{candidate_pattern}了",
    )
    return any(re.search(pattern, message) for pattern in negation_patterns)


def _extract_budget_text(message: str) -> str | None:
    """Legacy helper for deprecated regex intake."""
    patterns = (
        r"(?:预算|人均预算|总预算|费用|花费|开销)[^\d$€¥￥]{0,6}([¥￥$€]?\s*\d+(?:\.\d+)?\s*(?:万|千|k)?\s*(?:元|人民币|美元|usd|eur|日元|jpy)?)",
        r"([¥￥$€]\s*\d+(?:\.\d+)?\s*(?:万|千|k)?(?:元|人民币|美元|usd|eur|日元|jpy)?)",
        r"(\d+(?:\.\d+)?\s*(?:万|千|k)\s*(?:元|人民币|美元|usd|eur|日元|jpy)?)",
    )
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None
