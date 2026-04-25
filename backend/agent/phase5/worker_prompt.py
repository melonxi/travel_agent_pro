# backend/agent/worker_prompt.py
"""Worker system prompt templates for Phase 5 parallel mode.

Design goal: maximize shared prefix across all Day Workers to achieve
high KV-Cache hit rates (Manus / Claude Code fork sub-agent pattern).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from state.models import TravelPlanState


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


_SOUL_PATH = Path(__file__).resolve().parent.parent / "context" / "soul.md"

_WORKER_ROLE = """## 角色

你是单日行程落地规划师。你的任务是为指定的一天生成完整的可执行 DayPlan。

## 硬法则

- 严格基于骨架安排展开，不要偷偷替换区域或主题。
- 区域连续性优先于景点密度——同一天的活动应在地理上聚拢。
- 时间安排必须留出现实缓冲（交通延误、排队、休息），不要把活动首尾无缝拼死。
- 用 get_poi_info 补齐缺失的坐标、票价、开放时间。
- 用 optimize_day_route 优化活动顺序。
- 用 calculate_route 验证关键移动是否可行。
- 餐饮可作为活动（category="food"），安排在合理时段。

## 工具回退策略

- 当专项工具返回无效信息时，可以进行有限次补救，但不要围绕同一 POI 或同一问题无限搜索。
- 如果已经具备区域、主题、核心活动和基本时间结构，应优先输出保守版 DayPlan。
- 当工具仍无法补齐细节时，可以基于骨架、区域连续性和常识性节奏完成保守安排。
- 不得编造具体营业时间、具体票价、明确预约要求；无法确认的事实写入 notes。
- 当系统提示进入收口模式时，必须停止继续调工具并直接提交 DayPlan。

## 交付方式

你完成单日规划后，必须优先调用 `submit_day_plan_candidate` 工具提交 DayPlan。

提交成功后，只输出一句简短确认，例如："已提交第 2 天计划。"

不要在自然语言正文里重复粘贴完整 JSON，除非系统明确提示提交工具不可用。

