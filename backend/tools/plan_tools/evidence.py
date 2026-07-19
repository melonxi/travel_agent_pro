"""Evidence-chain validation shared by plan-writing tools, plus the
set_excluded_candidates tool.

信息源使用规则（工具边界强制的硬规则）：

1. UGC 来源（xiaohongshu / user）的 fact 不允许标 confidence="confirmed"——
   营业时间、票价、政策等事实必须由 official / web 来源背书。
2. role="anchor" 的活动若没有可靠事实来源（official/web + fact + confirmed +
   source_url），必须 needs_recheck=true——允许"没查到但仍推荐"，
   不允许"没查到且装作可靠"。
3. source_ref 绑定（写入路径注入 SourceRegistry 时启用）：confirmed fact 必须
   引用检索工具铸造的 source_id，且能在本 session 的注册表中解析回真实 URL；
   伪造的 source_ref 直接拒绝（fail closed）。

其余为结构校验；非法枚举一律 ToolError 触发模型自修复，不做静默降级。
"""

from __future__ import annotations

from typing import Any

from state.models import (
    EVIDENCE_CLAIM_TYPES,
    EVIDENCE_CONFIDENCE_LEVELS,
    EVIDENCE_SOURCE_TYPES,
    EXCLUDED_CATEGORIES,
    TravelPlanState,
    UGC_SOURCE_TYPES,
    VISIT_ROLES,
)
from state.plan_writers import write_excluded_candidates
from tools.base import ToolError, tool
from tools.source_registry import SOURCE_ID_PATTERN, SourceRegistry

EVIDENCE_RECORD_SCHEMA = {
    "type": "object",
    # 严格模式：并行 Day Worker 的 submit schema 直接复用本结构。
    "additionalProperties": False,
    "properties": {
        "source_type": {
            "type": "string",
            "enum": sorted(EVIDENCE_SOURCE_TYPES),
            "description": "信息来源：official 官方 / web 普通网页 / xiaohongshu 小红书 UGC / user 用户自述",
        },
        "title": {"type": "string", "description": "来源标题或一句话描述"},
        "summary": {"type": "string", "description": "压缩后的信息摘要（不要贴原文）"},
        "claim_type": {
            "type": "string",
            "enum": sorted(EVIDENCE_CLAIM_TYPES),
            "description": "fact 可核验事实（营业时间/票价/政策）/ experience 主观体验（氛围/排队感受）/ warning 避坑提醒",
        },
        "confidence": {
            "type": "string",
            "enum": sorted(EVIDENCE_CONFIDENCE_LEVELS),
            "description": "confirmed 已被官方或普通 Web 事实来源确认 / unverified 未交叉验证",
        },
        "source_url": {"type": "string", "description": "来源链接，可选"},
        "observed_at": {
            "type": "string",
            "description": "信息观察/发布时间（如 2026-05），提醒时效性，可选",
        },
        "source_ref": {
            "type": "string",
            "description": (
                "来源引用 id（如 src_1a2b3c4d5e），必须原样复制 web_search 结果中的 "
                "source_id，不能自己编造。confidence=confirmed 的 fact 必须携带。"
            ),
        },
    },
    "required": ["source_type", "summary", "claim_type"],
}

VISIT_INFO_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "role": {
            "type": "string",
            "enum": sorted(VISIT_ROLES),
            "description": "anchor 当天强锚点 / normal 普通活动 / backup 备选",
        },
        "recommendation_reason": {
            "type": "string",
            "description": "为什么推荐这个安排（能溯源到画像或证据）",
        },
        "needs_recheck": {
            "type": "boolean",
            "description": "信息不足或未交叉验证时必须为 true，提醒出发前复核",
        },
        "evidence": {
            "type": "array",
            "items": EVIDENCE_RECORD_SCHEMA,
            "description": "支撑该安排的证据记录（压缩摘要 + 来源），1-3 条为宜",
        },
    },
    "required": ["role", "recommendation_reason"],
}

