from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from memory.v3_models import (
    MemoryProfileItem,
    SessionWorkingMemory,
    UserMemoryProfile,
    WorkingMemoryItem,
)


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


@dataclass
class V3ExtractionRoutes:
    profile: bool = False
    working_memory: bool = False

    @property
    def any(self) -> bool:
        return self.profile or self.working_memory


@dataclass
class V3ExtractionGateResult:
    should_extract: bool = False
    routes: V3ExtractionRoutes = field(default_factory=V3ExtractionRoutes)
    reason: str = ""
    message: str = ""


_V3_PROFILE_BUCKETS = (
    "constraints",
    "rejections",
    "stable_preferences",
    "preference_hypotheses",
)

_V3_EXTRACTION_TOOL_NAME = "extract_memory_candidates"
_V3_EXTRACTION_GATE_TOOL_NAME = "decide_memory_extraction"
_V3_PROFILE_EXTRACTION_TOOL_NAME = "extract_profile_memory"
_V3_WORKING_MEMORY_EXTRACTION_TOOL_NAME = "extract_working_memory"

_V3_PROFILE_DOMAINS = [
    "pace",
    "food",
    "hotel",
    "accommodation",
    "flight",
    "train",
    "budget",
    "family",
    "accessibility",
    "planning_style",
    "documents",
    "general",
]

_V3_WORKING_MEMORY_DOMAINS = sorted(
    {*
        _V3_PROFILE_DOMAINS,
        "attraction",
        "transport",
    }
)

_V3_POLARITIES = ["prefer", "avoid", "neutral"]
_V3_STABILITIES = [
    "explicit_declared",
    "pattern_observed",
    "hard_constraint",
    "soft_constraint",
]
_V3_WORKING_KINDS = [
    "temporary_preference",
    "temporary_rejection",
    "decision_hint",
    "open_question",
    "watchout",
    "note",
]


def _build_profile_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "enum": _V3_PROFILE_DOMAINS,
                "description": "长期画像所属领域。只能使用枚举值。",
            },
            "key": {
                "type": "string",
                "description": "稳定字段名，使用 snake_case，例如 avoid_red_eye、avoid_spicy。",
            },
            "value": {
                "description": "偏好或约束的值。可为 string / boolean / number / object。",
            },
            "applicability": {
                "type": "string",
                "description": "这条画像在什么场景下适用，必须是可复用的长期适用范围说明。",
            },
            "recall_hints": {
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "用于检索该画像的领域关键词数组。",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "用于检索该画像的主题关键词数组。",
                    },
                    "aliases": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "用户可能用来指代该画像的同义说法数组。",
                    },
                },
                "required": ["domains", "keywords", "aliases"],
                "additionalProperties": False,
                "description": "便于后续召回的检索提示元数据。",
            },
            "source_refs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "description": "来源类型，例如 message 或 prior_memory。",
                        },
                        "session_id": {
                            "type": "string",
                            "description": "来源会话标识。",
                        },
                        "quote": {
                            "type": "string",
                            "description": "支撑这条画像的原始引用，当前轮必须来自用户当前消息，且不得包含敏感信息。",
                        },
                    },
                    "required": ["kind", "session_id", "quote"],
                    "additionalProperties": False,
                },
                "description": "支持该画像的来源引用数组。",
            },
            "polarity": {
                "type": "string",
                "enum": _V3_POLARITIES,
                "description": "用户是偏好、规避还是中性事实。",
            },
            "stability": {
                "type": "string",
                "enum": _V3_STABILITIES,
                "description": "稳定性标签。单次观察长期偏好请放 preference_hypotheses，而不是伪装成稳定偏好。",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "0 到 1 的置信度。",
            },
            "reason": {
                "type": "string",
                "description": "简短说明为什么值得写入记忆。",
            },
            "evidence": {
                "type": "string",
                "description": "尽量贴近用户原话的证据短句。",
            },
        },
        "required": [
            "domain",
            "key",
            "value",
            "applicability",
            "recall_hints",
            "source_refs",
            "polarity",
            "stability",
            "confidence",
            "reason",
            "evidence",
        ],
        "additionalProperties": False,
    }


