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
        "travel_plan_markdown": {
            "type": "string",
            "description": "正式交付的 travel_plan.md 内容，必须包含 H1 和逐日小节。",
        },
        "checklist_markdown": {
            "type": "string",
            "description": "正式交付的 checklist.md 内容，必须包含 H1 和清单项。",
        },
    },
    "required": ["plan_data", "travel_plan_markdown", "checklist_markdown"],
}


def _has_h1(text: str) -> bool:
    return bool(re.search(r"(?m)^#\s+\S+", text))


def _has_day_section(text: str) -> bool:
    return "## 第" in text or "### 第" in text


def _has_list_items(text: str) -> bool:
    return bool(re.search(r"(?m)^- (?:\[[ xX]\] )?.+", text))


def _normalize_markdown(value: str) -> str:
    return value.strip() + "\n"


def make_generate_summary_tool(plan: TravelPlanState):
    @tool(
        name="generate_summary",
        description="""提交正式交付物。
Use when: 用户在阶段 7，需要冻结最终 travel_plan.md 与 checklist.md。
Don't use when: 逐日行程未完成，或需要回退前序阶段。
        返回规范化后的双 markdown 交付物内容。""",
        phases=[7],
        parameters=_PARAMETERS,
        side_effect="write",
        human_label="提交正式交付物",
    )
    async def generate_trip_summary(
        plan_data: dict,
        travel_plan_markdown: str,
        checklist_markdown: str,
    ) -> dict:
        if plan.deliverables:
            raise ToolError(
                "交付物已冻结；如需重生成，请先回退相关阶段后再提交。",
                error_code="DELIVERABLES_FROZEN",
            )

        if not isinstance(plan_data, dict):
            plan_data = {}

        travel_plan_markdown = _normalize_markdown(travel_plan_markdown)
        checklist_markdown = _normalize_markdown(checklist_markdown)

        if not _has_h1(travel_plan_markdown) or not _has_day_section(
            travel_plan_markdown
        ):
            raise ToolError(
                "travel_plan_markdown 必须包含 H1 标题和逐日章节（如“## 第 1 天”）。",
                error_code="INVALID_ARGUMENTS",
            )

        if not _has_h1(checklist_markdown) or not _has_list_items(checklist_markdown):
            raise ToolError(
                "checklist_markdown 必须包含 H1 标题和至少一个清单项。",
                error_code="INVALID_ARGUMENTS",
            )

        destination = str(plan_data.get("destination") or "未知目的地")

        return {
            "summary": f"已生成并冻结 {destination} 的 travel_plan.md 与 checklist.md",
            "travel_plan_markdown": travel_plan_markdown,
            "checklist_markdown": checklist_markdown,
        }

    return generate_trip_summary
