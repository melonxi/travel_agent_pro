from __future__ import annotations

import logging
import re
import time
from datetime import date as dt_date, timedelta

from agent.compaction import (
    compact_messages_for_prompt,
    compute_prompt_budget,
    estimate_messages_tokens,
)
from agent.hooks import GateResult, HookManager
from agent.internal_tasks import InternalTask
from agent.message_filters import active_runtime_messages
from agent.tagged_context import app_event_message
from agent.types import Message, Role
from harness.judge import (
    build_judge_prompt,
    build_judge_tool,
    parse_judge_tool_arguments,
)
from harness.validator import (
    validate_hard_constraints,
    validate_incremental,
    validate_lock_budget,
)
from tools.plan_tools import PLAN_WRITER_TOOL_NAMES

from api.orchestration.session.pending_notes import flush_pending_system_notes, push_pending_system_note
from api.orchestration.common.telemetry_helpers import (
    _days_count_from_dates,
    _plan_writer_state_changes,
    _plan_writer_updates,
)

logger = logging.getLogger(__name__)

_REFERENCE_ONLY_WEATHER_NOTE = "精确日期预报不可用"
_REQUIRED_WEATHER_CONFIRMATION = "临近出发前再确认"
_FUTURE_WEATHER_ERROR_CODE = "FUTURE_WEATHER_NOT_TREATED_AS_EXACT"
_DELIVERABLE_CONSISTENCY_ERROR_CODE = "DELIVERABLE_FACTS_CONFLICT_WITH_PLAN"
_DELIVERABLE_ESTIMATION_ERROR_CODE = "DELIVERABLE_ESTIMATION_NOT_VISIBLE"
_ESTIMATION_VISIBILITY_MARKERS = ("估算", "未验证", "未返回可用", "⚠️")
_ISO_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
_TRANSPORT_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?:[A-Z]{2}\d{2,4}|[GDKCTZ]\d{1,5})(?![A-Z0-9])"
)
_BUDGET_AMOUNT_RE = re.compile(
    r"(?:[¥￥]\s*)?(\d+(?:,\d{3})*(?:\.\d+)?)\s*(万|千|元|块|CNY|RMB)?",
    re.IGNORECASE,
)
_WEATHER_CONTEXT_RE = re.compile(
    r"天气|气温|温度|降雨|下雨|小雨|中雨|大雨|阵雨|雷阵雨|晴|多云|阴|湿热|带伞"
)
_TEMPERATURE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:°\s*C|°C|℃|摄氏度|度)")


def _has_reference_only_weather(messages: list[Message] | None) -> bool:
    for message in messages or []:
        result = message.tool_result
        if message.role != Role.TOOL or result is None:
            continue
        if result.status != "success" or not isinstance(result.data, dict):
            continue
        forecast = result.data.get("forecast")
        if not isinstance(forecast, dict):
            continue
        note = str(forecast.get("note") or "")
        if _REFERENCE_ONLY_WEATHER_NOTE in note:
            return True
    return False


def _combined_deliverable_markdown(result_data: dict, arguments: dict) -> str:
    parts: list[str] = []
    for key in ("travel_plan_markdown", "checklist_markdown"):
        val = result_data.get(key)
        if val is not None:
            parts.append(str(val))
    daily_sections = arguments.get("daily_sections")
    if daily_sections and isinstance(daily_sections, list):
        day_parts: list[str] = []
        for section in daily_sections:
            if isinstance(section, dict):
                day = section.get("day", "?")
                title = section.get("title", "")
                content = section.get("content", "")
                heading = f"## 第 {day} 天"
                if title:
                    heading += f"  {title}"
                day_parts.append(heading)
                day_parts.append(content)
        if day_parts:
            parts.append("\n".join(day_parts))
    checklist_categories = arguments.get("checklist_categories")
    if checklist_categories and isinstance(checklist_categories, list):
        cat_parts: list[str] = []
        for cat in checklist_categories:
            if isinstance(cat, dict):
                category = cat.get("category", "")
                items = cat.get("items", [])
                if category:
                    cat_parts.append(f"### {category}")
                for item in items:
                    cat_parts.append(f"- {item}")
        if cat_parts:
            parts.append("\n".join(cat_parts))
    for key in ("travel_plan_markdown", "checklist_markdown"):
        val = arguments.get(key)
        if val is not None and key not in result_data:
            parts.append(str(val))
    return "\n\n".join(parts)


