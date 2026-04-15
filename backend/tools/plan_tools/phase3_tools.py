"""Category A: high-risk strong-schema Phase 3 tools.

These tools receive structured list[dict] or nested dict. Their JSON Schemas
forbid strings; this is where stringification is eradicated.
"""
from __future__ import annotations

from state.models import TravelPlanState
from state.plan_writers import (
    write_accommodation,
    write_accommodation_options,
    write_alternatives,
    write_candidate_pool,
    write_risks,
    write_selected_skeleton_id,
    write_selected_transport,
    write_shortlist,
    write_skeleton_plans,
    write_transport_options,
    write_trip_brief,
)
from tools.base import ToolError, tool


# ---------------------------------------------------------------------------
# set_skeleton_plans
# ---------------------------------------------------------------------------

_SET_SKELETON_PLANS_PARAMS = {
    "type": "object",
    "properties": {
        "plans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "days": {"type": "array", "items": {"type": "object"}},
                    "tradeoffs": {"type": "object"},
                },
                "required": ["id", "name"],
            },
            "description": "骨架方案列表，每个方案包含 id, name, days, tradeoffs",
        },
    },
    "required": ["plans"],
}


def make_set_skeleton_plans_tool(plan: TravelPlanState):
    @tool(
        name="set_skeleton_plans",
        description="写入骨架方案列表（整体替换）。每个方案必须包含 id 和 name。",
        phases=[3],
        parameters=_SET_SKELETON_PLANS_PARAMS,
        side_effect="write",
        human_label="写入骨架方案",
    )
    async def set_skeleton_plans(plans: list) -> dict:
        if not isinstance(plans, list):
            raise ToolError(
                f"plans 必须是 list，收到 {type(plans).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, p in enumerate(plans):
            if not isinstance(p, dict):
                raise ToolError(
                    f"plans[{i}] 必须是 dict，收到 {type(p).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个骨架方案必须是 JSON 对象",
                )
            if "id" not in p:
                raise ToolError(
                    f"plans[{i}] 缺少必填字段 'id'",
                    error_code="INVALID_VALUE",
                    suggestion='每个骨架必须有 id 字段，如 {"id": "plan_a", "name": "轻松版", ...}',
                )
        prev_count = len(plan.skeleton_plans)
        write_skeleton_plans(plan, plans)
        return {
            "updated_field": "skeleton_plans",
            "count": len(plans),
            "previous_count": prev_count,
        }

    return set_skeleton_plans


# ---------------------------------------------------------------------------
# select_skeleton
# ---------------------------------------------------------------------------

_SELECT_SKELETON_PARAMS = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "要锁定的骨架方案 ID（必须匹配 skeleton_plans 中某项的 id）",
        },
    },
    "required": ["id"],
}


def make_select_skeleton_tool(plan: TravelPlanState):
    @tool(
        name="select_skeleton",
        description="锁定一套骨架方案。id 必须匹配已写入 skeleton_plans 中的某个方案 id。",
        phases=[3],
        parameters=_SELECT_SKELETON_PARAMS,
        side_effect="write",
        human_label="锁定骨架方案",
    )
    async def select_skeleton(id: str) -> dict:
        if not isinstance(id, str) or not id.strip():
            raise ToolError(
                "id 必须是非空字符串",
                error_code="INVALID_VALUE",
                suggestion="请传入骨架方案的 id 字段值",
            )
        existing_ids = [s.get("id") for s in plan.skeleton_plans if isinstance(s, dict)]
        if id not in existing_ids:
            raise ToolError(
                f"未找到 id={id!r} 的骨架方案",
                error_code="INVALID_VALUE",
                suggestion=f"可选 id: {', '.join(existing_ids) if existing_ids else '(无已写入骨架)'}",
            )
        prev = plan.selected_skeleton_id
        write_selected_skeleton_id(plan, id)
        return {
            "updated_field": "selected_skeleton_id",
            "new_value": id,
            "previous_value": prev,
        }

    return select_skeleton


# ---------------------------------------------------------------------------
# set_candidate_pool
# ---------------------------------------------------------------------------

_SET_CANDIDATE_POOL_PARAMS = {
    "type": "object",
    "properties": {
        "pool": {
            "type": "array",
            "items": {"type": "object"},
            "description": "候选池列表",
        },
    },
    "required": ["pool"],
}


