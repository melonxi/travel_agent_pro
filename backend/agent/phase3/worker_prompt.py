# backend/agent/worker_prompt.py
"""Worker system prompt templates for Phase 3 parallel mode.

Design goal: maximize shared prefix across all Day Workers to achieve
high KV-Cache hit rates (Manus / Claude Code fork sub-agent pattern).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from state.models import TravelPlanState, normalize_pace_value

PACE_CORE_ACTIVITY_RANGES: dict[str, tuple[int, int]] = {
    "relaxed": (2, 3),
    "balanced": (3, 4),
    "intensive": (4, 5),
}


def core_activity_range_for_pace(pace: str | None) -> tuple[int, int]:
    normalized = normalize_pace_value(pace) or "balanced"
    return PACE_CORE_ACTIVITY_RANGES[normalized]


def max_core_activities_for_pace(pace: str | None) -> int:
    return core_activity_range_for_pace(pace)[1]


def core_activity_range_label(pace: str | None) -> str:
    low, high = core_activity_range_for_pace(pace)
    return f"{low}-{high}"


@dataclass
class DayTask:
    """A single day's task extracted from the skeleton."""

    day: int
    date: str
    skeleton_slice: dict[str, Any]
    pace: str
    locked_pois: list[str] = field(default_factory=list)
    candidate_pois: list[str] = field(default_factory=list)
    forbidden_pois: list[str] = field(default_factory=list)
    area_cluster: list[str] = field(default_factory=list)
    mobility_envelope: dict[str, Any] = field(default_factory=dict)
    fallback_slots: list[dict] = field(default_factory=list)
    date_role: str = "full_day"
    repair_hints: list[str] = field(default_factory=list)
    day_budget: int | None = None
    day_constraints: list[dict[str, str]] = field(default_factory=list)
    arrival_time: str | None = None
    departure_time: str | None = None
    arrival_transport: dict[str, Any] | None = None
    departure_transport: dict[str, Any] | None = None
    max_core_activities: int | None = None
    candidate_activity_slots: int | None = None
    demoted_locked_pois: list[str] = field(default_factory=list)


_WORKER_ROLE = """## 角色

你是单日行程落地规划师，由主 Agent 派发的并行子任务执行者。
你是多个并行 Worker 之一——其他 Worker 正在规划其他天的行程，你只负责指定的一天。

## 无用户交互

你与用户没有任何交互通道。不要提问、不要请求确认、不要给出 2-3 个选项让人选。
所有判断由你独立做出，通过 `submit_day_plan_candidate` 提交结果。

## 完成优于完美

一个覆盖所有硬约束的 70 分保守 DayPlan 远胜于一个无限搜索未完成的计划。
优先提交，Orchestrator 会做全局验证和修补。

## 优先级（冲突时）

1. 当前 DayTask 的硬约束（locked / forbidden / area_cluster / mobility）
2. 骨架的 area / theme / core_activities（方向性参考）
3. 通用旅行规划常识

## 硬法则

- 严格基于骨架安排展开，不要偷偷替换区域或主题。
- 区域连续性优先于景点密度——同一天的活动应在地理上聚拢。
- 时间安排必须留出现实缓冲（交通延误、排队、休息），不要把活动首尾无缝拼死。
- 连续活动必须满足：上一活动 end_time + 下一活动的 transport_duration_min <= 下一活动 start_time；不满足时必须减少活动或后移下一活动。
- 用 get_poi_info 补齐缺失的坐标和票价；开放时间无法结构化保存，关键不确定性写入 notes。
- 用 optimize_day_route 优化活动顺序。
- 用 calculate_route 验证关键移动是否可行。
- 普通用餐、接驳、入住优先合并到相邻活动的 notes；只有它本身是核心体验时才单独作为 activity。
- 餐饮作为核心体验时可用 category="food"，安排在合理时段。
- 到达 / 离开交通以已锁定大交通为唯一来源；禁止用 `web_search` 重新查航班或替换航班。锁定态缺字段时写 notes，不得自行补航班号、机场或票价。

## 你与全局的关系

- `forbidden_pois` 中的景点是其他天已经锁定的核心景点——使用它们会导致跨天 POI 重复，触发 Orchestrator 重新分配（计为你的失败）。
- 你提交的 DayPlan 由 Orchestrator 做跨天 POI 去重、时间冲突、预算检查等全局校验。
- 如果你的输出有局部问题，Orchestrator 会发回修复要求（repair_hints），你只需修正指定问题，不需要重做整天。
- 预算分配参考：每天大致均分总预算即可。

## 工具回退策略

- 当专项工具返回无效信息时，可以进行有限次补救，但不要围绕同一 POI 或同一问题无限搜索。
- 如果已经具备区域、主题、核心活动和基本时间结构，应优先输出保守版 DayPlan。
- 当工具仍无法补齐细节时，可以基于骨架、区域连续性和常识性节奏完成保守安排。
- 不得编造具体营业时间、具体票价、明确预约要求；无法确认的事实写入 notes。
- 当系统提示进入收口模式时，必须停止继续调工具并直接提交 DayPlan。

## 交付方式（唯一合法路径）

你完成单日规划后，**必须**调用 `submit_day_plan_candidate` 工具提交 DayPlan。
这是提交 DayPlan 的唯一方式。

提交成功后，只输出一句简短确认："已提交第 N 天计划。"

❌ 不要在自然语言正文中输出完整 DayPlan JSON。
❌ 不要绕过工具直接输出 JSON。

唯一例外：如果 `submit_day_plan_candidate` 返回 `SUBMIT_UNAVAILABLE` 错误（工具不可用），
才可以在最终文本中输出 DayPlan JSON 作为系统故障兜底。

如果 `submit_day_plan_candidate` 返回其他错误：
- 根据错误信息修正 DayPlan 后再次提交（最多 1 次）
- 如果错误说明 day 不匹配，必须把 day 改为当前任务天数
- 如果错误说明字段缺失，必须补齐字段

## 状态写入边界

`submit_day_plan_candidate` 只提交候选 DayPlan 给 Orchestrator 校验。
它不会直接写入最终行程状态。
你不能假设提交后计划已经最终确认。
Orchestrator 会统一做跨天校验、必要重派和最终写入。"""