_RELIABLE_SOURCE_TYPES = {"official", "web"}


def is_reliable_fact_record(record: Any) -> bool:
    """可靠来源 = official/web 的已确认事实且带可追溯 URL。

    单看 source_type 不够：web 来源的 experience/unverified 记录
    （"某篇游记说氛围很好"）不能作为 anchor 关闭 needs_recheck 的依据。
    """
    return (
        isinstance(record, dict)
        and record.get("source_type") in _RELIABLE_SOURCE_TYPES
        and record.get("claim_type") == "fact"
        and record.get("confidence") == "confirmed"
        and isinstance(record.get("source_url"), str)
        and record["source_url"].strip().startswith(("http://", "https://"))
    )


def _enum_error(prefix: str, field: str, value: Any, allowed: set[str]) -> ToolError:
    return ToolError(
        f"{prefix}.{field} 非法值: {value!r}",
        error_code="INVALID_VALUE",
        suggestion=f"{field} 必须是 {' / '.join(sorted(allowed))} 之一",
    )


def validate_evidence_records(
    records: Any,
    prefix: str,
    *,
    source_registry: SourceRegistry | None = None,
    session_id: str | None = None,
) -> None:
    """结构校验 + 信息源规则。records 允许为空列表。

    source_registry 注入时启用 source_ref 绑定校验：伪造引用 fail closed，
    confirmed fact 必须携带可解析的 source_ref。未注入（legacy/单测路径）
    时只做结构与格式校验。
    """
    if not isinstance(records, list):
        raise ToolError(
            f"{prefix}.evidence 必须是 list，收到 {type(records).__name__}",
            error_code="INVALID_VALUE",
            suggestion="evidence 应为 list[object]，每条包含 source_type, summary, claim_type",
        )
    registry_active = source_registry is not None and bool(session_id)
    for i, record in enumerate(records):
        record_prefix = f"{prefix}.evidence[{i}]"
        if not isinstance(record, dict):
            raise ToolError(
                f"{record_prefix} 必须是 dict，收到 {type(record).__name__}",
                error_code="INVALID_VALUE",
                suggestion="每条证据必须是 JSON 对象",
            )
        source_type = record.get("source_type")
        if source_type not in EVIDENCE_SOURCE_TYPES:
            raise _enum_error(record_prefix, "source_type", source_type, EVIDENCE_SOURCE_TYPES)
        claim_type = record.get("claim_type")
        if claim_type not in EVIDENCE_CLAIM_TYPES:
            raise _enum_error(record_prefix, "claim_type", claim_type, EVIDENCE_CLAIM_TYPES)
        confidence = record.get("confidence", "unverified")
        if confidence not in EVIDENCE_CONFIDENCE_LEVELS:
            raise _enum_error(
                record_prefix, "confidence", confidence, EVIDENCE_CONFIDENCE_LEVELS
            )
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ToolError(
                f"{record_prefix}.summary 必须是非空字符串",
                error_code="INVALID_VALUE",
                suggestion="summary 写压缩后的信息摘要，不要留空",
            )
        # 硬规则 1：UGC 不允许被写成已确认事实。
        if (
            source_type in UGC_SOURCE_TYPES
            and claim_type == "fact"
            and confidence == "confirmed"
        ):
            raise ToolError(
                f"{record_prefix}: {source_type} 来源的 fact 不允许标 confidence=confirmed",
                error_code="UGC_FACT_NOT_CONFIRMABLE",
                suggestion=(
                    "营业时间/票价/政策等事实必须用 official 或 web 来源交叉验证后才能 "
                    "confirmed；UGC 信息请改为 claim_type=experience/warning，"
                    "或保持 confidence=unverified 并在 visit_info 上标 needs_recheck=true。"
                ),
            )
        _validate_source_ref(
            record,
            record_prefix,
            registry_active=registry_active,
            source_registry=source_registry,
            session_id=session_id or "",
        )


