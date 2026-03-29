# backend/tools/generate_summary.py
from __future__ import annotations

from tools.base import tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "plan_data": {
            "type": "object",
            "description": "完整的旅行计划数据，包含天数、目的地、预算等信息",
        },
    },
    "required": ["plan_data"],
}


def make_generate_summary_tool():
    @tool(
        name="generate_summary",
        description="""生成旅行计划摘要。
Use when: 用户在阶段 7，需要生成最终旅行计划总结。
Don't use when: 计划尚未完成。
返回格式化的行程摘要，含天数、预算等关键信息。""",
        phases=[7],
        parameters=_PARAMETERS,
    )
    async def generate_trip_summary(plan_data: dict) -> dict:
        destination = plan_data.get("destination", "未知目的地")
        days = plan_data.get("days", [])
        total_days = len(days) if days else plan_data.get("total_days", 0)

        # Calculate total budget
        budget = plan_data.get("budget", {})
        flight_cost = budget.get("flights", 0)
        hotel_cost = budget.get("hotels", 0)
        activities_cost = budget.get("activities", 0)
        food_cost = budget.get("food", 0)
        total_budget = flight_cost + hotel_cost + activities_cost + food_cost

        # Build day summaries
        day_summaries = []
        for i, day in enumerate(days, 1):
            activities = day.get("activities", [])
            activity_names = [a.get("name", "未知活动") for a in activities]
            day_summaries.append(
                f"第{i}天: {', '.join(activity_names) if activity_names else '自由活动'}"
            )

        summary_lines = [
            f"🗺️ 目的地: {destination}",
            f"📅 行程天数: {total_days}天",
            f"💰 预计总预算: ¥{total_budget}",
        ]
        if day_summaries:
            summary_lines.append("")
            summary_lines.extend(day_summaries)

        return {
            "summary": "\n".join(summary_lines),
            "total_days": total_days,
            "total_budget": total_budget,
        }

    return generate_trip_summary