_DAYPLAN_SCHEMA = """## DayPlan 结构要求

无论是调用 `submit_day_plan_candidate`，还是在工具不可用时通过最终文本兜底输出，都必须使用以下结构：

```json
{
  "day": <天数>,
  "date": "<YYYY-MM-DD>",
"tips": "<当天小贴士：如天气提醒、穿着建议、注意事项>",
  "activities": [
    {
      "name": "<活动名称>",
      "location": {"name": "<地点名>", "lat": <纬度>, "lng": <经度>},
      "start_time": "HH:MM",
      "end_time": "HH:MM",
      "category": "<类别>",
      "cost": <人民币数字>,
      "transport_from_prev": "<可选：从上一地点的交通方式>",
      "transport_duration_min": "<可选：分钟数>",
      "transport_estimated": "<可选：true 表示该段交通时长为未经 calculate_route 验证的保守估算>",
      "notes": "<可选：如天气提醒、穿着建议、注意事项等小贴士，行程安排必须写在 activities>"
    }
  ]
}
```

硬约束：
- location 必须是 dict（含 name, lat, lng），不能是字符串
- start_time / end_time 必须是 "HH:MM" 格式，且 end_time > start_time
- cost 是数字（人民币），没有时填 0；不能是字符串如 "100元"
- category 必须是以下枚举之一：shrine, museum, food, transport, activity, shopping, park, viewpoint, experience
- 交通时长来自 calculate_route 实算时，transport_estimated 省略或为 false；未拿到可用路线而采用保守估算时，必须设 transport_estimated=true（不要只写进 notes 小字）

常见结构错误（绝对不允许）：
1. `"location": "浅草寺"` → 必须是 `{"name": "浅草寺", "lat": 35.7148, "lng": 139.7967}`
2. `"cost": "100元"` → 必须是数字 `100`
3. `"start_time": "09:00", "end_time": "09:00"` → end_time 必须晚于 start_time
4. `"category": "景点"` → 必须使用枚举值（如 shrine, museum, park 等）

完整字段定义和约束请以 `submit_day_plan_candidate` 工具的参数 schema 为准。"""