def make_set_candidate_pool_tool(plan: TravelPlanState):
    @tool(
        name="set_candidate_pool",
        description="写入候选池（整体替换）。每个候选项必须是 JSON 对象。",
        phases=[3],
        parameters=_SET_CANDIDATE_POOL_PARAMS,
        side_effect="write",
        human_label="写入候选池",
    )
    async def set_candidate_pool(pool: list) -> dict:
        if not isinstance(pool, list):
            raise ToolError(
                f"pool 必须是 list，收到 {type(pool).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(pool):
            if not isinstance(item, dict):
                raise ToolError(
                    f"pool[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个候选项必须是 JSON 对象",
                )
        prev_count = len(plan.candidate_pool)
        write_candidate_pool(plan, pool)
        return {
            "updated_field": "candidate_pool",
            "count": len(pool),
            "previous_count": prev_count,
        }

    return set_candidate_pool


# ---------------------------------------------------------------------------
# set_shortlist
# ---------------------------------------------------------------------------

_SET_SHORTLIST_PARAMS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object"},
            "description": "候选短名单",
        },
    },
    "required": ["items"],
}


def make_set_shortlist_tool(plan: TravelPlanState):
    @tool(
        name="set_shortlist",
        description="写入候选短名单（整体替换）。",
        phases=[3],
        parameters=_SET_SHORTLIST_PARAMS,
        side_effect="write",
        human_label="写入候选短名单",
    )
    async def set_shortlist(items: list) -> dict:
        if not isinstance(items, list):
            raise ToolError(
                f"items 必须是 list，收到 {type(items).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ToolError(
                    f"items[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个短名单项必须是 JSON 对象",
                )
        prev_count = len(plan.shortlist)
        write_shortlist(plan, items)
        return {
            "updated_field": "shortlist",
            "count": len(items),
            "previous_count": prev_count,
        }

    return set_shortlist


# ---------------------------------------------------------------------------
# set_transport_options
# ---------------------------------------------------------------------------

_SET_TRANSPORT_OPTIONS_PARAMS = {
    "type": "object",
    "properties": {
        "options": {
            "type": "array",
            "items": {"type": "object"},
            "description": "交通候选列表",
        },
    },
    "required": ["options"],
}


