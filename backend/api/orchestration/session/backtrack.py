from __future__ import annotations

import uuid

from state.models import TravelPlanState

BACKTRACK_PATTERNS: dict[int, list[str]] = {
    1: [
        "重新开始",
        "从头来",
        "换个需求",
        "换个目的地",
        "不想去这里",
        "不去了",
        "换地方",
    ],
    3: ["改日期", "换时间", "日期不对", "换住宿", "不住这", "换个区域"],
}

TRIP_RESET_PATTERNS = tuple(BACKTRACK_PATTERNS[1]) + (
    "换目的地",
    "改目的地",
    "新行程",
    "重新规划",
)


def detect_backtrack(message: str, plan: TravelPlanState) -> int | None:
    for target_phase, patterns in BACKTRACK_PATTERNS.items():
        if target_phase >= plan.phase:
            continue
        if any(pattern in message for pattern in patterns):
            return target_phase
    return None


def is_new_trip_backtrack(to_phase: int, reason_text: str) -> bool:
    return to_phase == 1 and any(
        pattern in reason_text for pattern in TRIP_RESET_PATTERNS
    )


async def rotate_trip_on_reset_backtrack(
    *,
    user_id: str,
    plan: TravelPlanState,
    to_phase: int,
    reason_text: str,
) -> bool:
    if not is_new_trip_backtrack(to_phase, reason_text):
        return False
    plan.trip_id = f"trip_{uuid.uuid4().hex[:12]}"
    del user_id
    return True
