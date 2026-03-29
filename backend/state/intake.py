from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from state.models import Budget, DateRange, TravelPlanState


def parse_dates_value(value: Any, *, today: date | None = None) -> DateRange | None:
    if isinstance(value, dict):
        return DateRange.from_dict(value)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    today = today or date.today()
    iso_dates = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text)
    if len(iso_dates) >= 2:
        start = date.fromisoformat(iso_dates[0].replace("/", "-"))
        end = date.fromisoformat(iso_dates[1].replace("/", "-"))
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
    if not isinstance(value, str):
        return None

    text = value.strip().lower().replace(",", "")
    if not text:
        return None

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


def extract_trip_facts(message: str, *, today: date | None = None) -> dict[str, Any]:
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

    return updates


def apply_trip_facts(
    plan: TravelPlanState,
    message: str,
    *,
    today: date | None = None,
) -> set[str]:
    updates = extract_trip_facts(message, today=today)
    for field, value in updates.items():
        setattr(plan, field, value)
    return set(updates)


def _extract_destination(message: str) -> str | None:
    patterns = (
        r"(?:去|到|飞往|前往)([A-Za-z\u4e00-\u9fff·]{2,20})",
        r"(?:目的地[是为：:]?)([A-Za-z\u4e00-\u9fff·]{2,20})",
    )
    for pattern in patterns:
        match = re.search(pattern, message)
        if not match:
            continue

        candidate = match.group(1)
        candidate = re.sub(r"(玩|旅游|旅行|出差|待|住|看|逛|过|度假|呆).*$", "", candidate)
        candidate = re.sub(r"(预算|大概|大约|准备|打算).*$", "", candidate)
        candidate = re.sub(r"\d.*$", "", candidate)
        candidate = candidate.strip(" ，。,.;；:：")
        if any(token in candidate for token in ("或者", "或", "和", "、", "/")):
            return None
        if len(candidate) >= 2:
            return candidate
    return None


def _extract_budget_text(message: str) -> str | None:
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
