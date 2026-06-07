from __future__ import annotations

import re

from state.models import TravelPlanState
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "plan_data": {
            "type": "object",
            "description": "完整的旅行计划数据，至少包含目的地等基础信息。",
        },
        "title": {
            "type": "string",
            "description": "行程计划标题，如「东京5日旅行计划」。",
        },
        "overview": {
            "type": "string",
            "description": "行程总体概述（2-3句话），包含目的地、出行日期、整体风格等。",
        },
        "daily_sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {
                        "type": "integer",
                        "description": "第几天，从 1 开始。",
                    },
                    "date": {
                        "type": "string",
                        "description": "日期，如 2025-07-10。",
                    },
                    "title": {
                        "type": "string",
                        "description": "当天标题，如「浅草寺与秋叶原」。",
                    },
                    "content": {
                        "type": "string",
                        "description": "当天行程详情（markdown 格式），列出活动、时间、交通等。",
                    },
                },
                "required": ["day", "content"],
            },
            "description": "逐日行程列表，每天一个条目。代码会自动生成「## 第 N 天」章节标题。",
        },
        "checklist_title": {
            "type": "string",
            "description": "出发前清单标题，如「东京出发前清单」。",
        },
        "checklist_categories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "类别名，如「证件与文件」「财务准备」。",
                    },
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "该类别下的清单项，如「护照有效期确认」「日元兑换」。",
                    },
                },
                "required": ["category", "items"],
            },
            "description": "出发前清单分类。代码会自动生成 H1 标题和分类小标题。",
        },
    },
    "required": ["plan_data", "title", "daily_sections", "checklist_title", "checklist_categories"],
}


def _has_estimated_transport(plan: TravelPlanState) -> list[str]:
    markers: list[str] = []
    for day in getattr(plan, "daily_plans", []) or []:
        for activity in getattr(day, "activities", []) or []:
            if getattr(activity, "transport_estimated", False):
                summary = getattr(activity, "summary", "") or getattr(activity, "name", "") or ""
                markers.append(summary)
    return markers


def _inject_estimation_markers(content: str, estimated_items: list[str]) -> str:
    if not estimated_items:
        return content
    estimation_note = "（⚠️ 交通时长为估算，未经路线工具验证）"
    lines = content.split("\n")
    result_lines: list[str] = []
    injected = False
    for line in lines:
        result_lines.append(line)
        if not injected:
            for item in estimated_items:
                fragment = item[:10] if len(item) >= 10 else item
                if fragment and fragment in line:
                    result_lines[-1] = line + estimation_note
                    injected = True
                    break
    if not injected:
        result_lines.append("")
        result_lines.append(f"> {estimation_note}")
    return "\n".join(result_lines)


def _build_travel_plan_markdown(
    *,
    title: str,
    overview: str,
    daily_sections: list[dict],
    estimated_items: list[str],
) -> str:
    sections: list[str] = []

    sections.append(f"# {title}")
    sections.append("")
    if overview:
        sections.append(overview.strip())
        sections.append("")

    sorted_sections = sorted(daily_sections, key=lambda s: s.get("day", 0))
    for section in sorted_sections:
        day = section.get("day", 0)
        date_str = section.get("date", "")
        section_title = section.get("title", "")
        content = (section.get("content") or "").strip()

        heading = f"## 第 {day} 天"
        if date_str:
            heading += f"  {date_str}"
        if section_title:
            heading += f"  {section_title}"
        sections.append(heading)
        sections.append("")

        if content:
            if estimated_items:
                content = _inject_estimation_markers(content, estimated_items)
            sections.append(content)
            if not content.endswith("\n"):
                sections.append("")

    return "\n".join(sections)


def _build_checklist_markdown(
    *,
    checklist_title: str,
    checklist_categories: list[dict],
) -> str:
    sections: list[str] = []

    sections.append(f"# {checklist_title}")
    sections.append("")

    for cat in checklist_categories:
        category = cat.get("category", "")
        items = cat.get("items", [])
        if category:
            sections.append(f"### {category}")
            sections.append("")
        for item in items:
            sections.append(f"- [ ] {item}")
        sections.append("")

    return "\n".join(sections)


