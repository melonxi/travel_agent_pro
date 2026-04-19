from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from memory.models import MemoryCandidate, MemoryItem, Rejection, UserMemory
from memory.v3_models import (
    MemoryProfileItem,
    SessionWorkingMemory,
    UserMemoryProfile,
    WorkingMemoryItem,
)


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


@dataclass
class V3ProfileUpdates:
    constraints: list[MemoryProfileItem] = field(default_factory=list)
    rejections: list[MemoryProfileItem] = field(default_factory=list)
    stable_preferences: list[MemoryProfileItem] = field(default_factory=list)
    preference_hypotheses: list[MemoryProfileItem] = field(default_factory=list)


@dataclass
class V3ExtractionResult:
    profile_updates: V3ProfileUpdates = field(default_factory=V3ProfileUpdates)
    working_memory: list[WorkingMemoryItem] = field(default_factory=list)
    episode_evidence: list[dict[str, Any]] = field(default_factory=list)
    state_observations: list[dict[str, Any]] = field(default_factory=list)
    drop: list[dict[str, Any]] = field(default_factory=list)


_V3_PROFILE_BUCKETS = (
    "constraints",
    "rejections",
    "stable_preferences",
    "preference_hypotheses",
)


def build_v3_extraction_prompt(
    user_messages: list[str],
    profile: UserMemoryProfile,
    working_memory: SessionWorkingMemory,
    plan_facts: dict[str, Any],
) -> str:
    messages_text = "\n".join(f"- {message}" for message in user_messages)
    profile_text = json.dumps(profile.to_dict(), ensure_ascii=False, indent=2)
    working_text = json.dumps(
        working_memory.to_dict(), ensure_ascii=False, indent=2
    )
    facts_text = json.dumps(plan_facts, ensure_ascii=False, indent=2)
    return f"""你在维护一个旅行助手的长期记忆系统（v3）。请从下列用户消息中提取不同类别的信号，并按严格 JSON 输出。

用户消息：
{messages_text}

当前解析出的本次行程事实（state_observations 的真相源）：
{facts_text}

已有长期画像：
{profile_text}

已有会话工作记忆：
{working_text}

**分类规则：**
- `profile_updates.constraints`：跨旅行的硬约束（例如「不坐红眼航班」「不住青旅」）。
- `profile_updates.rejections`：明确的拒绝项，要带 value。
- `profile_updates.stable_preferences`：多次观察到的稳定偏好。
- `profile_updates.preference_hypotheses`：**单次观察**得到的偏好假设，默认 status=pending。
- `working_memory`：**本次会话**内的临时信号，例如「先别考虑迪士尼」。要带 expires。
- `episode_evidence`：可以写入未来 episode 的证据对象。
- `state_observations`：**本次行程**的事实（destination/dates/budget/travelers/candidate_pool/skeleton/daily_plans）——它们属于 TravelPlanState，不是记忆，绝对不能进 profile_updates。
- `drop`：payment、membership、身份证号、护照号、手机号、邮箱、银行卡号等 PII 或敏感信息。

**硬性要求：**
- 本次目的地、日期、预算、旅客人数、候选池、骨架、每日计划 → 永远放 `state_observations`。
- 不要把临时信号写进 profile_updates；不要把长期偏好写进 working_memory。
- 不要推测用户没说过的偏好。
- 不要重复已有 profile 条目。
- 所有文本字段必须为中文简体字符串。

**输出 JSON（严格遵守 schema）：**
{{
  "profile_updates": {{
    "constraints": [MemoryProfileItem],
    "rejections": [MemoryProfileItem],
    "stable_preferences": [MemoryProfileItem],
    "preference_hypotheses": [MemoryProfileItem]
  }},
  "working_memory": [WorkingMemoryItem],
  "episode_evidence": [object],
  "state_observations": [object],
  "drop": [object]
}}

如果没有可提取内容，输出所有字段都是空数组的 JSON。"""


def parse_v3_extraction_response(response: str) -> V3ExtractionResult:
    text = (response or "").strip()
    if text.startswith("```"):
        try:
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        except IndexError:
            return V3ExtractionResult()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return V3ExtractionResult()
    if not isinstance(data, dict):
        return V3ExtractionResult()

    profile_raw = data.get("profile_updates")
    profile_raw = profile_raw if isinstance(profile_raw, dict) else {}
    profile_updates = V3ProfileUpdates(
        constraints=_parse_profile_bucket(profile_raw.get("constraints")),
        rejections=_parse_profile_bucket(profile_raw.get("rejections")),
        stable_preferences=_parse_profile_bucket(profile_raw.get("stable_preferences")),
        preference_hypotheses=_parse_profile_bucket(
            profile_raw.get("preference_hypotheses")
        ),
    )

    working_memory: list[WorkingMemoryItem] = []
    for raw in _as_list(data.get("working_memory")):
        if not isinstance(raw, dict):
            continue
        try:
            working_memory.append(WorkingMemoryItem.from_dict(raw))
        except (KeyError, TypeError, ValueError):
            continue

    return V3ExtractionResult(
        profile_updates=profile_updates,
        working_memory=working_memory,
        episode_evidence=[
            raw for raw in _as_list(data.get("episode_evidence")) if isinstance(raw, dict)
        ],
        state_observations=[
            raw
            for raw in _as_list(data.get("state_observations"))
            if isinstance(raw, dict)
        ],
        drop=[raw for raw in _as_list(data.get("drop")) if isinstance(raw, dict)],
    )


def _parse_profile_bucket(raw: Any) -> list[MemoryProfileItem]:
    items: list[MemoryProfileItem] = []
    for entry in _as_list(raw):
        if not isinstance(entry, dict):
            continue
        try:
            items.append(MemoryProfileItem.from_dict(entry))
        except (KeyError, TypeError, ValueError):
            continue
    return items


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []
