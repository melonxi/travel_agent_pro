from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RepairHintOutcome:
    message: str
    key: str


def build_phase3_state_repair_message(
    *,
    plan: Any | None,
    current_phase: int,
    assistant_text: str,
    repair_hints_used: set[str],
) -> RepairHintOutcome | None:
    if current_phase != 3 or plan is None:
        return None
    if not plan.destination:
        return None
    text = assistant_text.strip()
    if len(text) < 12:
        return None

    step = getattr(plan, "phase3_step", "")
    repair_key = f"p3_{step}"
    if repair_key in repair_hints_used:
        stronger_key = f"p3_{step}_retry"
        if stronger_key in repair_hints_used:
            return None
        repair_key = stronger_key

    skeleton_signals = ("骨架", "轻松版", "平衡版", "高密度版", "深度版", "跳岛")
    has_skeleton_signals = any(token in text for token in skeleton_signals) or bool(
        re.search(r"方案\s*[A-C1-3]", text)
    )

    if (
        step == "brief"
        and not plan.trip_brief
        and any(token in text for token in ("画像", "偏好", "约束", "预算", "日期", "旅行"))
    ):
        return RepairHintOutcome(
            key=repair_key,
            message=(
                "[状态同步提醒]\n"
                "你刚刚已经完成了旅行画像说明，但 `trip_brief` 仍为空。"
                "请先调用 `set_trip_brief(fields={goal, pace, departure_city})`"
                " 写入画像核心字段；must_do 用 `add_preferences` 写入，"
                "avoid 用 `add_constraints` 写入，预算用 `update_trip_basics` 写入。"
                "写完后再继续，不要重复整段面向用户解释。"
            ),
        )

    if step == "candidate":
        if not plan.shortlist and any(
            token in text for token in ("候选", "推荐", "不建议", "why", "why_not")
        ):
            if not plan.candidate_pool:
                return RepairHintOutcome(
                    key=repair_key,
                    message=(
                        "[状态同步提醒]\n"
                        "你刚刚已经给出了候选筛选结果，但 `candidate_pool` 仍为空。"
                        "请先调用 `set_candidate_pool(pool=[...])` 写入候选全集，"
                        "再调用 `set_shortlist(items=[...])` 写入第一轮筛选结果。"
                        "写入 shortlist 后系统会自动推进子阶段。"
                    ),
                )
            return RepairHintOutcome(
                key=repair_key,
                message=(
                    "[状态同步提醒]\n"
                    "你刚刚已经给出了候选筛选结果，但 `shortlist` 仍为空。"
                    "请先调用 `set_shortlist(items=[...])` 写入第一轮筛选结果。"
                    "写入 shortlist 后系统会自动推进子阶段。"
                ),
            )

        if not plan.skeleton_plans and has_skeleton_signals:
            return RepairHintOutcome(
                key=repair_key,
                message=(
                    "[状态同步提醒]\n"
                    "你刚刚已经给出了骨架方案，但 `skeleton_plans` 仍为空。"
                    "请先调用 `set_skeleton_plans(plans=[...])`"
                    " 写入结构化骨架方案列表（每个方案必须包含 `id` 和 `name`）。"
                    '如果用户已经明确选中某套方案，再调用 `select_skeleton(id="...")`。'
                    "写入后系统会自动推进子阶段。"
                ),
            )

    if step == "skeleton" and not plan.skeleton_plans and has_skeleton_signals:
        return RepairHintOutcome(
            key=repair_key,
            message=(
                "[状态同步提醒]\n"
                "你刚刚已经给出了 2-3 套骨架方案，但 `skeleton_plans` 仍为空。"
                "请先调用 `set_skeleton_plans(plans=[...])`"
                " 写入结构化骨架方案列表。"
                '如果用户已经明确选中某套方案，再调用 `select_skeleton(id="...")`，'
                "系统会自动推进到 lock 子阶段。"
            ),
        )

    if step == "lock":
        missing_fields: list[str] = []
        if not plan.transport_options and any(
            token in text for token in ("航班", "火车", "高铁", "交通")
        ):
            missing_fields.append("`set_transport_options(options=[...])`")
        if (
            not plan.accommodation_options
            and not plan.accommodation
            and any(token in text for token in ("住宿", "酒店", "民宿", "旅馆"))
        ):
            missing_fields.append(
                "`set_accommodation_options(options=[...])` 或 `set_accommodation(area=...)`"
            )
        if not plan.risks and any(token in text for token in ("风险", "注意", "天气")):
            missing_fields.append("`set_risks(list=[...])`")
        if not plan.alternatives and any(
            token in text for token in ("备选", "替代", "雨天")
        ):
            missing_fields.append("`set_alternatives(list=[...])`")
        if missing_fields:
            fields_str = "、".join(missing_fields)
            return RepairHintOutcome(
                key=repair_key,
                message=(
                    "[状态同步提醒]\n"
                    f"你刚刚已经给出了锁定阶段建议，但以下字段仍未写入：{fields_str}。"
                    "请先把结构化结果写入对应字段；只有用户明确选中了交通或住宿时，才写 `selected_transport` 或 `accommodation`。"
                ),
            )

    return None


def build_phase5_state_repair_message(
    *,
    plan: Any | None,
    current_phase: int,
    assistant_text: str,
    repair_hints_used: set[str],
) -> RepairHintOutcome | None:
    if current_phase != 5 or plan is None:
        return None
    if not plan.dates:
        return None
    repair_key = "p5_daily"
    if repair_key in repair_hints_used:
        return None
    text = assistant_text.strip()
    if len(text) < 20:
        return None

    total_days = plan.dates.total_days
    planned_days = set()
    for daily_plan in plan.daily_plans:
        if hasattr(daily_plan, "day"):
            planned_days.add(daily_plan.day)
        elif isinstance(daily_plan, dict):
            planned_days.add(daily_plan.get("day"))
    planned_count = len(planned_days)

    if planned_count >= total_days:
        return None

    day_pattern_count = len(
        re.findall(
            r"第\s*[1-9一二三四五六七八九十]\s*天|Day\s*\d|DAY\s*\d",
            text,
        )
    )
    has_time_slots = bool(re.search(r"\d{1,2}:\d{2}", text))
    has_activity_markers = any(
        keyword in text
        for keyword in ("活动", "景点", "行程", "安排", "上午", "下午", "晚上", "餐厅")
    )
    has_json_markers = (
        sum(
            1
            for keyword in ('"day"', '"date"', '"activities"', '"start_time"')
            if keyword in text
        )
        >= 2
    )
    has_date_patterns = bool(re.search(r"\d{4}-\d{2}-\d{2}", text))

    if (
        (day_pattern_count >= 1 and (has_time_slots or has_activity_markers))
        or has_json_markers
        or (has_date_patterns and has_activity_markers)
    ):
        return RepairHintOutcome(
            key=repair_key,
            message=(
                "[状态同步提醒]\n"
                f"你刚刚已经给出了逐日行程安排，但 `daily_plans` 仍只有 {planned_count}/{total_days} 天。"
                '请立即调用 `save_day_plan(mode="create", day=缺失天数, date=对应日期, activities=活动列表)` 逐天保存缺失天数，'
                "或在需要一次性完整覆盖时调用 `replace_all_day_plans(days=完整天数列表)`。"
                "`optimize_day_route` 只做路线辅助，不能替代状态写入。"
            ),
        )
    return None