def _iter_text_values(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        texts: list[str] = []
        for item in value.values():
            texts.extend(_iter_text_values(item))
        return texts
    if isinstance(value, (list, tuple)):
        texts = []
        for item in value:
            texts.extend(_iter_text_values(item))
        return texts
    return []


def _trip_date_set(plan) -> set[str]:
    dates = getattr(plan, "dates", None)
    if dates is None:
        return set()
    try:
        start = dt_date.fromisoformat(dates.start)
        end = dt_date.fromisoformat(dates.end)
    except (TypeError, ValueError):
        return {str(getattr(dates, "start", "")), str(getattr(dates, "end", ""))}
    if end < start or (end - start).days > 366:
        return {dates.start, dates.end}
    return {
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    }


def _transport_codes(value) -> set[str]:
    codes: set[str] = set()
    for text in _iter_text_values(value):
        codes.update(_TRANSPORT_CODE_RE.findall(text.upper().replace(" ", "")))
    return codes


def _known_other_accommodation_names(plan) -> set[str]:
    accommodation = getattr(plan, "accommodation", None)
    locked_hotel = str(getattr(accommodation, "hotel", "") or "").strip()
    names: set[str] = set()
    for option in getattr(plan, "accommodation_options", []) or []:
        if not isinstance(option, dict):
            continue
        name = str(option.get("name") or option.get("hotel") or "").strip()
        if name and name != locked_hotel:
            names.add(name)
    return names


def _parse_budget_amount(raw: str, unit: str | None) -> float | None:
    try:
        amount = float(raw.replace(",", ""))
    except ValueError:
        return None
    normalized_unit = (unit or "").lower()
    if normalized_unit == "万":
        amount *= 10_000
    elif normalized_unit == "千":
        amount *= 1_000
    return amount


def _budget_amounts_from_total_lines(markdown: str) -> list[float]:
    amounts: list[float] = []
    for line in markdown.splitlines():
        if "总预算" not in line and "预算上限" not in line:
            continue
        for raw, unit in _BUDGET_AMOUNT_RE.findall(line):
            amount = _parse_budget_amount(raw, unit)
            if amount is not None:
                amounts.append(amount)
    return amounts


def _weather_state_text(plan) -> str:
    parts: list[str] = []
    for day in getattr(plan, "daily_plans", []) or []:
        tips = str(getattr(day, "tips", "") or "")
        if _WEATHER_CONTEXT_RE.search(tips):
            parts.append(tips)
        for activity in getattr(day, "activities", []) or []:
            activity_notes = str(getattr(activity, "notes", "") or "")
            if _WEATHER_CONTEXT_RE.search(activity_notes):
                parts.append(activity_notes)
    return "\n".join(parts)


def _temperatures(text: str) -> list[float]:
    values: list[float] = []
    for raw in _TEMPERATURE_RE.findall(text):
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values


def build_deliverable_consistency_errors(
    plan,
    result_data: dict | None,
    arguments: dict | None,
) -> list[str]:
    result_data = result_data if isinstance(result_data, dict) else {}
    arguments = arguments if isinstance(arguments, dict) else {}
    if any(
        result_data.get(key) is not None
        for key in ("travel_plan_markdown", "checklist_markdown")
    ):
        markdown = _combined_deliverable_markdown(result_data, {})
    else:
        markdown = _combined_deliverable_markdown({}, arguments)
    errors: list[str] = []

    allowed_dates = _trip_date_set(plan)
    mentioned_dates = set(_ISO_DATE_RE.findall(markdown))
    unexpected_dates = sorted(mentioned_dates - allowed_dates)
    if allowed_dates and unexpected_dates:
        errors.append(
            "日期与状态不一致：交付物出现 "
            + "、".join(unexpected_dates[:3])
            + f"，但当前行程日期是 {min(allowed_dates)} 至 {max(allowed_dates)}"
        )

    selected_transport = getattr(plan, "selected_transport", None)
    locked_codes = _transport_codes(selected_transport)
    mentioned_codes = _transport_codes(markdown)
    unexpected_codes = sorted(mentioned_codes - locked_codes)
    if locked_codes and unexpected_codes:
        errors.append(
            "锁定交通不一致：交付物出现 "
            + "、".join(unexpected_codes[:3])
            + "，但当前锁定交通是 "
            + "、".join(sorted(locked_codes))
        )

    other_hotels = sorted(
        name for name in _known_other_accommodation_names(plan) if name in markdown
    )
    accommodation = getattr(plan, "accommodation", None)
    locked_hotel = str(getattr(accommodation, "hotel", "") or "").strip()
    if locked_hotel and other_hotels:
        errors.append(
            "锁定住宿不一致：交付物出现 "
            + "、".join(other_hotels[:3])
            + f"，但当前锁定住宿是 {locked_hotel}"
        )

    budget = getattr(plan, "budget", None)
    budget_total = float(getattr(budget, "total", 0) or 0)
    if budget_total > 0:
        total_amounts = _budget_amounts_from_total_lines(markdown)
        close_amounts = [
            amount
            for amount in total_amounts
            if abs(amount - budget_total) <= max(1.0, budget_total * 0.02)
        ]
        if total_amounts and not close_amounts:
            errors.append(
                "总预算与状态不一致：交付物写 "
                + "、".join(f"{amount:g}" for amount in total_amounts[:3])
                + f"，但当前总预算是 {budget_total:g}"
            )

    state_weather = _weather_state_text(plan)
    state_temps = _temperatures(state_weather)
    markdown_temps = _temperatures(markdown)
    if state_temps and markdown_temps:
        has_matching_temp = any(
            abs(markdown_temp - state_temp) <= 3.0
            for markdown_temp in markdown_temps
            for state_temp in state_temps
        )
        if not has_matching_temp:
            errors.append(
                "天气温度与逐日行程不一致：状态记录约 "
                + "、".join(f"{temp:g}°C" for temp in sorted(set(state_temps))[:3])
                + "，交付物写约 "
                + "、".join(f"{temp:g}°C" for temp in sorted(set(markdown_temps))[:3])
            )

    return errors


def _has_estimated_transport(plan) -> bool:
    for day in getattr(plan, "daily_plans", []) or []:
        for activity in getattr(day, "activities", []) or []:
            if getattr(activity, "transport_estimated", False):
                return True
    return False


def build_estimation_visibility_errors(
    plan,
    result_data: dict | None,
    arguments: dict | None,
) -> list[str]:
    if not _has_estimated_transport(plan):
        return []
    result_data = result_data if isinstance(result_data, dict) else {}
    arguments = arguments if isinstance(arguments, dict) else {}
    if any(
        result_data.get(key) is not None
        for key in ("travel_plan_markdown", "checklist_markdown")
    ):
        markdown = _combined_deliverable_markdown(result_data, {})
    else:
        markdown = _combined_deliverable_markdown({}, arguments)
    if any(marker in markdown for marker in _ESTIMATION_VISIBILITY_MARKERS):
        return []
    daily_sections = arguments.get("daily_sections")
    if daily_sections and isinstance(daily_sections, list):
        for section in daily_sections:
            content = str(section.get("content", ""))
            if any(marker in content for marker in _ESTIMATION_VISIBILITY_MARKERS):
                return []
    return [
        "逐日行程含未经路线工具验证的估算通勤，但交付物未对用户标注"
        "（缺少「估算」等可见提示）"
    ]


def evaluate_deliverable_gate(
    plan,
    result_data: dict | None,
    arguments: dict | None,
) -> tuple[str, list[str]] | None:
    consistency_errors = build_deliverable_consistency_errors(
        plan, result_data, arguments
    )
    if consistency_errors:
        return (_DELIVERABLE_CONSISTENCY_ERROR_CODE, consistency_errors)
    estimation_errors = build_estimation_visibility_errors(
        plan, result_data, arguments
    )
    if estimation_errors:
        return (_DELIVERABLE_ESTIMATION_ERROR_CODE, estimation_errors)
    return None


def _mark_future_weather_delivery_error(result) -> None:
    result.status = "error"
    result.data = None
    result.error_code = _FUTURE_WEATHER_ERROR_CODE
    result.error = (
        "check_weather 返回的是近似参考天气，不是出行日精确预报；"
        "交付物不能写成确定天气。"
    )
    result.suggestion = (
        f"把天气表述改为参考信息，并在 travel_plan_markdown 或 "
        f"checklist_markdown 中明确写「{_REQUIRED_WEATHER_CONFIRMATION}」。"
    )


def _mark_deliverable_consistency_error(result, errors: list[str]) -> None:
    result.status = "error"
    result.data = None
    result.error_code = _DELIVERABLE_CONSISTENCY_ERROR_CODE
    result.error = "交付物与已锁定的行程状态不一致：" + "；".join(errors[:5])
    result.suggestion = (
        "请按 TravelPlanState 的权威字段重写 generate_summary；"
        "不要改写已锁定交通、住宿、日期、预算或逐日天气事实。"
    )


def _mark_deliverable_estimation_error(result, errors: list[str]) -> None:
    result.status = "error"
    result.data = None
    result.error_code = _DELIVERABLE_ESTIMATION_ERROR_CODE
    result.error = "交付物未标注估算通勤：" + "；".join(errors[:5])
    result.suggestion = (
        "逐日行程存在未经 calculate_route 验证的估算通勤，"
        "系统应自动在 travel_plan_markdown 中标注；"
        "如未自动标注，请在 daily_sections 的 content 中手动添加「估算」或「⚠️」标记后重新提交。"
    )


def build_agent_hooks(
    *,
    plan,
    sessions: dict[str, dict],
    resolved_context_window: dict[str, int],
    config,
    context_mgr,
    compression_events: list[dict] | None,
    create_llm_provider_func,
    collect_forced_tool_call_arguments,
    quality_gate_retries: dict,
):
    hooks = HookManager()
    internal_task_events: list[InternalTask] = []

    async def on_tool_call(**kwargs):
        tool_name = kwargs.get("tool_name")
        if tool_name in PLAN_WRITER_TOOL_NAMES:
            result = kwargs.get("result")
            if (
                result
                and isinstance(result.data, dict)
                and result.data.get("backtracked")
            ):
                session = sessions.get(plan.session_id)
                if session:
                    session["needs_rebuild"] = True
            return

    async def on_validate(**kwargs):
        tool_name = kwargs.get("tool_name")
        if tool_name == "generate_summary":
            tc = kwargs.get("tool_call")
            result = kwargs.get("result")
            if not (
                tc
                and result
                and result.status == "success"
                and isinstance(result.data, dict)
            ):
                return
            messages = kwargs.get("messages")

            def _apply_gate() -> None:
                gate = evaluate_deliverable_gate(
                    plan, result.data, tc.arguments or {}
                )
                if gate is None:
                    return
                code, errors = gate
                if code == _DELIVERABLE_CONSISTENCY_ERROR_CODE:
                    _mark_deliverable_consistency_error(result, errors)
                else:
                    _mark_deliverable_estimation_error(result, errors)

            if not _has_reference_only_weather(messages):
                _apply_gate()
                return
            markdown = _combined_deliverable_markdown(result.data, tc.arguments or {})
            if _REQUIRED_WEATHER_CONFIRMATION not in markdown:
                _mark_future_weather_delivery_error(result)
                return
            _apply_gate()
            return

        if tool_name in PLAN_WRITER_TOOL_NAMES:
            tc = kwargs.get("tool_call")
            result = kwargs.get("result")
            arguments = tc.arguments if tc and tc.arguments else {}
            session = sessions.get(plan.session_id)
            if not (
                result
                and result.status == "success"
                and isinstance(result.data, dict)
                and session
            ):
                return

            updates = _plan_writer_updates(tool_name, arguments, result.data)
            if not updates:
                return

            session["_pending_state_changes"] = _plan_writer_state_changes(
                tool_name,
                arguments,
                result.data,
            )
            errors: list[str] = []
            for update in updates:
                field = update["field"]
                value = update["value"]
                errors.extend(validate_incremental(plan, field, value))
                if field in ("selected_transport", "accommodation"):
                    errors.extend(validate_lock_budget(plan))

            if errors:
                session["_pending_validation_errors"] = errors
                push_pending_system_note(
                    session,
                    "[实时约束检查]\n"
                    + "\n".join(f"- {error}" for error in errors),
                )

    async def on_before_llm(**kwargs):
        msgs = kwargs.get("messages")
        tools = kwargs.get("tools") or []
        phase = kwargs.get("phase", plan.phase)
        if not msgs:
            return
        session = sessions.get(plan.session_id)
        if session:
            flush_pending_system_notes(session, msgs)
        prompt_budget = compute_prompt_budget(
            resolved_context_window["value"],
            config.llm.max_tokens,
        )
        estimated_tokens_before = estimate_messages_tokens(msgs, tools=tools)
        message_count_before = len(msgs)

        tool_compaction = compact_messages_for_prompt(
            msgs,
            prompt_budget=prompt_budget,
            tools=tools,
        )
        if tool_compaction.changed:
            msgs[:] = tool_compaction.messages

        estimated_after_tool_compaction = estimate_messages_tokens(
            msgs, tools=tools
        )
        if (
            tool_compaction.changed
            and estimated_after_tool_compaction <= prompt_budget
        ):
            if compression_events is not None:
                compression_events.append(
                    {
                        "timestamp": time.time(),
                        "message_count_before": message_count_before,
                        "message_count_after": len(msgs),
                        "must_keep_count": 0,
                        "compressed_count": tool_compaction.compacted_tool_messages,
                        "estimated_tokens_before": estimated_tokens_before,
                        "estimated_tokens_after": estimated_after_tool_compaction,
                        "mode": "tool_compaction",
                        "reason": (
                            f"prompt 预算 {prompt_budget} 内进行 {tool_compaction.mode or 'moderate'}"
                            f" TOOL 压缩，usage_ratio={tool_compaction.usage_ratio_before:.2f}"
                        ),
                    }
                )
            return

        if not context_mgr.should_compress(msgs, prompt_budget, tools=tools):
            return

        must_keep, compressible = context_mgr.classify_messages(msgs)
        recent = msgs[-4:]
        recent_ids = {id(m) for m in recent}
        older_compressible = [m for m in compressible if id(m) not in recent_ids]
        summary_source = (
            older_compressible if len(older_compressible) > 2 else compressible
        )
        if len(summary_source) <= 2:
            return

        summary_text = await context_mgr.compress_for_transition(
            messages=summary_source,
            from_phase=phase,
            to_phase=phase,
            llm_factory=None,
        )
        if not summary_text:
            return

        summary_lines = summary_text.splitlines()
        summary = app_event_message(
            "history_summary",
            "[对话摘要]\n" + "\n".join(summary_lines[-12:]),
        )

        rebuilt: list[Message] = []
        seen_ids: set[int] = set()

        def append_unique(message: Message) -> None:
            ident = id(message)
            if ident in seen_ids:
                return
            rebuilt.append(message)
            seen_ids.add(ident)

        sys_msg = msgs[0] if msgs and msgs[0].role == Role.SYSTEM else None
        if sys_msg:
            append_unique(sys_msg)
        for message in must_keep:
            append_unique(message)
        append_unique(summary)
        for message in recent:
            append_unique(message)

        msgs[:] = rebuilt

        estimated_after_summary = estimate_messages_tokens(msgs, tools=tools)
        if compression_events is not None:
            compression_events.append(
                {
                    "timestamp": time.time(),
                    "message_count_before": message_count_before,
                    "message_count_after": len(msgs),
                    "must_keep_count": len(must_keep),
                    "compressed_count": len(summary_source),
                    "estimated_tokens_before": estimated_tokens_before,
                    "estimated_tokens_after": estimated_after_summary,
                    "mode": "history_summary",
                    "reason": (
                        f"prompt 预算 {prompt_budget} 仍不足，"
                        f"压缩旧消息并保留最近 {len(recent)} 条"
                    ),
                }
            )

    hooks.register("before_llm_call", on_before_llm)

    async def on_soft_judge(**kwargs):
        tool_name = kwargs.get("tool_name")
        if tool_name not in (
            "save_day_plan",
            "replace_all_day_plans",
            "generate_summary",
        ):
            return
        tool_call = kwargs.get("tool_call")
        result = kwargs.get("result")
        session = sessions.get(plan.session_id)
        if not (result and result.status == "success"):
            return
        task_id = f"soft_judge:{getattr(tool_call, 'id', tool_name)}"
        started_at = time.time()
        internal_task_events.append(
            InternalTask(
                id=task_id,
                kind="soft_judge",
                label="行程质量评审",
                status="pending",
                message="正在检查行程节奏、地理顺路性和个性化匹配…",
                related_tool_call_id=getattr(tool_call, "id", None),
                started_at=started_at,
            )
        )
        if not plan.daily_plans:
            if tool_name == "generate_summary" and session:
                session["_phase4_deliverables_quality"] = {
                    "tool_call_id": getattr(tool_call, "id", tool_name),
                    "status": "approved",
                    "reason": "soft_judge_skipped_no_daily_plans",
                }
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="soft_judge",
                    label="行程质量评审",
                    status="skipped",
                    message="暂无每日行程，跳过质量评审。",
                    related_tool_call_id=getattr(tool_call, "id", None),
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return
        if not session:
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="soft_judge",
                    label="行程质量评审",
                    status="skipped",
                    message="会话已不可用，跳过质量评审。",
                    related_tool_call_id=getattr(tool_call, "id", None),
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return
        try:
            prefs = {p.key: p.value for p in plan.preferences}
            prompt_text = build_judge_prompt(plan.to_dict(), prefs)
            judge_llm = create_llm_provider_func(config.llm)
            judge_msgs = [
                Message(role=Role.SYSTEM, content="你是旅行行程质量评估专家。"),
                Message(role=Role.USER, content=prompt_text),
            ]
            score_args = await collect_forced_tool_call_arguments(
                judge_llm,
                messages=judge_msgs,
                tool_def=build_judge_tool(),
            )
            score = parse_judge_tool_arguments(score_args)
        except Exception as exc:
            logger.warning("soft judge failed", exc_info=True)
            if tool_name == "generate_summary" and session:
                session["_phase4_deliverables_quality"] = {
                    "tool_call_id": getattr(tool_call, "id", tool_name),
                    "status": "approved",
                    "reason": "soft_judge_error_allows_freeze",
                }
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="soft_judge",
                    label="行程质量评审",
                    status="error",
                    message="质量评审未完成，不影响已保存的行程。",
                    error=str(exc),
                    related_tool_call_id=getattr(tool_call, "id", None),
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return
        # Stage judge scores for the TOOL_RESULT handler to attach to ToolCallRecord
        judge_scores = {
            "overall": score.overall,
            "pace": score.pace,
            "geography": score.geography,
            "coherence": score.coherence,
            "personalization": score.personalization,
            "suggestions_count": len(score.suggestions),
        }
        session["_pending_judge_scores"] = judge_scores
        stats = session.get("stats")
        if stats and stats.tool_calls:
            latest = stats.tool_calls[-1]
            if latest.tool_name == tool_name and latest.judge_scores is None:
                latest.judge_scores = judge_scores
        should_inject_feedback = (
            score.overall < config.quality_gate.threshold
        )
        suggestions = score.suggestions or [
            "请根据质量评估结果先修订交付物，再重新提交 generate_summary。"
        ]
        if should_inject_feedback:
            feedback_count = int(session.get("_soft_judge_repair_feedback_count", 0))
            feedback_limit = max(1, int(config.quality_gate.max_retries))
            should_inject_feedback = feedback_count < feedback_limit
            session["_soft_judge_repair_feedback_count"] = feedback_count + 1
        if tool_name == "generate_summary":
            session["_phase4_deliverables_quality"] = {
                "tool_call_id": getattr(tool_call, "id", tool_name),
                "status": "blocked" if should_inject_feedback else "approved",
                "overall": score.overall,
                "threshold": config.quality_gate.threshold,
            }
        if should_inject_feedback:
            suggestion_text = "\n".join(f"- {s}" for s in suggestions)
            active_runtime_messages(session).append(
                app_event_message(
                    "soft_judge",
                    f"行程质量评估（{score.overall:.1f}/5）：\n{suggestion_text}",
                )
            )
        final_status = (
            "warning"
            if score.suggestions or score.overall < config.quality_gate.threshold
            else "success"
        )
        final_message = (
            f"评分 {score.overall:.1f}/5，发现 {len(score.suggestions)} 条改进建议。"
            if score.suggestions or score.overall < config.quality_gate.threshold
            else f"评分 {score.overall:.1f}/5，未发现需要立即处理的问题。"
        )
        internal_task_events.append(
            InternalTask(
                id=task_id,
                kind="soft_judge",
                label="行程质量评审",
                status=final_status,
                message=final_message,
                related_tool_call_id=getattr(tool_call, "id", None),
                result=judge_scores,
                started_at=started_at,
                ended_at=time.time(),
            )
        )

    hooks.register("after_tool_call", on_tool_call)
    hooks.register("after_tool_call", on_validate)
    hooks.register("after_tool_result", on_soft_judge)

    async def on_before_phase_transition(**kwargs):
        target_plan = kwargs.get("plan", plan)
        from_phase = int(kwargs.get("from_phase", target_plan.phase))
        to_phase = int(kwargs.get("to_phase", from_phase))
        session = sessions.get(target_plan.session_id)
        task_id = f"quality_gate:{target_plan.session_id}:{from_phase}:{to_phase}"
        started_at = time.time()
        internal_task_events.append(
            InternalTask(
                id=task_id,
                kind="quality_gate",
                label="阶段推进检查",
                status="pending",
                message=f"正在判断 Phase {from_phase} 是否可以进入 Phase {to_phase}…",
                blocking=True,
                scope="turn",
                result={"from_phase": from_phase, "to_phase": to_phase},
                started_at=started_at,
            )
        )

        # Feasibility gate: catch impossible plans early (Phase 1→2)
        if from_phase == 1 and to_phase == 2:
            from harness.feasibility import check_feasibility

            days_count = _days_count_from_dates(target_plan.dates)
            budget_total = None
            if target_plan.budget and target_plan.budget.total:
                budget_total = target_plan.budget.total
            feas = check_feasibility(
                target_plan.destination, budget_total, days_count
            )
            if not feas.feasible:
                feedback = (
                    "[可行性检查]\n当前旅行计划存在以下问题：\n"
                    + "\n".join(f"- {r}" for r in feas.reasons)
                    + "\n请调整后再继续。"
                )
                if session:
                    active_runtime_messages(session).append(
                        app_event_message("feasibility", feedback)
                    )
                internal_task_events.append(
                    InternalTask(
                        id=task_id,
                        kind="quality_gate",
                        label="阶段推进检查",
                        status="warning",
                        message="可行性检查未通过，暂不推进阶段。",
                        blocking=True,
                        scope="turn",
                        result={"reasons": feas.reasons},
                        started_at=started_at,
                        ended_at=time.time(),
                    )
                )
                return GateResult(allowed=False, feedback=feedback)

        errors = validate_hard_constraints(target_plan)
        if errors:
            feedback = "[质量门控]\n硬约束冲突，必须修正：\n" + "\n".join(
                f"- {error}" for error in errors
            )
            if session:
                active_runtime_messages(session).append(
                    app_event_message("hard_constraint", feedback)
                )
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="warning",
                    message="发现硬约束冲突，暂不推进阶段。",
                    blocking=True,
                    scope="turn",
                    result={"errors": errors},
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return GateResult(allowed=False, feedback=feedback)

        if (from_phase, to_phase) not in {(2, 3), (3, 4)}:
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="success",
                    message=f"允许进入 Phase {to_phase}。",
                    blocking=True,
                    scope="turn",
                    result={"from_phase": from_phase, "to_phase": to_phase},
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return GateResult(allowed=True)

        try:
            prefs = {p.key: p.value for p in target_plan.preferences}
            prompt_text = build_judge_prompt(target_plan.to_dict(), prefs)
            judge_llm = create_llm_provider_func(config.llm)
            judge_msgs = [
                Message(role=Role.SYSTEM, content="你是旅行行程质量评估专家。"),
                Message(role=Role.USER, content=prompt_text),
            ]
            score_args = await collect_forced_tool_call_arguments(
                judge_llm,
                messages=judge_msgs,
                tool_def=build_judge_tool(),
            )
            score = parse_judge_tool_arguments(score_args)
        except Exception as exc:
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="skipped",
                    message="阶段推进检查不可用，已跳过并允许主流程继续。",
                    blocking=True,
                    scope="turn",
                    error=str(exc),
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return GateResult(allowed=True)
        if score.overall >= config.quality_gate.threshold:
            quality_gate_retries.pop(
                (target_plan.session_id, from_phase, to_phase),
                None,
            )
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="success",
                    message=f"评分 {score.overall:.1f}/5，可以进入 Phase {to_phase}。",
                    blocking=True,
                    scope="turn",
                    result={"overall": score.overall},
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return GateResult(allowed=True)

        retry_key = (target_plan.session_id, from_phase, to_phase)
        retry_count = quality_gate_retries.get(retry_key, 0)
        if retry_count >= config.quality_gate.max_retries:
            quality_gate_retries.pop(retry_key, None)
            internal_task_events.append(
                InternalTask(
                    id=task_id,
                    kind="quality_gate",
                    label="阶段推进检查",
                    status="warning",
                    message="质量门控已达到重试上限，本次允许继续。",
                    blocking=True,
                    scope="turn",
                    result={"overall": score.overall},
                    started_at=started_at,
                    ended_at=time.time(),
                )
            )
            return GateResult(allowed=True)

        quality_gate_retries[retry_key] = retry_count + 1
        suggestions = score.suggestions or [
            "请根据当前旅行画像补强方案质量后再推进阶段。"
        ]
        suggestion_text = "\n".join(f"- {suggestion}" for suggestion in suggestions)
        feedback = (
            f"[质量门控]\n当前方案评分 {score.overall:.1f}/5，"
            f"低于阈值 {config.quality_gate.threshold:.1f}。"
            f"请修正后再进入 Phase {to_phase}：\n{suggestion_text}"
        )
        if session:
            active_runtime_messages(session).append(
                app_event_message("quality_gate", feedback)
            )
        internal_task_events.append(
            InternalTask(
                id=task_id,
                kind="quality_gate",
                label="阶段推进检查",
                status="warning",
                message=f"评分 {score.overall:.1f}/5，低于阈值 {config.quality_gate.threshold:.1f}。",
                blocking=True,
                scope="turn",
                result={"overall": score.overall, "suggestions": suggestions},
                started_at=started_at,
                ended_at=time.time(),
            )
        )
        return GateResult(allowed=False, feedback=feedback)

    hooks.register_gate("before_phase_transition", on_before_phase_transition)

    return hooks, internal_task_events