def _validate_source_ref(
    record: dict,
    record_prefix: str,
    *,
    registry_active: bool,
    source_registry: SourceRegistry | None,
    session_id: str,
) -> None:
    """硬规则 3：source_ref 必须能回溯到检索工具铸造的登记记录。"""
    source_ref = record.get("source_ref")
    if source_ref is not None:
        if not isinstance(source_ref, str) or not SOURCE_ID_PATTERN.match(source_ref):
            raise ToolError(
                f"{record_prefix}.source_ref 格式非法: {source_ref!r}",
                error_code="INVALID_VALUE",
                suggestion=(
                    "source_ref 必须原样复制 web_search 结果中的 source_id"
                    "（形如 src_1a2b3c4d5e），不能自己编造。"
                ),
            )
        if registry_active:
            registered = source_registry.lookup(session_id, source_ref)
            if registered is None:
                raise ToolError(
                    f"{record_prefix}.source_ref 无法解析: {source_ref}",
                    error_code="UNKNOWN_SOURCE_REF",
                    suggestion=(
                        "该 source_id 不在本 session 的来源注册表中——只能引用"
                        "本 session 内 web_search 结果返回的 source_id，不能编造"
                        "或复用其他会话的 id。"
                    ),
                )
            source_url = record.get("source_url")
            if (
                isinstance(source_url, str)
                and source_url.strip()
                and source_url.strip() != registered.get("url")
            ):
                raise ToolError(
                    f"{record_prefix}: source_url 与 source_ref 登记的 URL 不一致",
                    error_code="SOURCE_REF_URL_MISMATCH",
                    suggestion=(
                        f"source_ref={source_ref} 登记的 URL 是 "
                        f"{registered.get('url')}；source_url 必须与其一致，"
                        "或直接省略 source_url 由注册表提供。"
                    ),
                )
    # confirmed fact 必须可回溯（仅在注册表在场的写入路径强制）。
    if (
        registry_active
        and record.get("claim_type") == "fact"
        and record.get("confidence", "unverified") == "confirmed"
        and source_ref is None
    ):
        raise ToolError(
            f"{record_prefix}: confidence=confirmed 的 fact 必须携带 source_ref",
            error_code="CONFIRMED_FACT_NEEDS_SOURCE_REF",
            suggestion=(
                "已确认事实必须引用 web_search 结果中的 source_id（source_ref 字段），"
                "使其可回溯到真实工具调用；没有对应搜索结果时请改为 "
                "confidence=unverified。"
            ),
        )


def validate_visit_info(
    visit_info: Any,
    prefix: str,
    *,
    source_registry: SourceRegistry | None = None,
    session_id: str | None = None,
) -> None:
    """visit_info 结构校验 + 锚点最小证据规则。"""
    if not isinstance(visit_info, dict):
        raise ToolError(
            f"{prefix}.visit_info 必须是 dict，收到 {type(visit_info).__name__}",
            error_code="INVALID_VALUE",
            suggestion="visit_info 应包含 role, recommendation_reason，可选 needs_recheck, evidence",
        )
    role = visit_info.get("role", "normal")
    if role not in VISIT_ROLES:
        raise _enum_error(f"{prefix}.visit_info", "role", role, VISIT_ROLES)
    reason = visit_info.get("recommendation_reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ToolError(
            f"{prefix}.visit_info.recommendation_reason 必须是非空字符串",
            error_code="INVALID_VALUE",
            suggestion="写清楚为什么推荐这个安排（应能溯源到画像或证据）",
        )
    needs_recheck = visit_info.get("needs_recheck", False)
    if not isinstance(needs_recheck, bool):
        raise ToolError(
            f"{prefix}.visit_info.needs_recheck 必须是布尔值",
            error_code="INVALID_VALUE",
            suggestion="信息不足时 needs_recheck=true，已交叉验证时 false",
        )
    evidence = visit_info.get("evidence", [])
    validate_evidence_records(
        evidence,
        f"{prefix}.visit_info",
        source_registry=source_registry,
        session_id=session_id,
    )
    # 硬规则 2：anchor 没有可靠来源时必须显式标记待复核。
    if role == "anchor" and not needs_recheck:
        has_reliable = any(is_reliable_fact_record(record) for record in evidence)
        if not has_reliable:
            raise ToolError(
                f"{prefix}.visit_info: role=anchor 但没有可靠事实来源"
                "（official/web + claim_type=fact + confidence=confirmed + source_url）",
                error_code="ANCHOR_NEEDS_RELIABLE_SOURCE",
                suggestion=(
                    "强锚点至少要有一条 official 或 web 来源、claim_type=fact、"
                    "confidence=confirmed 且带 http(s) source_url 的证据；"
                    "暂时查不到时保留推荐，但必须 needs_recheck=true。"
                ),
            )