def _build_working_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "phase": {
                "type": "integer",
                "description": "该临时信号产生于哪个阶段，例如 1 / 3 / 5 / 7。",
            },
            "kind": {
                "type": "string",
                "enum": _V3_WORKING_KINDS,
                "description": "会话内临时记忆的类型。",
            },
            "domains": {
                "type": "array",
                "items": {"type": "string", "enum": _V3_WORKING_MEMORY_DOMAINS},
                "description": "相关领域，至少一个。",
            },
            "content": {
                "type": "string",
                "description": "会话内短期记忆内容，例如“这轮先别考虑迪士尼”。",
            },
            "reason": {
                "type": "string",
                "description": "为什么需要在当前会话保留这个临时信号。",
            },
            "status": {
                "type": "string",
                "enum": ["active"],
                "description": "当前仅允许 active。",
            },
            "expires": {
                "type": "object",
                "properties": {
                    "on_session_end": {"type": "boolean"},
                    "on_trip_change": {"type": "boolean"},
                    "on_phase_exit": {"type": "boolean"},
                },
                "required": [
                    "on_session_end",
                    "on_trip_change",
                    "on_phase_exit",
                ],
                "additionalProperties": False,
            },
        },
        "required": [
            "phase",
            "kind",
            "domains",
            "content",
            "reason",
            "status",
            "expires",
        ],
        "additionalProperties": False,
    }


def build_v3_extraction_tool() -> dict[str, Any]:
    profile_item_schema = _build_profile_item_schema()
    working_item_schema = _build_working_item_schema()
    return {
        "name": _V3_EXTRACTION_TOOL_NAME,
        "description": (
            "只提取两类可写入结果：长期画像 profile_updates 与会话工作记忆 "
            "working_memory。当前行程事实、PII、以及与本轮无关的推测都不要输出。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profile_updates": {
                    "type": "object",
                    "properties": {
                        bucket: {"type": "array", "items": profile_item_schema}
                        for bucket in _V3_PROFILE_BUCKETS
                    },
                    "required": list(_V3_PROFILE_BUCKETS),
                    "additionalProperties": False,
                },
                "working_memory": {"type": "array", "items": working_item_schema},
            },
            "required": ["profile_updates", "working_memory"],
            "additionalProperties": False,
        },
    }


def build_v3_profile_extraction_tool() -> dict[str, Any]:
    profile_item_schema = _build_profile_item_schema()
    return {
        "name": _V3_PROFILE_EXTRACTION_TOOL_NAME,
        "description": (
            "只提取跨旅行长期用户画像 profile_updates。当前行程事实、"
            "会话临时信号、PII、以及推测内容都不要输出。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profile_updates": {
                    "type": "object",
                    "properties": {
                        bucket: {"type": "array", "items": profile_item_schema}
                        for bucket in _V3_PROFILE_BUCKETS
                    },
                    "required": list(_V3_PROFILE_BUCKETS),
                    "additionalProperties": False,
                },
            },
            "required": ["profile_updates"],
            "additionalProperties": False,
        },
    }


def build_v3_working_memory_extraction_tool() -> dict[str, Any]:
    working_item_schema = _build_working_item_schema()
    return {
        "name": _V3_WORKING_MEMORY_EXTRACTION_TOOL_NAME,
        "description": (
            "只提取当前 session/trip 内短期有用的 working_memory。"
            "长期画像、当前行程权威事实、PII、以及推测内容都不要输出。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "working_memory": {"type": "array", "items": working_item_schema},
            },
            "required": ["working_memory"],
            "additionalProperties": False,
        },
    }


