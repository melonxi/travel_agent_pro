"""Rule-based feasibility checker — catches obviously impossible trip plans
before expensive LLM planning rounds.
"""
from __future__ import annotations
from dataclasses import dataclass, field

# Minimum realistic daily cost in CNY for popular destinations
_MIN_DAILY_COST: dict[str, int] = {
    "东京": 800, "大阪": 700, "京都": 700, "巴黎": 1200,
    "伦敦": 1300, "纽约": 1500, "洛杉矶": 1200, "旧金山": 1300,
    "悉尼": 1100, "墨尔本": 1000, "曼谷": 400, "清迈": 300,
    "新加坡": 900, "吉隆坡": 400, "首尔": 600, "釜山": 500,
    "台北": 500, "香港": 800, "澳门": 700, "迪拜": 1000,
    "罗马": 900, "米兰": 900, "巴塞罗那": 800, "马德里": 700,
    "柏林": 700, "慕尼黑": 800, "阿姆斯特丹": 900, "苏黎世": 1500,
    "温哥华": 1000, "多伦多": 900,
}
_DEFAULT_MIN_DAILY = 500  # fallback for unknown destinations

_MIN_DAYS: dict[str, int] = {
    "东京": 3, "巴黎": 3, "纽约": 3, "伦敦": 3,
    "悉尼": 4, "迪拜": 2,
}
_DEFAULT_MIN_DAYS = 2


@dataclass
class FeasibilityResult:
    feasible: bool = True
    reasons: list[str] = field(default_factory=list)


def check_feasibility(
    destination: str | None,
    budget_total: int | None,
    days: int | None,
) -> FeasibilityResult:
    """Return a FeasibilityResult with any infeasibility reasons."""
    result = FeasibilityResult()
    if not destination:
        return result  # can't check without destination

    min_daily = _MIN_DAILY_COST.get(destination, _DEFAULT_MIN_DAILY)
    min_days = _MIN_DAYS.get(destination, _DEFAULT_MIN_DAYS)

    if days is not None and days < min_days:
        result.feasible = False
        result.reasons.append(
            f"{destination}建议至少{min_days}天，当前仅{days}天"
        )

    if budget_total is not None and days is not None and days > 0:
        daily_budget = budget_total / days
        if daily_budget < min_daily * 0.5:
            result.feasible = False
            result.reasons.append(
                f"{destination}每日最低消费约{min_daily}元，"
                f"当前日均预算仅{daily_budget:.0f}元，严重不足"
            )
    elif budget_total is not None and budget_total < min_daily * min_days * 0.5:
        result.feasible = False
        result.reasons.append(
            f"{destination}最低预算约{min_daily * min_days}元，"
            f"当前总预算{budget_total}元不足"
        )

    return result
