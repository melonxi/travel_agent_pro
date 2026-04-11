from __future__ import annotations

import json
from typing import Any

from memory.models import Rejection, UserMemory


def build_extraction_prompt(
    user_messages: list[str],
    existing_memory: UserMemory,
) -> str:
    messages_text = "\n".join(f"- {message}" for message in user_messages)
    memory_text = json.dumps(existing_memory.to_dict(), ensure_ascii=False, indent=2)
    return f"""从以下用户消息中提取**持久化个人偏好**（适用于未来任何旅行，不限于本次）。

用户消息：
{messages_text}

已有记忆：
{memory_text}

提取规则：
- 只提取用户明确表达的偏好，不推测
- 排除本次旅行专属信息（具体目的地、具体日期、本次预算）
- 适合提取：饮食禁忌、住宿星级/类型偏好、飞行座位偏好、节奏偏好、带小孩/老人的常态
- 不适合提取："这次想去京都""预算3万""4月15号出发"
- 已有记忆中已包含的不要重复输出

严格输出 JSON：
{{"preferences": {{"key": "value"}}, "rejections": [{{"item": "...", "reason": "...", "permanent": true}}]}}
如果没有可提取的内容，输出 {{"preferences": {{}}, "rejections": []}}"""


def parse_extraction_response(response: str) -> tuple[dict[str, Any], list[dict]]:
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}, []

    preferences = data.get("preferences", {})
    rejections = data.get("rejections", [])
    if not isinstance(preferences, dict):
        preferences = {}
    if not isinstance(rejections, list):
        rejections = []
    return preferences, rejections


class MemoryMerger:
    def merge(
        self,
        existing: UserMemory,
        preferences: dict[str, Any],
        rejections: list[dict],
    ) -> UserMemory:
        for key, value in preferences.items():
            existing.explicit_preferences[key] = value

        existing_items = {rejection.item for rejection in existing.rejections}
        for rejection in rejections:
            item = rejection.get("item", "")
            if item and item not in existing_items:
                existing.rejections.append(
                    Rejection(
                        item=item,
                        reason=rejection.get("reason", ""),
                        permanent=rejection.get("permanent", False),
                    )
                )
                existing_items.add(item)

        return existing