def build_v3_extraction_gate_tool() -> dict[str, Any]:
    return {
        "name": _V3_EXTRACTION_GATE_TOOL_NAME,
        "description": (
            "判断当前这一轮对话是否值得继续执行较重的记忆提取。"
            "只根据当前轮上下文判断 should_extract 和 routes，不要产出具体记忆条目。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "should_extract": {
                    "type": "boolean",
                    "description": "当前轮是否值得继续执行正式记忆提取。",
                },
                "routes": {
                    "type": "object",
                    "properties": {
                        "profile": {
                            "type": "boolean",
                            "description": "是否需要执行长期画像 profile extraction。",
                        },
                        "working_memory": {
                            "type": "boolean",
                            "description": "是否需要执行当前会话 working memory extraction。",
                        },
                    },
                    "required": ["profile", "working_memory"],
                    "additionalProperties": False,
                },
                "reason": {
                    "type": "string",
                    "description": "稳定英文/蛇形标识，例如 explicit_preference_signal、trip_state_only。",
                },
                "message": {
                    "type": "string",
                    "description": "给前端展示的简短中文说明。",
                },
            },
            "required": ["should_extract", "routes", "reason", "message"],
            "additionalProperties": False,
        },
    }


def v3_extraction_tool_name() -> str:
    return _V3_EXTRACTION_TOOL_NAME


def v3_profile_extraction_tool_name() -> str:
    return _V3_PROFILE_EXTRACTION_TOOL_NAME


def v3_working_memory_extraction_tool_name() -> str:
    return _V3_WORKING_MEMORY_EXTRACTION_TOOL_NAME


def v3_extraction_gate_tool_name() -> str:
    return _V3_EXTRACTION_GATE_TOOL_NAME


def parse_v3_extraction_tool_arguments(arguments: dict[str, Any] | None) -> V3ExtractionResult:
    if not isinstance(arguments, dict):
        return V3ExtractionResult()
    return parse_v3_extraction_response(json.dumps(arguments, ensure_ascii=False))


