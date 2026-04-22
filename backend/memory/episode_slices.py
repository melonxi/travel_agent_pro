from __future__ import annotations

from typing import Any

from memory.v3_models import ArchivedTripEpisode, EpisodeSlice

_MAX_CONTENT_LEN = 180

_SLICE_META: dict[str, dict[str, list[str]]] = {
    "itinerary_pattern": {
        "domains": ["planning_style", "pace", "itinerary"],
        "keywords": ["骨架", "节奏", "路线", "区域", "天数"],
    },
    "stay_choice": {
        "domains": ["hotel", "accommodation"],
        "keywords": ["住宿", "酒店", "民宿", "区域"],
    },
    "transport_choice": {
        "domains": ["transport", "train", "flight"],
        "keywords": ["交通", "航班", "高铁", "火车", "到达"],
    },
    "budget_signal": {
        "domains": ["budget"],
        "keywords": ["预算", "花费", "成本", "分配"],
    },
    "rejected_option": {
        "domains": ["general"],
        "keywords": ["拒绝", "排除", "不要", "避开"],
    },
    "pitfall": {
        "domains": ["general", "pace"],
        "keywords": ["坑", "教训", "注意", "疲劳", "风险"],
    },
}


def build_episode_slices(episode: ArchivedTripEpisode, *, now: str) -> list[EpisodeSlice]:
    slices: list[EpisodeSlice] = []
    base_entities = _base_entities(episode)

    slices.append(
        _build_slice(
            episode=episode,
            now=now,
            slice_type="itinerary_pattern",
            index=1,
            content=_itinerary_pattern_content(episode),
            entities={
                **base_entities,
                "selected_skeleton": _entity_text(episode.selected_skeleton),
                "daily_plan_summary": _entity_text(episode.daily_plan_summary),
            },
            applicability="仅供行程结构参考；当前日期、体力和预算变化时需重排。",
        )
    )

    if episode.accommodation is not None:
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="stay_choice",
                index=1,
                content=_stay_choice_content(episode),
                entities={**base_entities, "accommodation": _entity_text(episode.accommodation)},
                applicability="仅供住宿偏好参考；库存和价格变化时需重新选择。",
            )
        )

    if episode.selected_transport is not None:
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="transport_choice",
                index=1,
                content=_transport_choice_content(episode),
                entities={
                    **base_entities,
                    "selected_transport": _entity_text(episode.selected_transport),
                },
                applicability="仅供交通方式参考；班次和出发条件变化时需重新判断。",
            )
        )

    if episode.budget is not None or episode.final_plan_summary:
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="budget_signal",
                index=1,
                content=_budget_signal_content(episode),
                entities={**base_entities, "budget": _entity_text(episode.budget)},
                applicability="仅供预算分配参考；当前价格变化时需重新计算。",
            )
        )

    rejected_entries = [
        item for item in _as_list_or_empty(episode.decision_log) if item.get("type") == "rejected"
    ]
    for index, rejected_item in enumerate(rejected_entries[:2], start=1):
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="rejected_option",
                index=index,
                content=_rejected_option_content(rejected_item),
                entities={
                    **base_entities,
                    "decision_category": _entity_text(rejected_item.get("category")),
                    "rejected_value": _entity_text(rejected_item.get("value")),
                },
                applicability="仅供避让相似选项；不代表所有同类选项都要排除。",
            )
        )

    for index, lesson in enumerate(_as_list_or_empty(episode.lesson_log)[:2], start=1):
        slices.append(
            _build_slice(
                episode=episode,
                now=now,
                slice_type="pitfall",
                index=index,
                content=_pitfall_content(lesson),
                entities={
                    **base_entities,
                    "lesson_kind": _entity_text(lesson.get("kind")),
                    "lesson": _entity_text(lesson),
                },
                applicability="仅供风险提醒；具体行程需结合当前节奏和体力。",
            )
        )

    return slices[:8]


def _build_slice(
    *,
    episode: ArchivedTripEpisode,
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


def _base_entities(episode: ArchivedTripEpisode) -> dict[str, Any]:
    return {
        "destination": _entity_text(episode.destination) or "",
        "trip_id": _entity_text(episode.trip_id),
        "session_id": _entity_text(episode.session_id),
    }


def _itinerary_pattern_content(episode: ArchivedTripEpisode) -> str:
    parts: list[str] = []
    skeleton = _render_value(episode.selected_skeleton)
    if skeleton:
        parts.append(f"行程骨架：{skeleton}")
    daily = _render_value(episode.daily_plan_summary)
    if daily:
        parts.append(f"每日节奏：{daily}")
    if not parts:
        parts.append("历史行程骨架。")
    return "；".join(parts)


def _stay_choice_content(episode: ArchivedTripEpisode) -> str:
    rendered = _render_value(episode.accommodation)
    return f"住宿选择：{rendered}" if rendered else "住宿选择。"


def _transport_choice_content(episode: ArchivedTripEpisode) -> str:
    rendered = _render_value(episode.selected_transport)
    return f"交通选择：{rendered}" if rendered else "交通选择。"


def _rejected_option_content(rejected_item: dict[str, Any]) -> str:
    category = _render_value(rejected_item.get("category"))
    value = _render_value(rejected_item.get("value"))
    reason = _render_value(rejected_item.get("reason"))
    parts = [part for part in [category, value, reason] if part]
    if not parts:
        return "已拒绝选项。"
    return f"已拒绝选项：{'；'.join(parts)}"


def _pitfall_content(lesson: dict[str, Any]) -> str:
    rendered = _render_value(lesson.get("content") or lesson)
    return f"教训：{rendered}" if rendered else "教训。"


def _budget_signal_content(episode: ArchivedTripEpisode) -> str:
    parts: list[str] = []
    budget = episode.budget or {}
    amount = budget.get("total", budget.get("amount")) if isinstance(budget, dict) else None
    currency = budget.get("currency") if isinstance(budget, dict) else None
    if amount is not None:
        amount_text = _render_value(amount)
        parts.append(f"预算：{amount_text} {currency}" if currency else f"预算：{amount_text}")
    elif budget:
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
