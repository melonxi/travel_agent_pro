"""Category A: high-risk strong-schema Phase 3 tools.

These tools receive structured list[dict] or nested dict. Their JSON Schemas
forbid strings; this is where stringification is eradicated.
"""

from __future__ import annotations

from state.models import TravelPlanState
from state.plan_writers import (
    clear_selected_skeleton_id,
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


def _validated_skeleton_id_map(
    skeleton_plans: list[object],
) -> tuple[dict[str, str], set[str]]:
    valid_ids: dict[str, str] = {}
    colliding_ids: set[str] = set()
    for item in skeleton_plans:
        if not isinstance(item, dict):
            continue
        skeleton_id = item.get("id")
        if isinstance(skeleton_id, str) and skeleton_id.strip():
            normalized_id = skeleton_id.strip()
            existing_raw_id = valid_ids.get(normalized_id)
            if existing_raw_id is None:
                valid_ids[normalized_id] = skeleton_id
            else:
                colliding_ids.add(normalized_id)
    return valid_ids, colliding_ids


def _reconcile_selected_skeleton_after_rewrite(
    plan: TravelPlanState,
    previous_skeleton_plans: list[object],
    normalized_plans: list[dict],
    seen_ids: set[str],
) -> None:
    current_selected_id = plan.selected_skeleton_id
    if not isinstance(current_selected_id, str):
        return

    normalized_selected_id = current_selected_id.strip()
    matched_previous_id_indexes: set[int] = set()
    matched_previous_name_indexes: set[int] = set()
    for index, item in enumerate(previous_skeleton_plans):
        if not isinstance(item, dict):
            continue
        previous_id = item.get("id")
        previous_name = item.get("name")
        if (
            isinstance(previous_id, str)
            and previous_id.strip() == normalized_selected_id
        ):
            matched_previous_id_indexes.add(index)
        if (
            isinstance(previous_name, str)
            and previous_name.strip() == normalized_selected_id
        ):
            matched_previous_name_indexes.add(index)

    matched_previous_indexes = (
        matched_previous_id_indexes | matched_previous_name_indexes
    )
    if len(matched_previous_indexes) != 1:
        clear_selected_skeleton_id(plan)
        return

    matched_previous_index = next(iter(matched_previous_indexes))
    matched_ids = [
        skeleton_plan["id"]
        for skeleton_plan in normalized_plans
        if skeleton_plan.get("name") == normalized_selected_id
    ]

    def write_if_uniquely_resolved(candidate: str) -> None:
        matching_indexes = {
            index
            for index, skeleton_plan in enumerate(normalized_plans)
            if skeleton_plan.get("id") == candidate
            or skeleton_plan.get("name") == candidate
        }
        if len(matching_indexes) == 1:
            write_selected_skeleton_id(plan, candidate)
        else:
            clear_selected_skeleton_id(plan)

    if matched_previous_index in matched_previous_id_indexes:
        if normalized_selected_id in seen_ids:
            write_if_uniquely_resolved(normalized_selected_id)
        elif (
            matched_previous_index in matched_previous_name_indexes
            and len(matched_ids) == 1
        ):
            write_if_uniquely_resolved(matched_ids[0])
        else:
            clear_selected_skeleton_id(plan)
        return

    if len(matched_ids) == 1:
        write_if_uniquely_resolved(matched_ids[0])
    else:
        clear_selected_skeleton_id(plan)


def make_set_skeleton_plans_tool(plan: TravelPlanState):
    @tool(
        name="set_skeleton_plans",
        description=(
            "写入骨架方案列表（整体替换）。每个方案必须包含 id 和 name。\n"
            "触发条件：完成 2-3 套骨架方案设计后必须立即调用，不允许只在正文描述方案而不写入状态。\n"
            "禁止行为：不要在回复正文中完整列出骨架方案却不调用此工具——右侧工作台和后续阶段依赖此状态字段。\n"
            "写入后效果：skeleton_plans 整体替换；如果之前已有 selected_skeleton_id，系统会自动检查其是否仍然有效。"
        ),
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
        seen_ids: set[str] = set()
        normalized_plans: list[dict] = []
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
            skeleton_id = p.get("id")
            if not isinstance(skeleton_id, str) or not skeleton_id.strip():
                raise ToolError(
                    f"plans[{i}].id 必须是非空字符串",
                    error_code="INVALID_VALUE",
                    suggestion='每个骨架必须有非空 id，如 {"id": "plan_a", "name": "轻松版", ...}',
                )
            if "name" not in p:
                raise ToolError(
                    f"plans[{i}] 缺少必填字段 'name'",
                    error_code="INVALID_VALUE",
                    suggestion='每个骨架必须有 name 字段，如 {"id": "plan_a", "name": "轻松版", ...}',
                )
            skeleton_name = p.get("name")
            if not isinstance(skeleton_name, str) or not skeleton_name.strip():
                raise ToolError(
                    f"plans[{i}].name 必须是非空字符串",
                    error_code="INVALID_VALUE",
                    suggestion='每个骨架必须有非空 name，如 {"id": "plan_a", "name": "轻松版", ...}',
                )
            normalized_id = skeleton_id.strip()
            normalized_name = skeleton_name.strip()
            if normalized_id in seen_ids:
                raise ToolError(
                    f"plans[{i}].id {normalized_id!r} 重复",
                    error_code="INVALID_VALUE",
                    suggestion="每个骨架方案的 id 必须唯一",
                )
            seen_ids.add(normalized_id)
            normalized_plan = dict(p)
            normalized_plan["id"] = normalized_id
            normalized_plan["name"] = normalized_name
            normalized_plans.append(normalized_plan)
        prev_count = len(plan.skeleton_plans)
        previous_skeleton_plans = list(plan.skeleton_plans)
        write_skeleton_plans(plan, normalized_plans)
        _reconcile_selected_skeleton_after_rewrite(
            plan,
            previous_skeleton_plans,
            normalized_plans,
            seen_ids,
        )
        return {
            "updated_field": "skeleton_plans",
            "count": len(normalized_plans),
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
        description=(
            "锁定一套骨架方案。id 必须匹配已写入 skeleton_plans 中的某个方案 id。\n"
            "触发条件：用户明确选择了某套骨架方案后必须立即调用。\n"
            "前置条件：skeleton_plans 必须已写入且不为空。\n"
            "禁止行为：不要用 set_trip_brief 来记录用户的骨架选择——set_trip_brief 只用于旅行画像，骨架选择必须用本工具。\n"
            "写入后效果：selected_skeleton_id 写入后，系统会自动推进到 lock 子阶段。"
        ),
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
        normalized_id = id.strip()
        existing_id_map, colliding_ids = _validated_skeleton_id_map(plan.skeleton_plans)
        if normalized_id in colliding_ids:
            raise ToolError(
                f"id={normalized_id!r} 存在冲突的历史骨架记录",
                error_code="INVALID_VALUE",
                suggestion="请先清理重复的骨架 id 后再选择",
            )
        if normalized_id not in existing_id_map:
            selectable_ids = [
                existing_id
                for existing_id in existing_id_map
                if existing_id not in colliding_ids
            ]
            raise ToolError(
                f"未找到 id={normalized_id!r} 的骨架方案",
                error_code="INVALID_VALUE",
                suggestion=f"可选 id: {', '.join(selectable_ids) if selectable_ids else '(无已写入骨架)'}",
            )
        matched_id = existing_id_map[normalized_id]
        prev = plan.selected_skeleton_id
        write_selected_skeleton_id(plan, matched_id)
        return {
            "updated_field": "selected_skeleton_id",
            "new_value": matched_id,
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
        description=(
            "写入候选池（整体替换）。每个候选项必须是 JSON 对象。\n"
            "触发条件：完成候选景点/活动/区域的收集后必须立即调用，不允许只在正文列出候选而不写入状态。\n"
            "禁止行为：不要把筛选后的短名单写入 candidate_pool——全集用 set_candidate_pool，筛选结果用 set_shortlist。\n"
            "写入后效果：candidate_pool 整体替换，右侧工作台会展示候选全集。"
        ),
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
        description=(
            "写入候选短名单（整体替换）。\n"
            "触发条件：完成候选筛选后必须立即调用，通常与 set_candidate_pool 在同一轮使用。\n"
            "前置条件：应先写入 candidate_pool，再写入 shortlist。\n"
            "禁止行为：不要把未筛选的全集写入 shortlist——全集用 set_candidate_pool。\n"
            "写入后效果：shortlist 写入后，系统会自动推进到 skeleton 子阶段。"
        ),
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
        description=(
            "写入交通候选列表（整体替换）。\n"
            "触发条件：搜索到大交通方案（航班、火车等）后必须立即调用，不允许只在正文描述交通方案而不写入状态。\n"
            "禁止行为：不要把用户最终选中的交通方案写入此字段——选中结果用 select_transport。\n"
            "写入后效果：transport_options 整体替换，右侧工作台会展示交通候选列表。"
        ),
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
        description=(
            "锁定交通方案。传入选中的交通对象。\n"
            "触发条件：用户明确选择了某个交通方案后必须立即调用。\n"
            "前置条件：transport_options 应已写入。\n"
            "禁止行为：不要在用户未明确确认时擅自调用此工具。\n"
            "写入后效果：selected_transport 写入，锁定大交通选择。"
        ),
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
        description=(
            "写入住宿候选列表（整体替换）。\n"
            "触发条件：搜索到住宿方案后必须立即调用，不允许只在正文描述住宿选项而不写入状态。\n"
            "禁止行为：不要把用户最终确认的住宿写入此字段——确认结果用 set_accommodation。\n"
            "写入后效果：accommodation_options 整体替换，右侧工作台会展示住宿候选列表。"
        ),
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
        description=(
            "锁定住宿区域和酒店。\n"
            "触发条件：用户明确确认住宿区域或酒店后必须立即调用。\n"
            "前置条件：accommodation_options 应已写入，或用户直接指定了住宿。\n"
            "禁止行为：不要在用户未明确确认时擅自调用此工具。\n"
            "写入后效果：accommodation 写入，是 Phase 3 完成的必要条件之一。"
        ),
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
        description=(
            "写入风险点列表（整体替换）。\n"
            "触发条件：识别到行程中的风险点（天气、交通、时间冲突、安全等）后应调用。\n"
            "写入后效果：risks 整体替换，前端会在行程旁展示风险提示。"
        ),
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
        description=(
            "写入备选方案列表（整体替换）。\n"
            "触发条件：为行程中的高风险环节准备了雨天备案或替代方案后应调用。\n"
            "写入后效果：alternatives 整体替换，前端会在行程旁展示备选方案。"
        ),
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
        description=(
            "更新旅行画像（增量合并到现有 trip_brief）。\n"
            "触发条件：收集到用户的旅行目标、节奏偏好、出发城市、必去/不去等画像信息后必须立即调用。\n"
            "禁止行为：此工具只用于写旅行画像，不要用它记录骨架选择（应用 select_skeleton）、候选池（应用 set_candidate_pool）或其他非画像信息。\n"
            "写入后效果：trip_brief 增量合并，brief 子阶段完成的必要条件。"
        ),
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