def _first_value(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _format_price(data: dict[str, Any]) -> str | None:
    price = data.get("price")
    currency = data.get("currency")
    if isinstance(price, dict):
        currency = currency or price.get("currency")
        price = price.get("total") or price.get("amount") or price.get("value")
    if price in (None, ""):
        price = data.get("total_price") or data.get("total")
    if price in (None, ""):
        return None
    if currency:
        return f"{price} {currency}"
    return str(price)


def _format_transport_segment(segment: dict[str, Any], label: str) -> str:
    parts = [label]
    airline = _first_value(
        segment,
        ("airline", "airline_name", "carrier", "marketing_airline"),
    )
    number = _first_value(
        segment,
        (
            "flight_number",
            "flight_no",
            "train_number",
            "train_no",
            "transport_no",
            "number",
        ),
    )
    if airline or number:
        parts.append(" ".join(str(v) for v in (airline, number) if v))

    dep = _first_value(
        segment,
        (
            "departure_airport",
            "departure_station",
            "from_airport",
            "from",
            "origin",
        ),
    )
    arr = _first_value(
        segment,
        (
            "arrival_airport",
            "arrival_station",
            "to_airport",
            "to",
            "destination",
        ),
    )
    route = segment.get("route")
    departure_text = segment.get("departure")
    if not route and isinstance(departure_text, str) and "→" in departure_text:
        route = departure_text
    dep_time = segment.get("departure_time") or segment.get("dep_time")
    arr_time = segment.get("arrival_time") or segment.get("arr_time")
    if dep_time is None and not route:
        dep_time = departure_text
    if arr_time is None:
        arr_time = segment.get("arrival")
    if route and not (dep or arr):
        parts.append(str(route))
    if dep or arr or dep_time or arr_time:
        left = " ".join(str(v) for v in (dep, dep_time) if v)
        right = " ".join(str(v) for v in (arr, arr_time) if v)
        parts.append(f"{left} -> {right}".strip())

    price = _format_price(segment)
    if price:
        parts.append(f"票价 {price}")

    return " / ".join(part for part in parts if part)


def _transport_payload_for_direction(
    transport: dict[str, Any],
    direction: str,
) -> dict[str, Any] | None:
    keys = ("outbound", "going") if direction == "outbound" else ("return", "inbound")
    for key in keys:
        value = transport.get(key)
        if isinstance(value, dict):
            payload = dict(value)
            payload.setdefault("direction", direction)
            return payload
    return None


def _transport_segments_by_direction(
    transport: dict[str, Any],
    direction: str,
) -> list[dict[str, Any]]:
    nested = _transport_payload_for_direction(transport, direction)
    if nested is not None:
        return [nested]

    segments = transport.get("segments")
    if not isinstance(segments, list):
        return []
    return [
        seg for seg in segments
        if isinstance(seg, dict) and seg.get("direction") == direction
    ]


def format_locked_transport_block(transport: dict[str, Any] | None) -> str:
    if not isinstance(transport, dict) or not transport:
        return ""

    lines = ["## 已锁定大交通（只读）", ""]
    label = _first_value(transport, ("label", "name", "id", "option_id"))
    if label:
        lines.append(f"- 方案：{label}")
    total_price = _format_price(transport)
    if total_price:
        lines.append(f"- 总价：{total_price}")

    outbound = _transport_segments_by_direction(transport, "outbound")
    returns = _transport_segments_by_direction(transport, "return")
    if not outbound and not returns and isinstance(transport.get("segments"), list):
        outbound = [seg for seg in transport["segments"] if isinstance(seg, dict)]

    for idx, segment in enumerate(outbound, start=1):
        lines.append(f"- {_format_transport_segment(segment, f'去程 {idx}')}")
    for idx, segment in enumerate(returns, start=1):
        lines.append(f"- {_format_transport_segment(segment, f'返程 {idx}')}")

    lines.append(
        "- 使用规则：到达 / 离开交通以此锁定态为唯一来源；"
        "禁止重新查航班、替换航班或补造缺失字段。"
    )
    return "\n".join(lines)


def build_shared_prefix(plan: TravelPlanState) -> str:
    """Build the shared prefix for all Day Workers.

    This prefix is identical across all workers to maximize KV-Cache hit rate.
    Do NOT include any per-day information here.
    """
    parts = []

    # 旅行上下文（只读）
    parts.append("## 旅行上下文\n")
    if plan.destination:
        parts.append(f"- 目的地：{plan.destination}")
    if plan.dates:
        parts.append(
            f"- 日期范围：{plan.dates.start} 至 {plan.dates.end}"
            f"（{plan.dates.total_days} 天）"
        )
    if plan.travelers:
        line = f"- 出行人数：{plan.travelers.adults} 成人"
        if plan.travelers.children:
            line += f"、{plan.travelers.children} 儿童"
        parts.append(line)
    if plan.trip_brief:
        _BRIEF_EXCLUDE = {"dates", "total_days", "budget_per_day"}
        _BRIEF_INCLUDE = {
            "goal",
            "pace",
            "departure_city",
            "style",
            "must_do",
            "avoid",
        }
        parts.append("- 旅行画像（全局）：")
        for key in sorted(plan.trip_brief.keys()):
            if key in _BRIEF_EXCLUDE:
                continue
            if key in _BRIEF_INCLUDE:
                parts.append(f"  - {key}: {plan.trip_brief[key]}")
    if plan.accommodation:
        parts.append(f"- 住宿区域：{plan.accommodation.area}")
        if plan.accommodation.hotel:
            parts.append(f"- 住宿酒店：{plan.accommodation.hotel}")
    locked_transport_block = format_locked_transport_block(plan.selected_transport)
    if locked_transport_block:
        parts.append("")
        parts.append(locked_transport_block)
    if plan.budget:
        parts.append(f"- 总预算：{plan.budget.total} {plan.budget.currency}")
        total_days = plan.dates.total_days if plan.dates else 0
        if total_days > 0:
            daily_avg = round(plan.budget.total / total_days)
            parts.append(f"- 日均参考：约 {daily_avg} {plan.budget.currency}/天")
    if plan.preferences:
        pref_strs = sorted(
            [f"{p.key}: {p.value}" for p in plan.preferences if p.key]
        )
        if pref_strs:
            parts.append(f"- 用户偏好：{'; '.join(pref_strs)}")

    # Only global hard constraints in shared prefix; day-level constraints go to suffix
    if plan.constraints:
        global_constraints = sorted(
            [
                f"[{c.type}] {c.description}"
                for c in plan.constraints
                if c.type == "hard"
            ]
        )
        if global_constraints:
            parts.append(f"- 全局硬约束：{'; '.join(global_constraints)}")

    # 角色和规则
    parts.append("\n---\n")
    parts.append(_WORKER_ROLE)
    parts.append("\n---\n")
    parts.append(_DAYPLAN_SCHEMA)

    return "\n".join(parts)


def _build_constraint_block(task: DayTask) -> str:
    lines: list[str] = []
    has_constraints = (
        task.locked_pois
        or task.candidate_pois
        or task.forbidden_pois
        or task.area_cluster
        or task.mobility_envelope
        or task.date_role != "full_day"
        or task.fallback_slots
        or task.repair_hints
        or task.arrival_transport
        or task.departure_transport
    )
    if not has_constraints:
        return ""

    lines.append("\n## 硬约束（必须遵守）\n")

    max_core = task.max_core_activities or max_core_activities_for_pace(task.pace)
    candidate_slots = (
        task.candidate_activity_slots
        if task.candidate_activity_slots is not None
        else max(max_core - len(task.locked_pois), 0)
    )
    lines.append(
        f"- **活动数量预算**：本天最多 {max_core} 个核心活动；"
        f"locked_pois 已占 {len(task.locked_pois)} 个，"
        f"candidate_pois 可新增名额 {candidate_slots} 个。"
    )
    lines.append(
        "  - 不要为普通用餐、入住、交通接驳单独增加 activity；"
        "把它们并入相邻核心活动的 notes。"
    )
    if task.demoted_locked_pois:
        lines.append(
            "- 以下 POI 因活动预算不足，从强锚点降级为候选替换备选："
            f"{', '.join(task.demoted_locked_pois)}"
        )

    if task.locked_pois:
        lines.append(
            "- ⛔ **必须包含**（违反 = DayPlan 无效，Orchestrator 会要求重做）："
            f"{', '.join(task.locked_pois)}"
        )
    if task.candidate_pois:
        if candidate_slots > 0:
            lines.append(
                f"- ✅ **候选池**（只用于补足剩余活动位，最多新增 {candidate_slots} 个，不要求用完）："
                f"{', '.join(task.candidate_pois)}"
            )
            lines.append(
                "  - 如果加入候选会超过活动数量预算，必须舍弃候选而不是追加活动"
            )
            lines.append("  - 如候选池中所有 POI 均不可行，才在同 area_cluster 内自行补充")
        else:
            lines.append(
                "- ✅ **候选池**（当前无新增名额，仅作 locked_pois 不可行时的替换备选；"
                "不得追加为新活动）："
                f"{', '.join(task.candidate_pois)}"
            )
    if task.forbidden_pois:
        lines.append(
            f"- 🚫 **禁止使用**（已被其他天锁定，违反 = 跨天 POI 重复，触发 Orchestrator 重新分配）："
            f"{', '.join(task.forbidden_pois)}"
        )
    if task.area_cluster:
        lines.append(f"- **当日区域**：{', '.join(task.area_cluster)}")

    env = task.mobility_envelope
    if env:
        max_hops = env.get("max_cross_area_hops", "不限")
        max_leg = env.get("max_transit_leg_min", "不限")
        lines.append(
            f"- **移动限制**: 最多跨 {max_hops} 个区域, "
            f"单段交通 ≤ {max_leg} 分钟"
        )

    if task.date_role == "arrival_day":
        lines.append("\n### 🛬 到达日约束")
        if task.arrival_time:
            lines.append(f"- 预计到达时间：{task.arrival_time}")
            lines.append(f"- 首活动开始时间不得早于 {task.arrival_time} + 2 小时")
        else:
            lines.append("- 首活动开始时间须留出至少 2 小时接驳缓冲")
        if task.arrival_transport:
            lines.append(
                f"- 到港交通："
                f"{_format_transport_segment(task.arrival_transport, '已锁定')}"
            )
        lines.append("- 建议首活动安排在住宿区域附近，降低接驳风险")
    elif task.date_role == "departure_day":
        lines.append("\n### 🛫 离开日约束")
        if task.departure_time:
            lines.append(f"- 预计出发时间：{task.departure_time}")
            lines.append(f"- 末活动结束时间不得晚于 {task.departure_time} 前 3 小时")
        else:
            lines.append("- 末活动结束时间须留出至少 3 小时前往交通枢纽")
        if task.departure_transport:
            lines.append(
                f"- 离港交通："
                f"{_format_transport_segment(task.departure_transport, '已锁定')}"
            )
        lines.append("- 建议末活动安排在交通枢纽附近")
    elif task.date_role == "arrival_departure_day":
        lines.append("\n### 🛬🛫 到达+离开日约束")
        if task.arrival_time:
            lines.append(f"- 预计到达时间：{task.arrival_time}")
            lines.append(f"- 首活动不得早于 {task.arrival_time} + 2 小时")
        else:
            lines.append("- 首活动须留出至少 2 小时接驳缓冲")
        if task.arrival_transport:
            lines.append(
                f"- 到港交通："
                f"{_format_transport_segment(task.arrival_transport, '已锁定')}"
            )
        if task.departure_time:
            lines.append(f"- 预计出发时间：{task.departure_time}")
            lines.append(f"- 末活动不得晚于 {task.departure_time} 前 3 小时")
        else:
            lines.append("- 末活动须留出至少 3 小时前往交通枢纽")
        if task.departure_transport:
            lines.append(
                f"- 离港交通："
                f"{_format_transport_segment(task.departure_transport, '已锁定')}"
            )
        lines.append("- 建议只安排住宿附近或交通枢纽附近的轻松活动")

    if task.fallback_slots:
        lines.append("\n### 备选方案")
        for slot in task.fallback_slots:
            target = slot.get("replace_if_unavailable", "?")
            alts = slot.get("alternatives", [])
            lines.append(f"- 如 {target} 不可行 → 替换为: {', '.join(alts)}")

    if task.repair_hints:
        lines.append("\n### ⚠️ 修复要求（上一轮校验发现的问题，**本轮必须逐一解决**）")
        for hint in task.repair_hints:
            lines.append(f"- **{hint}**")

    return "\n".join(lines)


def build_day_suffix(task: DayTask) -> str:
    """Build the per-day suffix that differs across workers."""
    parts = [f"\n---\n\n## 你的任务：第 {task.day} 天（{task.date}）\n"]

    sk = task.skeleton_slice
    parts.append("骨架安排：")
    if "area" in sk:
        parts.append(f"- 主区域：{sk['area']}")
    if "theme" in sk:
        parts.append(f"- 主题：{sk['theme']}")
    if "core_activities" in sk:
        activities = sk["core_activities"]
        if isinstance(activities, list):
            parts.append(f"- 方向性活动线索：{'、'.join(str(a) for a in activities)}")
        else:
            parts.append(f"- 方向性活动线索：{activities}")
        parts.append("  （线索仅供参考，具体 POI 由下方 locked_pois / candidate_pois 决定）")
    if "fatigue" in sk:
        parts.append(f"- 疲劳等级：{sk['fatigue']}")
    if "budget_level" in sk:
        parts.append(f"- 预算等级：{sk['budget_level']}")
    if task.day_budget is not None:
        parts.append(f"- 建议日预算：约 {task.day_budget} 元/天（仅供参考，硬性约束以总预算为准）")

    if task.day_constraints:
        parts.append("- 天级别约束：")
        for c in task.day_constraints:
            parts.append(f"  - [{c['type']}] {c['description']}")

    max_core = task.max_core_activities or max_core_activities_for_pace(task.pace)
    parts.append(
        f"\n节奏要求：{task.pace} → 本天 {core_activity_range_label(task.pace)} 个核心活动，"
        f"硬上限 {max_core} 个"
    )

    constraint_block = _build_constraint_block(task)
    if constraint_block:
        parts.append(constraint_block)

    parts.append(
        "\n请执行以上 DayTask。"
        "优先补齐核心 POI 的坐标和票价（开放时间不确定写 notes）；"
        "完成后调用 `submit_day_plan_candidate` 提交候选 DayPlan。"
    )

    return "\n".join(parts)


def split_skeleton_to_day_tasks(
    skeleton: dict[str, Any],
    plan: TravelPlanState,
) -> list[DayTask]:
    """Split a selected skeleton into per-day tasks."""
    from datetime import date as dt_date, timedelta

    days_data = skeleton.get("days", [])
    start = dt_date.fromisoformat(plan.dates.start) if plan.dates else None
    raw_pace = plan.trip_brief.get("pace") if plan.trip_brief else None
    pace = normalize_pace_value(raw_pace) or "balanced"

    tasks: list[DayTask] = []
    for i, day_skeleton in enumerate(days_data):
        day_num = i + 1
        if start:
            day_date = (start + timedelta(days=i)).isoformat()
        else:
            day_date = f"day-{day_num}"
        sk = day_skeleton if isinstance(day_skeleton, dict) else {}
        locked_pois = (
            sk.get("locked_pois", [])
            if isinstance(sk.get("locked_pois"), list)
            else []
        )
        max_core = max_core_activities_for_pace(pace)
        tasks.append(
            DayTask(
                day=day_num,
                date=day_date,
                skeleton_slice=sk,
                pace=pace,
                locked_pois=locked_pois,
                candidate_pois=sk.get("candidate_pois", []) if isinstance(sk.get("candidate_pois"), list) else [],
                area_cluster=sk.get("area_cluster", []) if isinstance(sk.get("area_cluster"), list) else [],
                mobility_envelope=sk.get("mobility_envelope", {}) if isinstance(sk.get("mobility_envelope"), dict) else {},
                fallback_slots=sk.get("fallback_slots", []) if isinstance(sk.get("fallback_slots"), list) else [],
                date_role=sk.get("date_role", "full_day") if isinstance(sk.get("date_role"), str) else "full_day",
                max_core_activities=max_core,
                candidate_activity_slots=max(max_core - len(locked_pois), 0),
            )
        )
    return tasks
