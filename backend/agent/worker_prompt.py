# backend/agent/worker_prompt.py
"""Worker system prompt templates for Phase 5 parallel mode.

Design goal: maximize shared prefix across all Day Workers to achieve
high KV-Cache hit rates (Manus / Claude Code fork sub-agent pattern).
"""

from __future__ import annotations

from dataclasses import dataclass
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
- 餐饮可作为活动（category="food"），安排在合理时段。"""

_DAYPLAN_SCHEMA = """## DayPlan 严格 JSON 结构

完成规划后，你的**最后一条消息**必须包含一个 JSON 代码块，格式如下：

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
- start_time / end_time 必须是 "HH:MM" 格式
- cost 是数字（人民币），没有时填 0
- category 必须提供（shrine, museum, food, transport, activity, shopping, park 等）"""


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
    parts.append(
        "\n请为这一天生成完整的 DayPlan JSON。"
        "先用工具补齐信息和优化路线，最后输出 JSON。"
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
        tasks.append(
            DayTask(
                day=day_num,
                date=day_date,
                skeleton_slice=day_skeleton if isinstance(day_skeleton, dict) else {},
                pace=pace,
            )
        )
    return tasks