如果 `submit_day_plan_candidate` 返回错误：
- 根据错误信息修正 DayPlan 后再次提交。
- 如果错误说明 day 不匹配，必须把 day 改为当前任务天数。
- 如果错误说明字段缺失，必须补齐字段。
- 最多修正 1 次；仍失败时，再在最终文本中输出合法 DayPlan JSON 作为兜底。

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
  "notes": "<当天补充说明>",
  "activities": [
    {
      "name": "<活动名称>",
      "location": {"name": "<地点名>", "lat": <纬度>, "lng": <经度>},
      "start_time": "HH:MM",
      "end_time": "HH:MM",
      "category": "<类别>",
      "cost": <人民币数字>,
      "transport_from_prev": "<从上一地点的交通方式>",
      "transport_duration_min": <分钟数>,
      "notes": "<可选备注>"
    }
  ]
}
```

硬约束：
- location 必须是 dict（含 name, lat, lng），不能是字符串
- start_time / end_time 必须是 "HH:MM" 格式，且 end_time > start_time
- cost 是数字（人民币），没有时填 0；不能是字符串如 "100元"
- category 必须是以下枚举之一：shrine, museum, food, transport, activity, shopping, park, viewpoint, experience

常见结构错误（绝对不允许）：
1. `"location": "浅草寺"` → 必须是 `{"name": "浅草寺", "lat": 35.7148, "lng": 139.7967}`
2. `"cost": "100元"` → 必须是数字 `100`
3. `"start_time": "09:00", "end_time": "09:00"` → end_time 必须晚于 start_time
4. `"category": "景点"` → 必须使用枚举值（如 shrine, museum, park 等）

完整字段定义和约束请以 `submit_day_plan_candidate` 工具的参数 schema 为准。"""


def _load_soul() -> str:
    if _SOUL_PATH.exists():
        return _SOUL_PATH.read_text(encoding="utf-8")
    return "你是一个旅行规划 Agent。"


def build_shared_prefix(plan: TravelPlanState) -> str:
    """Build the shared prefix for all Day Workers.

    This prefix is identical across all workers to maximize KV-Cache hit rate.
    Do NOT include any per-day information here.
    """
    parts = [_load_soul()]

    # 旅行上下文（只读）
    parts.append("\n---\n\n## 旅行上下文\n")
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
        parts.append("- 旅行画像：")
        for key, val in plan.trip_brief.items():
            if key in ("dates", "total_days"):
                continue
            parts.append(f"  - {key}: {val}")
    if plan.accommodation:
        parts.append(f"- 住宿区域：{plan.accommodation.area}")
        if plan.accommodation.hotel:
            parts.append(f"- 住宿酒店：{plan.accommodation.hotel}")
    if plan.budget:
        parts.append(f"- 总预算：{plan.budget.total} {plan.budget.currency}")
    if plan.preferences:
        pref_strs = [f"{p.key}: {p.value}" for p in plan.preferences if p.key]
        if pref_strs:
            parts.append(f"- 用户偏好：{'; '.join(pref_strs)}")
    if plan.constraints:
        cons_strs = [f"[{c.type}] {c.description}" for c in plan.constraints]
        if cons_strs:
            parts.append(f"- 用户约束：{'; '.join(cons_strs)}")

    # 角色和规则
    parts.append("\n---\n")
    parts.append(_WORKER_ROLE)
    parts.append("\n---\n")
    parts.append(_DAYPLAN_SCHEMA)

    return "\n".join(parts)


def _build_constraint_block(task: DayTask) -> str:
    lines: list[str] = []
    has_constraints = (
        task.locked_pois or task.candidate_pois or task.forbidden_pois
        or task.area_cluster or task.mobility_envelope
        or task.date_role != "full_day" or task.fallback_slots or task.repair_hints
    )
    if not has_constraints:
        return ""

    lines.append("\n## 硬约束（必须遵守）\n")

    if task.locked_pois:
        lines.append(f"- **必须包含的活动**: {', '.join(task.locked_pois)}")
    if task.candidate_pois:
        lines.append(f"- **允许使用的候选池**: {', '.join(task.candidate_pois)}")
        lines.append("- 优先从候选池选取，如需额外补充须在同 area_cluster 内")
    if task.forbidden_pois:
        lines.append(f"- **禁止使用（已分配给其他天）**: {', '.join(task.forbidden_pois)}")
    if task.area_cluster:
        lines.append(f"- **当日区域**: {', '.join(task.area_cluster)}")

    env = task.mobility_envelope
    if env:
        max_hops = env.get("max_cross_area_hops", "不限")
        max_leg = env.get("max_transit_leg_min", "不限")
        lines.append(f"- **移动限制**: 最多跨 {max_hops} 个区域, 单段交通 ≤ {max_leg} 分钟")

    if task.date_role == "arrival_day":
        lines.append("- **到达日**: 注意大交通到达时间，首活动须留足接驳缓冲")
    elif task.date_role == "departure_day":
        lines.append("- **离开日**: 注意大交通离开时间，末活动须留足前往交通枢纽的时间")

    if task.fallback_slots:
        lines.append("\n### 备选方案")
        for slot in task.fallback_slots:
            target = slot.get("replace_if_unavailable", "?")
            alts = slot.get("alternatives", [])
            lines.append(f"- 如 {target} 不可行 → 替换为: {', '.join(alts)}")

    if task.repair_hints:
        lines.append("\n### ⚠️ 修复要求（上一轮校验发现的问题）")
        for hint in task.repair_hints:
            lines.append(f"- {hint}")

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
            parts.append(f"- 核心活动：{'、'.join(str(a) for a in activities)}")
        else:
            parts.append(f"- 核心活动：{activities}")
    if "fatigue" in sk:
        parts.append(f"- 疲劳等级：{sk['fatigue']}")
    if "budget_level" in sk:
        parts.append(f"- 预算等级：{sk['budget_level']}")

    # 节奏 → 活动数量范围
    pace = task.pace
    if pace == "relaxed":
        count_range = "2-3"
    elif pace == "intensive":
        count_range = "4-5"
    else:
        count_range = "3-4"
    parts.append(f"\n节奏要求：{pace} → 本天 {count_range} 个核心活动")

    constraint_block = _build_constraint_block(task)
    if constraint_block:
        parts.append(constraint_block)

    parts.append(
        "\n请执行以上 DayTask，为这一天生成完整 DayPlan。"
        "先用只读工具补齐信息和优化路线；"
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
    pace = plan.trip_brief.get("pace", "balanced") if plan.trip_brief else "balanced"

    tasks: list[DayTask] = []
    for i, day_skeleton in enumerate(days_data):
        day_num = i + 1
        if start:
            day_date = (start + timedelta(days=i)).isoformat()
        else:
            day_date = f"day-{day_num}"
        sk = day_skeleton if isinstance(day_skeleton, dict) else {}
        tasks.append(
            DayTask(
                day=day_num,
                date=day_date,
                skeleton_slice=sk,
                pace=pace,
                locked_pois=sk.get("locked_pois", []) if isinstance(sk.get("locked_pois"), list) else [],
                candidate_pois=sk.get("candidate_pois", []) if isinstance(sk.get("candidate_pois"), list) else [],
                area_cluster=sk.get("area_cluster", []) if isinstance(sk.get("area_cluster"), list) else [],
                mobility_envelope=sk.get("mobility_envelope", {}) if isinstance(sk.get("mobility_envelope"), dict) else {},
                fallback_slots=sk.get("fallback_slots", []) if isinstance(sk.get("fallback_slots"), list) else [],
                date_role=sk.get("date_role", "full_day") if isinstance(sk.get("date_role"), str) else "full_day",
            )
        )
    return tasks