# ---------------------------------------------------------------------------
# set_excluded_candidates
# ---------------------------------------------------------------------------

_SET_EXCLUDED_CANDIDATES_PARAMS = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "被淘汰/暂缓的候选名称"},
                    "reason": {"type": "string", "description": "淘汰或暂缓的具体原因"},
                    "category": {
                        "type": "string",
                        "enum": sorted(EXCLUDED_CATEGORIES),
                        "description": (
                            "淘汰类别：distance 距离 / schedule 时间排不下 / weather 天气 / "
                            "preference 与画像冲突 / duplicate 重复体验 / evidence 证据不足"
                        ),
                    },
                    "reconsider_when": {
                        "type": "string",
                        "description": "什么条件下值得重新考虑（如“行程延长到 6 天”），可选",
                    },
                    "source_candidate_id": {
                        "type": "string",
                        "description": "对应 candidate_pool/shortlist 项的稳定 id，可选",
                    },
                },
                "required": ["name", "reason", "category"],
            },
            "description": "被淘汰/暂缓候选列表（整体替换）",
        },
    },
    "required": ["items"],
}


def make_set_excluded_candidates_tool(plan: TravelPlanState):
    @tool(
        name="set_excluded_candidates",
        description=(
            "写入被淘汰/暂缓的候选列表（整体替换）。\n"
            "触发条件：候选筛选或逐日排程中明确淘汰某些候选后应调用——淘汰是可解释的决策，"
            "不允许只在正文说'不建议去 X'而不留下结构化记录。\n"
            "禁止行为：不要把仍在考虑中的候选写入此字段——仍在考虑的留在 candidate_pool/shortlist。\n"
            "写入后效果：excluded_candidates 整体替换；Phase 4 交付物会自动生成'已排除/暂缓项目'章节。"
        ),
        phases=[2, 3],
        parameters=_SET_EXCLUDED_CANDIDATES_PARAMS,
        side_effect="write",
        human_label="记录淘汰候选",
    )
    async def set_excluded_candidates(items: list) -> dict:
        if not isinstance(items, list):
            raise ToolError(
                f"items 必须是 list，收到 {type(items).__name__}",
                error_code="INVALID_VALUE",
                suggestion="请传 list[object]，每项包含 name, reason, category",
            )
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ToolError(
                    f"items[{i}] 必须是 dict，收到 {type(item).__name__}",
                    error_code="INVALID_VALUE",
                    suggestion="每个淘汰记录必须是 JSON 对象",
                )
            for field in ("name", "reason"):
                value = item.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ToolError(
                        f"items[{i}].{field} 必须是非空字符串",
                        error_code="INVALID_VALUE",
                        suggestion=f"每个淘汰记录必须写明 {field}",
                    )
            category = item.get("category")
            if category not in EXCLUDED_CATEGORIES:
                raise _enum_error(f"items[{i}]", "category", category, EXCLUDED_CATEGORIES)
        prev_count = len(plan.excluded_candidates)
        write_excluded_candidates(plan, items)
        return {
            "updated_field": "excluded_candidates",
            "count": len(items),
            "previous_count": prev_count,
        }

    return set_excluded_candidates
