from __future__ import annotations

from typing import Any

from memory.models import TripEpisode
from memory.v3_models import EpisodeSlice

_MAX_CONTENT_LEN = 180

_SLICE_META: dict[str, dict[str, list[str]]] = {
    "accepted_pattern": {
        "domains": ["planning_style", "pace"],
        "keywords": ["pattern", "骨架", "节奏", "方案"],
    },
    "rejected_option": {
        "domains": ["general"],
        "keywords": ["拒绝", "排除", "不要", "避开"],
    },
    "pitfall": {
        "domains": ["general", "pace"],
        "keywords": ["坑", "教训", "注意", "疲劳"],
    },
    "budget_signal": {
        "domains": ["budget"],
        "keywords": ["预算", "花费", "成本", "分配"],
    },
}


def build_episode_slices(episode: TripEpisode, *, now: str) -> list[EpisodeSlice]:
    accepted_items = _as_list_or_empty(episode.accepted_items)
    rejected_items = _as_list_or_empty(episode.rejected_items)
    lessons = _as_list_or_empty(episode.lessons)
    slices: list[EpisodeSlice] = []
    base_entities = _base_entities(episode)

    if episode.selected_skeleton is not None:
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="accepted_pattern",
                index=1,
                content=_accepted_pattern_content(episode, accepted_items),
                entities={
                    **base_entities,
                    "selected_skeleton": _entity_text(episode.selected_skeleton),
                    "accepted_items_count": len(accepted_items),
                },
                applicability=(
                    "仅供规划骨架参考；当前预算、同行人或时间变化时不能直接套用。"
                ),
            )
        )

    for index, rejected_item in enumerate(rejected_items[:2], start=1):
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="rejected_option",
                index=index,
                content=_rejected_option_content(rejected_item),
                entities={
                    **base_entities,
                    "rejected_item": _entity_text(rejected_item),
                    "rejected_index": index,
                },
                applicability="仅供避让相似选项；不代表所有同类选项都要排除。",
            )
        )

    for index, lesson in enumerate(lessons[:2], start=1):
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="pitfall",
                index=index,
                content=_pitfall_content(lesson),
                entities={
                    **base_entities,
                    "lesson": _entity_text(lesson),
                    "lesson_index": index,
                },
                applicability="仅供风险提醒；具体行程需结合当前节奏和体力。",
            )
        )

    if episode.budget is not None:
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="budget_signal",
                index=1,
                content=_budget_signal_content(episode),
                entities={
                    **base_entities,
                    "budget": _entity_text(episode.budget),
                    "budget_present": True,
                },
                applicability="仅供预算分配参考；当前预算或物价变化时需重新计算。",
            )
        )

    return slices[:8]


def _build_slice(
    *,
    episode: TripEpisode,
    now: str,
    slice_type: str,
    index: int,
    content: str,
    entities: dict[str, Any],
    applicability: str,
) -> EpisodeSlice:
    meta = _SLICE_META[slice_type]
    return EpisodeSlice(
        id=f"slice_{episode.id}_{slice_type}_{index:02d}",
        user_id=episode.user_id,
        source_episode_id=episode.id,
        source_trip_id=episode.trip_id,
        slice_type=slice_type,
        domains=list(meta["domains"]),
        entities=entities,
        keywords=list(meta["keywords"]),
        content=_truncate(content),
        applicability=applicability,
        created_at=now,
    )


def _base_entities(episode: TripEpisode) -> dict[str, Any]:
    return {
        "destination": episode.destination or "",
        "trip_id": episode.trip_id,
        "session_id": episode.session_id,
    }


def _accepted_pattern_content(
    episode: TripEpisode, accepted_items: list[Any]
) -> str:
    parts: list[str] = []
    skeleton = episode.selected_skeleton
    if skeleton:
        rendered_skeleton = _render_value(skeleton)
        if rendered_skeleton:
            parts.append(f"已选骨架：{rendered_skeleton}")
    if accepted_items:
        rendered_items = "；".join(
            rendered
            for rendered in (_render_value(item) for item in accepted_items[:2])
            if rendered
        )
        if rendered_items:
            parts.append(f"接受项：{rendered_items}")
    if not parts:
        parts.append("已选骨架。")
    return "；".join(parts)


def _rejected_option_content(rejected_item: Any) -> str:
    rendered = _render_value(rejected_item)
    if rendered:
        return f"已拒绝选项：{rendered}"
    return "已拒绝选项。"


def _pitfall_content(lesson: Any) -> str:
    rendered = _render_value(lesson)
    if rendered:
        return f"教训：{rendered}"
    return "教训。"


def _budget_signal_content(episode: TripEpisode) -> str:
    parts: list[str] = []
    budget = episode.budget or {}
    amount = budget.get("amount", budget.get("total"))
    currency = budget.get("currency")
    if amount is not None:
        amount_text = _render_value(amount)
        if currency:
            parts.append(f"预算：{amount_text} {currency}")
        else:
            parts.append(f"预算：{amount_text}")
    else:
        parts.append(f"预算：{_render_value(budget)}")
    summary = _render_value(episode.final_plan_summary)
    if summary:
        parts.append(f"总结：{summary}")
    return "；".join(parts)


def _render_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        parts: list[str] = []
        for key in sorted(value):
            rendered = _render_value(value[key])
            if rendered:
                parts.append(f"{_sanitize_text(str(key))}={rendered}")
        return _truncate("；".join(parts))
    if isinstance(value, (list, tuple)):
        parts = [_render_value(item) for item in value]
        parts = [part for part in parts if part]
        return _truncate("、".join(parts))
    return _truncate(_sanitize_text(str(value)))


def _entity_text(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return _truncate(_render_value(value))
    return _truncate(_render_value(value))


def _as_list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _sanitize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = " ".join(part.strip() for part in text.splitlines() if part.strip())
    return " ".join(text.split()).strip()


def _truncate(text: str) -> str:
    if len(text) <= _MAX_CONTENT_LEN:
        return text
    return text[:_MAX_CONTENT_LEN]