def make_generate_summary_tool(plan: TravelPlanState):
    @tool(
        name="generate_summary",
        description="""提交正式交付物。
Use when: 用户在阶段 4，需要冻结最终 travel_plan.md 与 checklist.md。
Don't use when: 逐日行程未完成，或需要回退前序阶段。
        返回规范化后的双 markdown 交付物内容。代码会自动生成 H1 标题、逐日章节标题和清单分类标题，只需提供内容。""",
        phases=[4],
        parameters=_PARAMETERS,
        side_effect="write",
        human_label="提交正式交付物",
    )
    async def generate_trip_summary(
        plan_data: dict,
        title: str,
        daily_sections: list[dict],
        checklist_title: str,
        checklist_categories: list[dict],
        overview: str = "",
        **_kwargs,
    ) -> dict:
        if plan.deliverables:
            raise ToolError(
                "交付物已冻结；如需重生成，请先回退相关阶段后再提交。",
                error_code="DELIVERABLES_FROZEN",
            )

        if not isinstance(plan_data, dict):
            plan_data = {}

        if not title or not title.strip():
            raise ToolError(
                "title 不能为空，请提供行程计划标题，如「东京5日旅行计划」。",
                error_code="INVALID_ARGUMENTS",
            )

        if not daily_sections:
            raise ToolError(
                "daily_sections 不能为空，请至少提供一天的行程内容。",
                error_code="INVALID_ARGUMENTS",
            )

        for i, section in enumerate(daily_sections):
            if not isinstance(section, dict):
                raise ToolError(
                    f"daily_sections[{i}] 必须是对象，包含 day 和 content 字段。",
                    error_code="INVALID_ARGUMENTS",
                )
            if "day" not in section:
                raise ToolError(
                    f"daily_sections[{i}] 缺少 day 字段。",
                    error_code="INVALID_ARGUMENTS",
                )
            content = section.get("content", "")
            if not content or not str(content).strip():
                raise ToolError(
                    f"daily_sections[{i}]（第 {section.get('day', '?')} 天）content 不能为空，请提供当天行程详情。",
                    error_code="INVALID_ARGUMENTS",
                )

        if not checklist_title or not checklist_title.strip():
            raise ToolError(
                "checklist_title 不能为空，请提供出发前清单标题。",
                error_code="INVALID_ARGUMENTS",
            )

        if not checklist_categories:
            raise ToolError(
                "checklist_categories 不能为空，请至少提供一个清单分类。",
                error_code="INVALID_ARGUMENTS",
            )

        total_items = 0
        for j, cat in enumerate(checklist_categories):
            if not isinstance(cat, dict):
                raise ToolError(
                    f"checklist_categories[{j}] 必须是对象，包含 category 和 items 字段。",
                    error_code="INVALID_ARGUMENTS",
                )
            items = cat.get("items", [])
            if not items:
                raise ToolError(
                    f"checklist_categories[{j}]（{cat.get('category', '?')}）items 不能为空。",
                    error_code="INVALID_ARGUMENTS",
                )
            total_items += len(items)

        if total_items < 3:
            raise ToolError(
                f"清单项目总数过少（{total_items} 项），请补充更多出发前准备事项。",
                error_code="INVALID_ARGUMENTS",
            )

        estimated_items = _has_estimated_transport(plan)

        travel_plan_markdown = _build_travel_plan_markdown(
            title=title.strip(),
            overview=overview or "",
            daily_sections=daily_sections,
            estimated_items=estimated_items,
        )
        checklist_markdown = _build_checklist_markdown(
            checklist_title=checklist_title.strip(),
            checklist_categories=checklist_categories,
        )

        destination = str(plan_data.get("destination") or "未知目的地")

        return {
            "summary": f"已生成并冻结 {destination} 的 travel_plan.md 与 checklist.md",
            "travel_plan_markdown": travel_plan_markdown,
            "checklist_markdown": checklist_markdown,
        }

    return generate_trip_summary