def parse_v3_extraction_gate_tool_arguments(
    arguments: dict[str, Any] | None,
) -> V3ExtractionGateResult:
    if not isinstance(arguments, dict):
        return V3ExtractionGateResult()
    routes_raw = arguments.get("routes")
    if not isinstance(routes_raw, dict):
        return V3ExtractionGateResult(
            should_extract=False,
            routes=V3ExtractionRoutes(),
            reason="invalid_route_payload",
            message="",
        )
    routes = V3ExtractionRoutes(
        profile=bool(routes_raw.get("profile", False)),
        working_memory=bool(routes_raw.get("working_memory", False)),
    )
    if not bool(arguments.get("should_extract", False)):
        routes = V3ExtractionRoutes()
    reason = str(arguments.get("reason", "") or "").strip()
    message = str(arguments.get("message", "") or "").strip()
    return V3ExtractionGateResult(
        should_extract=routes.any,
        routes=routes,
        reason=reason,
        message=message,
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
    return f"""你在执行一个内部记忆提取任务。你的目标只有两类输出：

1. `profile_updates`
长期可复用的用户画像，包含：
- `constraints`：跨旅行硬约束，例如“不坐红眼航班”“不住青旅”
- `rejections`：明确拒绝的对象
- `stable_preferences`：已经足够稳定、值得长期保留的偏好
- `preference_hypotheses`：只有单次观察、暂时不够稳定的偏好假设

2. `working_memory`
只对当前会话/当前 trip 暂时有用的短期信号，例如“这轮先别考虑迪士尼”

请调用工具 `{_V3_EXTRACTION_TOOL_NAME}` 提交结果，不要输出 JSON 正文、不要输出解释文字、不要输出代码块。

用户消息：
{messages_text}

当前解析出的本次行程事实：
{facts_text}

已有长期画像：
{profile_text}

已有会话工作记忆：
{working_text}

分类规则：
- `profile_updates.constraints`：跨旅行的硬约束（例如「不坐红眼航班」「不住青旅」）。
- `profile_updates.rejections`：明确的拒绝项，要带 value。
- `profile_updates.stable_preferences`：多次观察到的稳定偏好。
- `profile_updates.preference_hypotheses`：单次观察得到的偏好假设。
- `working_memory`：本次会话内的临时信号，例如「先别考虑迪士尼」。要带 expires。

硬性要求：
- 本次目的地、日期、预算、旅客人数、候选池、骨架、每日计划都属于当前 trip state，不要输出到任何字段。
- 支付信息、会员信息、身份证号、护照号、手机号、邮箱、银行卡号等敏感信息直接忽略，不要输出。
- 不要把临时信号写进 profile_updates；不要把长期偏好写进 working_memory。
- 不要推测用户没说过的偏好。
- 不要重复已有 profile 条目或 working memory 条目。
- `domain`、`polarity`、`stability`、`kind`、`status` 必须严格遵守工具 schema 的枚举。
- `key` 使用稳定的 snake_case；`reason` 和 `evidence` 可以是简洁中文说明，不强制所有文本字段都写成中文。

示例：
- 用户说“我不坐红眼航班” -> 放进 `profile_updates.constraints`
- 用户说“我不吃辣”且这是第一次观察 -> 优先放进 `profile_updates.preference_hypotheses`
- 用户说“这轮先别考虑迪士尼” -> 放进 `working_memory`
- 用户说“这次预算 3 万、五一去京都” -> 这是当前行程事实，不要输出

如果没有可提取内容，就调用工具并传入空数组。"""


def build_v3_profile_extraction_prompt(
    user_messages: list[str],
    profile: UserMemoryProfile,
    plan_facts: dict[str, Any],
) -> str:
    messages_text = "\n".join(f"- {message}" for message in user_messages)
    profile_text = json.dumps(profile.to_dict(), ensure_ascii=False, indent=2)
    facts_text = json.dumps(plan_facts, ensure_ascii=False, indent=2)
    return f"""你在执行一个内部长期画像提取任务。你的目标只有 `profile_updates`。

请调用工具 `{_V3_PROFILE_EXTRACTION_TOOL_NAME}` 提交结果，不要输出 JSON 正文、不要输出解释文字、不要输出代码块。

用户消息：
{messages_text}

当前解析出的本次行程事实：
{facts_text}

已有长期画像：
{profile_text}

分类规则：
- `constraints`：跨旅行硬约束，例如“不坐红眼航班”“不住青旅”。
- `rejections`：明确拒绝的对象，要带 value。
- `stable_preferences`：多次观察到，或用户明确声明为长期成立的稳定偏好。
- `preference_hypotheses`：单次观察得到的偏好假设。
- 每条 profile item 都必须补齐 `applicability`、`recall_hints.domains`、`recall_hints.keywords`、`recall_hints.aliases`、`source_refs`。
- `source_refs` 至少要包含一条来自当前轮用户消息的 `quote`，并且 `quote` 里不能包含手机号、邮箱、护照号、身份证号、银行卡号等敏感信息。
- `recall_hints.domains/keywords/aliases` 都必须是字符串数组，用于后续召回。

硬性要求：
- 本次目的地、日期、预算、旅客人数、候选池、骨架、每日计划都属于当前 trip state，不要输出。
- 当前会话临时信号不要输出；这些由会话工作记忆提取器处理。
- 支付信息、会员信息、身份证号、护照号、手机号、邮箱、银行卡号等敏感信息直接忽略，不要输出。
- 不要推测用户没说过的偏好。
- 不要重复已有 profile 条目。

如果没有可提取内容，就调用工具并传入空数组。"""


def build_v3_working_memory_extraction_prompt(
    user_messages: list[str],
    working_memory: SessionWorkingMemory,
    plan_facts: dict[str, Any],
) -> str:
    messages_text = "\n".join(f"- {message}" for message in user_messages)
    working_text = json.dumps(working_memory.to_dict(), ensure_ascii=False, indent=2)
    facts_text = json.dumps(plan_facts, ensure_ascii=False, indent=2)
    return f"""你在执行一个内部会话工作记忆提取任务。你的目标只有 `working_memory`。

请调用工具 `{_V3_WORKING_MEMORY_EXTRACTION_TOOL_NAME}` 提交结果，不要输出 JSON 正文、不要输出解释文字、不要输出代码块。

用户消息：
{messages_text}

当前解析出的本次行程事实：
{facts_text}

已有会话工作记忆：
{working_text}

分类规则：
- `temporary_preference`：只在当前 session/trip 内成立的临时偏好。
- `temporary_rejection`：只在当前 session/trip 内成立的临时否决。
- `decision_hint`：当前规划后续应记住的决策线索。
- `open_question`：用户还没有回答、后续需要追问的问题。
- `watchout`：当前 trip 后续需要避开的风险提醒。
- `note`：无法归入以上类型但当前会话有用的短期备注。

硬性要求：
- 当前 trip 的目的地、日期、预算、旅客人数、候选池、骨架、每日计划属于 TravelPlanState，不要输出。
- 长期偏好、长期约束、永久拒绝不要输出；这些由 profile extractor 处理。
- 支付信息、会员信息、身份证号、护照号、手机号、邮箱、银行卡号等敏感信息直接忽略，不要输出。
- 不要重复已有 working memory 条目。
- 每条 item 必须设置 `status=active`，并设置完整 expires。

如果没有可提取内容，就调用工具并传入空数组。"""


def build_v3_extraction_gate_prompt(
    user_messages: list[str],
    plan_facts: dict[str, Any],
    existing_memory_summary: dict[str, Any] | None = None,
) -> str:
    messages_text = "\n".join(f"- {message}" for message in user_messages)
    facts_text = json.dumps(plan_facts, ensure_ascii=False, indent=2)
    memory_summary_text = json.dumps(
        existing_memory_summary or {},
        ensure_ascii=False,
        indent=2,
    )
    return f"""你在执行一个轻量级的内部记忆判定任务。

你的职责不是提取记忆条目，而是先判断：当前这一轮对话，是否值得继续执行较重的记忆提取。

请调用工具 `{_V3_EXTRACTION_GATE_TOOL_NAME}` 返回：
- `should_extract`: true / false；只有当 `routes.profile` 或 `routes.working_memory` 其中至少一个为 true 时，`should_extract` 才应为 true
- `routes.profile`: 是否需要执行长期画像 profile extraction
- `routes.working_memory`: 是否需要执行当前会话 / 当前 trip 的 working memory extraction
- `reason`: 稳定英文标识
- `message`: 给前端看的简短中文说明

用户消息：
{messages_text}

当前解析出的本次行程事实：
{facts_text}

已有记忆摘要（仅用于避免重复和辅助判断）：
{memory_summary_text}

判定规则：
- 如果用户表达了跨旅行可复用的长期偏好、硬约束、明确拒绝，设置 `routes.profile=true`
- 如果用户表达了只对当前会话短期有用的临时信号，设置 `routes.working_memory=true`
- 如果 `routes.profile` 或 `routes.working_memory` 其中任意一个为 true，则 `should_extract=true`
- 如果本轮只是推进当前 trip 的事实状态，例如目的地、日期、预算、候选池、骨架、每日安排，没有新的可复用偏好信号，返回 `should_extract=false`
- 如果只是寒暄、确认、重复既有偏好、或空泛追问，返回 `should_extract=false`
- 如果已有记忆摘要里已经明确覆盖本轮信号，且没有新增细化或冲突信息，优先返回 `should_extract=false`
- 不要输出 JSON 正文、不要解释、不要输出具体 memory item

示例：
- “我以后都不坐红眼航班” -> `routes.profile=true`, `routes.working_memory=false`
- “这轮先别考虑迪士尼” -> `routes.profile=false`, `routes.working_memory=true`
- “我不吃辣，这轮先别考虑迪士尼” -> `routes.profile=true`, `routes.working_memory=true`
- “这次五一去京都，预算 3 万” -> `routes.profile=false`, `routes.working_memory=false`
"""


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


def parse_v3_profile_extraction_tool_arguments(
    arguments: dict[str, Any] | None,
) -> V3ExtractionResult:
    if not isinstance(arguments, dict):
        return V3ExtractionResult()
    return parse_v3_extraction_response(
        json.dumps(
            {
                "profile_updates": arguments.get("profile_updates", {}),
                "working_memory": [],
            },
            ensure_ascii=False,
        )
    )


def parse_v3_working_memory_extraction_tool_arguments(
    arguments: dict[str, Any] | None,
) -> V3ExtractionResult:
    if not isinstance(arguments, dict):
        return V3ExtractionResult()
    return parse_v3_extraction_response(
        json.dumps(
            {
                "profile_updates": {
                    "constraints": [],
                    "rejections": [],
                    "stable_preferences": [],
                    "preference_hypotheses": [],
                },
                "working_memory": arguments.get("working_memory", []),
            },
            ensure_ascii=False,
        )
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
