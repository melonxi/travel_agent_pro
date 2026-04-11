from __future__ import annotations

import json
from typing import Any

from memory.models import MemoryCandidate, MemoryItem, Rejection, UserMemory


_ALLOWED_CANDIDATE_DOMAINS = {
    "pace",
    "food",
    "hotel",
    "flight",
    "train",
    "budget",
    "family",
    "accessibility",
    "planning_style",
    "destination",
    "documents",
    "general",
}

_REQUIRED_CANDIDATE_FIELDS = {
    "type",
    "domain",
    "key",
    "value",
    "scope",
    "polarity",
    "confidence",
    "risk",
    "evidence",
    "reason",
}


def _coerce_attributes(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


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


def build_candidate_extraction_prompt(
    user_messages: list[str],
    existing_items: list[MemoryItem],
    plan_facts: dict[str, Any],
) -> str:
    messages_text = "\n".join(f"- {message}" for message in user_messages)
    items_text = json.dumps(
        [item.to_dict() for item in existing_items], ensure_ascii=False, indent=2
    )
    facts_text = json.dumps(plan_facts, ensure_ascii=False, indent=2)
    allowed_domains_text = ", ".join(sorted(_ALLOWED_CANDIDATE_DOMAINS))
    return f"""从以下信息中提取**候选 memory items**，用于后续审核与落库。

用户消息：
{messages_text}

当前解析出的本次行程事实：
{facts_text}

已有 memory items：
{items_text}

允许的 domain：
{allowed_domains_text}

规则：
- 本次目的地、日期、预算默认不是 global memory
- 只提取用户明确表达的内容，不推测
- 禁止提取 PII，包括身份证号、护照号、手机号、邮箱、银行卡号等
- 如果 domain 不在允许列表中，使用 general
- 不要重复已有 memory items

严格输出 JSON 数组，每个元素是一个候选对象。
如果没有候选，输出 []"""


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


def parse_candidate_extraction_response(response: str) -> list[MemoryCandidate]:
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(data, list):
        return []

    candidates: list[MemoryCandidate] = []
    for candidate_data in data:
        if not isinstance(candidate_data, dict):
            continue
        candidate_dict = dict(candidate_data)
        if any(field not in candidate_dict for field in _REQUIRED_CANDIDATE_FIELDS):
            continue
        raw_domain = str(candidate_dict.get("domain", "general"))
        if raw_domain not in _ALLOWED_CANDIDATE_DOMAINS:
            candidate_dict["domain"] = "general"
            attributes = _coerce_attributes(candidate_dict.get("attributes", {}))
            attributes["raw_domain"] = raw_domain
            candidate_dict["attributes"] = attributes
        try:
            candidates.append(MemoryCandidate.from_dict(candidate_dict))
        except (TypeError, ValueError, KeyError):
            continue
    return candidates


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