def make_set_transport_options_tool(plan: TravelPlanState):
    @tool(
        name="set_transport_options",
        description="写入交通候选列表（整体替换）。",
        phases=[3],
        parameters=_SET_TRANSPORT_OPTIONS_PARAMS,
        side_effect="write",
        human_label="写入交通候选",
    )
    async def set_transport_options(options: list) -> dict:
        if not isinstance(options, list):
            raise ToolError(
                f"options 必须是 list，收到 {type(options).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(options):
            if not isinstance(item, dict):
                raise ToolError(
                    f"options[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个交通选项必须是 JSON 对象",
                )
        prev_count = len(plan.transport_options)
        write_transport_options(plan, options)
        return {
            "updated_field": "transport_options",
            "count": len(options),
            "previous_count": prev_count,
        }

    return set_transport_options


# ---------------------------------------------------------------------------
# select_transport
# ---------------------------------------------------------------------------

_SELECT_TRANSPORT_PARAMS = {
    "type": "object",
    "properties": {
        "choice": {
            "type": "object",
            "description": "选中的交通方案",
        },
    },
    "required": ["choice"],
}


def make_select_transport_tool(plan: TravelPlanState):
    @tool(
        name="select_transport",
        description="锁定交通方案。传入选中的交通对象。",
        phases=[3],
        parameters=_SELECT_TRANSPORT_PARAMS,
        side_effect="write",
        human_label="锁定交通方案",
    )
    async def select_transport(choice: dict) -> dict:
        if not isinstance(choice, dict):
            raise ToolError(
                f"choice 必须是 dict，收到 {type(choice).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 JSON 对象",
            )
        prev = plan.selected_transport
        write_selected_transport(plan, choice)
        return {
            "updated_field": "selected_transport",
            "new_value": choice,
            "previous_value": prev,
        }

    return select_transport


# ---------------------------------------------------------------------------
# set_accommodation_options
# ---------------------------------------------------------------------------

_SET_ACCOMMODATION_OPTIONS_PARAMS = {
    "type": "object",
    "properties": {
        "options": {
            "type": "array",
            "items": {"type": "object"},
            "description": "住宿候选列表",
        },
    },
    "required": ["options"],
}


def make_set_accommodation_options_tool(plan: TravelPlanState):
    @tool(
        name="set_accommodation_options",
        description="写入住宿候选列表（整体替换）。",
        phases=[3],
        parameters=_SET_ACCOMMODATION_OPTIONS_PARAMS,
        side_effect="write",
        human_label="写入住宿候选",
    )
    async def set_accommodation_options(options: list) -> dict:
        if not isinstance(options, list):
            raise ToolError(
                f"options 必须是 list，收到 {type(options).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(options):
            if not isinstance(item, dict):
                raise ToolError(
                    f"options[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个住宿选项必须是 JSON 对象",
                )
        prev_count = len(plan.accommodation_options)
        write_accommodation_options(plan, options)
        return {
            "updated_field": "accommodation_options",
            "count": len(options),
            "previous_count": prev_count,
        }

    return set_accommodation_options


# ---------------------------------------------------------------------------
# set_accommodation
# ---------------------------------------------------------------------------

_SET_ACCOMMODATION_PARAMS = {
    "type": "object",
    "properties": {
        "area": {
            "type": "string",
            "description": "住宿区域/地址",
        },
        "hotel": {
            "type": "string",
            "description": "酒店名称（可选）",
        },
    },
    "required": ["area"],
}


def make_set_accommodation_tool(plan: TravelPlanState):
    @tool(
        name="set_accommodation",
        description="锁定住宿区域和酒店。",
        phases=[3, 5],
        parameters=_SET_ACCOMMODATION_PARAMS,
        side_effect="write",
        human_label="锁定住宿",
    )
    async def set_accommodation(area: str, hotel: str | None = None) -> dict:
        if not isinstance(area, str) or not area.strip():
            raise ToolError(
                "area 必须是非空字符串",
                error_code="INVALID_VALUE",
                suggestion='示例: "新宿"',
            )
        prev = plan.accommodation.to_dict() if plan.accommodation else None
        write_accommodation(plan, area=area.strip(), hotel=hotel)
        return {
            "updated_field": "accommodation",
            "new_value": plan.accommodation.to_dict(),
            "previous_value": prev,
        }

    return set_accommodation


# ---------------------------------------------------------------------------
# set_risks
# ---------------------------------------------------------------------------

_SET_RISKS_PARAMS = {
    "type": "object",
    "properties": {
        "list": {
            "type": "array",
            "items": {"type": "object"},
            "description": "风险点列表",
        },
    },
    "required": ["list"],
}


def make_set_risks_tool(plan: TravelPlanState):
    @tool(
        name="set_risks",
        description="写入风险点列表（整体替换）。",
        phases=[3, 5],
        parameters=_SET_RISKS_PARAMS,
        side_effect="write",
        human_label="写入风险点",
    )
    async def set_risks(list: list) -> dict:
        items = list
        if not isinstance(items, type([])):
            raise ToolError(
                f"list 必须是 list，收到 {type(items).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ToolError(
                    f"list[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个风险点必须是 JSON 对象",
                )
        prev_count = len(plan.risks)
        write_risks(plan, items)
        return {
            "updated_field": "risks",
            "count": len(items),
            "previous_count": prev_count,
        }

    return set_risks


# ---------------------------------------------------------------------------
# set_alternatives
# ---------------------------------------------------------------------------

_SET_ALTERNATIVES_PARAMS = {
    "type": "object",
    "properties": {
        "list": {
            "type": "array",
            "items": {"type": "object"},
            "description": "备选方案列表",
        },
    },
    "required": ["list"],
}


def make_set_alternatives_tool(plan: TravelPlanState):
    @tool(
        name="set_alternatives",
        description="写入备选方案列表（整体替换）。",
        phases=[3, 5],
        parameters=_SET_ALTERNATIVES_PARAMS,
        side_effect="write",
        human_label="写入备选方案",
    )
    async def set_alternatives(list: list) -> dict:
        items = list
        if not isinstance(items, type([])):
            raise ToolError(
                f"list 必须是 list，收到 {type(items).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]",
            )
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ToolError(
                    f"list[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个备选方案必须是 JSON 对象",
                )
        prev_count = len(plan.alternatives)
        write_alternatives(plan, items)
        return {
            "updated_field": "alternatives",
            "count": len(items),
            "previous_count": prev_count,
        }

    return set_alternatives


# ---------------------------------------------------------------------------
# set_trip_brief
# ---------------------------------------------------------------------------

_SET_TRIP_BRIEF_PARAMS = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "object",
            "description": "旅行画像字段，增量合并到 trip_brief 中",
        },
    },
    "required": ["fields"],
}


def make_set_trip_brief_tool(plan: TravelPlanState):
    @tool(
        name="set_trip_brief",
        description="更新旅行画像（增量合并到现有 trip_brief）。",
        phases=[3],
        parameters=_SET_TRIP_BRIEF_PARAMS,
        side_effect="write",
        human_label="更新旅行画像",
    )
    async def set_trip_brief(fields: dict) -> dict:
        if not isinstance(fields, dict):
            raise ToolError(
                f"fields 必须是 dict，收到 {type(fields).__name__}",
                error_code="INVALID_VALUE",
                suggestion='示例: {"goal": "慢旅行", "pace": "relaxed"}',
            )
        prev = dict(plan.trip_brief)
        write_trip_brief(plan, fields)
        return {
            "updated_field": "trip_brief",
            "new_value": plan.trip_brief,
            "previous_value": prev,
        }

    return set_trip_brief
