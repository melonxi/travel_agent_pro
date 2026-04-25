from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from agent.internal_tasks import InternalTask
from agent.types import Message, Role
from memory.async_jobs import build_gate_user_window
from memory.extraction import (
    V3ExtractionResult,
    build_v3_extraction_gate_prompt,
    build_v3_extraction_gate_tool,
    build_v3_extraction_prompt,
    build_v3_extraction_tool,
    build_v3_profile_extraction_prompt,
    build_v3_profile_extraction_tool,
    build_v3_working_memory_extraction_prompt,
    build_v3_working_memory_extraction_tool,
    parse_v3_extraction_gate_tool_arguments,
    parse_v3_extraction_tool_arguments,
    parse_v3_profile_extraction_tool_arguments,
    parse_v3_working_memory_extraction_tool_arguments,
)
from memory.policy import MemoryPolicy
from memory.profile_normalization import (
    merge_profile_item_with_existing,
    normalize_profile_item,
)
from memory.v3_models import generate_profile_item_id
from state.models import TravelPlanState

from api.orchestration.memory.contracts import (
    MemoryExtractionGateDecision,
    MemoryExtractionOutcome,
    MemoryExtractionProgress,
    MemoryRouteSaveProgress,
)

logger = logging.getLogger(__name__)


@dataclass
class MemoryExtractionRuntime:
    decide_memory_extraction: Callable[..., Any]
    extract_memory_candidates: Callable[..., Any]


def create_memory_extraction_runtime(
    *,
    config: Any,
    memory_mgr: Any,
    create_llm_provider_func: Callable[[Any], Any],
    collect_forced_tool_call_arguments: Callable[..., Any],
    build_memory_prompt_summary: Callable[..., Any],
    memory_plan_facts: Callable[[TravelPlanState], dict[str, Any]],
    publish_memory_task: Callable[[str, InternalTask], None],
    now_iso: Callable[[], str],
) -> MemoryExtractionRuntime:
    _GATE_MAX_USER_MESSAGES = 3
    _GATE_MAX_CHARS = 1200
    _EXTRACTION_TIMEOUT_SECONDS = 40.0
    _EXTRACTION_GATE_TIMEOUT_SECONDS = 30.0

    async def _decide_memory_extraction(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
    ) -> MemoryExtractionGateDecision:
        if not config.memory.enabled or not config.memory.extraction.enabled:
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="disabled",
                message="记忆提取未启用",
            )
        if config.memory.extraction.trigger != "each_turn":
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="trigger_not_matched",
                message="当前提取策略未在本轮触发",
            )
        if not user_messages:
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="no_user_messages",
                message="本轮没有可提取的用户消息",
            )

        gate_window = build_gate_user_window(
            user_messages=user_messages,
            max_messages=_GATE_MAX_USER_MESSAGES,
            max_chars=_GATE_MAX_CHARS,
        )
        if not gate_window:
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="no_user_messages",
                message="本轮没有可提取的用户消息",
            )

        memory_summary = await build_memory_prompt_summary(
            user_id=user_id,
            session_id=session_id,
            plan_snapshot=plan_snapshot,
        )
        prompt = build_v3_extraction_gate_prompt(
            user_messages=gate_window,
            plan_facts=memory_plan_facts(plan_snapshot),
            existing_memory_summary=memory_summary,
        )
        gate_llm = create_llm_provider_func(config.llm)
        logger.warning(
            "记忆提取判定开始调用模型 session=%s user=%s model=%s prompt_chars=%s user_messages=%s",
            session_id,
            user_id,
            config.llm.model,
            len(prompt),
            len(gate_window),
        )
        try:
            tool_args = await asyncio.wait_for(
                collect_forced_tool_call_arguments(
                    gate_llm,
                    messages=[Message(role=Role.USER, content=prompt)],
                    tool_def=build_v3_extraction_gate_tool(),
                ),
                timeout=_EXTRACTION_GATE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "记忆提取判定超时 session=%s user=%s timeout_seconds=%s",
                session_id,
                user_id,
                _EXTRACTION_GATE_TIMEOUT_SECONDS,
            )
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="timeout",
                message="记忆提取判定超时，已跳过本轮提取。",
            )
        except Exception:
            logger.exception(
                "记忆提取判定失败 session=%s user=%s",
                session_id,
                user_id,
            )
            return MemoryExtractionGateDecision(
                should_extract=False,
                reason="error",
                message="记忆提取判定失败，已跳过本轮提取。",
                error="记忆提取判定异常，请检查后端日志。",
            )

        decision = parse_v3_extraction_gate_tool_arguments(tool_args)
        if not decision.reason:
            decision.reason = (
                "memory_routes_detected"
                if decision.should_extract
                else "no_memory_routes"
            )
        if not decision.message:
            decision.message = (
                "检测到需要提取的记忆信号"
                if decision.should_extract
                else "本轮未发现需要提取的长期画像或工作记忆信号"
            )
        routes = {
            "profile": decision.routes.profile,
            "working_memory": decision.routes.working_memory,
        }
        logger.warning(
            "记忆提取判定完成 session=%s user=%s should_extract=%s reason=%s routes=%s",
            session_id,
            user_id,
            decision.should_extract,
            decision.reason,
            routes,
        )
        return MemoryExtractionGateDecision(
            should_extract=decision.should_extract,
            reason=decision.reason,
            message=decision.message,
            routes=routes,
        )

    async def _extract_memory_candidates(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
        routes: dict[str, bool] | None = None,
        turn_id: str | None = None,
    ) -> MemoryExtractionOutcome:
        if not config.memory.enabled or not config.memory.extraction.enabled:
            return MemoryExtractionOutcome(
                status="skipped",
                message="记忆提取未启用",
                item_ids=[],
                reason="disabled",
            )
        if config.memory.extraction.trigger != "each_turn":
            return MemoryExtractionOutcome(
                status="skipped",
                message="当前提取策略未在本轮触发",
                item_ids=[],
                reason="trigger_not_matched",
            )
        if not user_messages:
            return MemoryExtractionOutcome(
                status="skipped",
                message="本轮没有可提取的用户消息",
                item_ids=[],
                reason="no_user_messages",
            )

        progress = MemoryExtractionProgress()
        try:
            return await asyncio.wait_for(
                _do_extract_memory_candidates(
                    session_id=session_id,
                    user_id=user_id,
                    user_messages=user_messages,
                    plan_snapshot=plan_snapshot,
                    routes=routes,
                    turn_id=turn_id,
                    progress=progress,
                ),
                timeout=_EXTRACTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "记忆提取超时 session=%s user=%s timeout_seconds=%s",
                session_id,
                user_id,
                _EXTRACTION_TIMEOUT_SECONDS,
            )
            message = "记忆提取超时，本轮未写入记忆。"
            if progress.saved_total > 0:
                message = "记忆提取超时，已保留部分写入结果，剩余内容将稍后重试。"
            return MemoryExtractionOutcome(
                status="warning",
                message=message,
                item_ids=list(progress.pending_ids),
                saved_profile_count=progress.saved_profile_count,
                saved_working_count=progress.saved_working_count,
                reason="timeout",
            )
        except Exception:
            logger.exception(
                "记忆提取失败 session=%s user=%s",
                session_id,
                user_id,
            )
            return MemoryExtractionOutcome(
                status="error",
                message="记忆提取失败，本轮未写入记忆。",
                item_ids=[],
                reason="error",
                error="记忆提取异常，请检查后端日志。",
            )

    async def _extract_combined_memory_items(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
        profile: Any,
        working_memory: Any,
    ) -> V3ExtractionResult:
        prompt = build_v3_extraction_prompt(
            user_messages=user_messages,
            profile=profile,
            working_memory=working_memory,
            plan_facts=memory_plan_facts(plan_snapshot),
        )
        extraction_llm = create_llm_provider_func(config.llm)
        logger.warning(
            "兼容记忆提取开始调用模型 session=%s user=%s model=%s prompt_chars=%s user_messages=%s",
            session_id,
            user_id,
            config.llm.model,
            len(prompt),
            len(user_messages),
        )
        tool_args = await collect_forced_tool_call_arguments(
            extraction_llm,
            messages=[Message(role=Role.USER, content=prompt)],
            tool_def=build_v3_extraction_tool(),
        )
        logger.warning(
            "兼容记忆提取模型返回 session=%s user=%s has_arguments=%s argument_keys=%s",
            session_id,
            user_id,
            bool(tool_args),
            sorted(tool_args.keys()) if isinstance(tool_args, dict) else [],
        )
        return parse_v3_extraction_tool_arguments(tool_args)

    async def _extract_profile_memory_items(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
        profile: Any,
    ) -> V3ExtractionResult:
        prompt = build_v3_profile_extraction_prompt(
            user_messages=user_messages,
            profile=profile,
            plan_facts=memory_plan_facts(plan_snapshot),
        )
        extraction_llm = create_llm_provider_func(config.llm)
        logger.warning(
            "长期画像记忆提取开始调用模型 session=%s user=%s model=%s prompt_chars=%s user_messages=%s",
            session_id,
            user_id,
            config.llm.model,
            len(prompt),
            len(user_messages),
        )
        tool_args = await collect_forced_tool_call_arguments(
            extraction_llm,
            messages=[Message(role=Role.USER, content=prompt)],
            tool_def=build_v3_profile_extraction_tool(),
        )
        logger.warning(
            "长期画像记忆提取模型返回 session=%s user=%s has_arguments=%s argument_keys=%s",
            session_id,
            user_id,
            bool(tool_args),
            sorted(tool_args.keys()) if isinstance(tool_args, dict) else [],
        )
        return parse_v3_profile_extraction_tool_arguments(tool_args)

    async def _extract_working_memory_items(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
        working_memory: Any,
    ) -> V3ExtractionResult:
        prompt = build_v3_working_memory_extraction_prompt(
            user_messages=user_messages,
            working_memory=working_memory,
            plan_facts=memory_plan_facts(plan_snapshot),
        )
        extraction_llm = create_llm_provider_func(config.llm)
        logger.warning(
            "工作记忆提取开始调用模型 session=%s user=%s model=%s prompt_chars=%s user_messages=%s",
            session_id,
            user_id,
            config.llm.model,
            len(prompt),
            len(user_messages),
        )
        tool_args = await collect_forced_tool_call_arguments(
            extraction_llm,
            messages=[Message(role=Role.USER, content=prompt)],
            tool_def=build_v3_working_memory_extraction_tool(),
        )
        logger.warning(
            "工作记忆提取模型返回 session=%s user=%s has_arguments=%s argument_keys=%s",
            session_id,
            user_id,
            bool(tool_args),
            sorted(tool_args.keys()) if isinstance(tool_args, dict) else [],
        )
        return parse_v3_working_memory_extraction_tool_arguments(tool_args)

    def _count_profile_updates(profile_updates: Any) -> int:
        return sum(
            len(items)
            for items in (
                profile_updates.constraints,
                profile_updates.rejections,
                profile_updates.stable_preferences,
                profile_updates.preference_hypotheses,
            )
        )

    async def _save_profile_updates(
        *,
        user_id: str,
        profile_updates: Any,
        policy: MemoryPolicy,
        now: str,
        route_progress: MemoryRouteSaveProgress,
        aggregate_progress: MemoryExtractionProgress,
    ) -> None:
        def _profile_items_match(
            comparison_bucket: str,
            existing_item: Any,
            incoming_item: Any,
        ) -> bool:
            if comparison_bucket in {"constraints", "stable_preferences"}:
                return (
                    existing_item.domain == incoming_item.domain
                    and existing_item.key == incoming_item.key
                )
            return (
                existing_item.domain == incoming_item.domain
                and existing_item.key == incoming_item.key
                and existing_item.value == incoming_item.value
            )

        def _candidate_profile_buckets(bucket_name: str) -> tuple[str, ...]:
            if bucket_name == "preference_hypotheses":
                return ("preference_hypotheses", "stable_preferences")
            return (bucket_name,)

        def _find_matching_profile_item_location(
            comparison_bucket: str,
            candidate_buckets: tuple[str, ...],
            incoming_item: Any,
        ) -> tuple[str, int] | None:
            for candidate_bucket in candidate_buckets:
                candidate_items = getattr(profile, candidate_bucket)
                for index, existing_item in enumerate(candidate_items):
                    if _profile_items_match(
                        comparison_bucket, existing_item, incoming_item
                    ):
                        return candidate_bucket, index
            return None

        def _upsert_profile_item_in_memory(bucket_name: str, item: Any) -> None:
            bucket_items = getattr(profile, bucket_name)
            for index, existing_item in enumerate(bucket_items):
                if existing_item.id == item.id:
                    bucket_items[index] = item
                    break
            else:
                bucket_items.append(item)

        profile = await memory_mgr.v3_store.load_profile(user_id)
        buckets = (
            ("constraints", profile_updates.constraints),
            ("rejections", profile_updates.rejections),
            ("stable_preferences", profile_updates.stable_preferences),
            ("preference_hypotheses", profile_updates.preference_hypotheses),
        )
        for bucket, items in buckets:
            for raw_item in items:
                normalized = normalize_profile_item(bucket, raw_item)
                match_location = _find_matching_profile_item_location(
                    bucket,
                    _candidate_profile_buckets(bucket),
                    normalized,
                )
                matched_bucket_name: str | None = None
                matched_index: int | None = None
                if match_location is not None:
                    matched_bucket_name, matched_index = match_location
                existing_items = (
                    [getattr(profile, matched_bucket_name)[matched_index]]
                    if matched_bucket_name is not None and matched_index is not None
                    else []
                )
                merged_bucket, merged_item = merge_profile_item_with_existing(
                    bucket,
                    normalized,
                    existing_items,
                )
                action = policy.classify_v3_profile_item(merged_bucket, merged_item)
                if action == "drop":
                    continue
                sanitized = policy.sanitize_v3_profile_item(merged_item)
                sanitized.status = action
                sanitized.updated_at = now
                if not sanitized.created_at:
                    sanitized.created_at = now
                sanitized.id = generate_profile_item_id(merged_bucket, sanitized)
                if (
                    matched_bucket_name is not None
                    and matched_index is not None
                    and matched_bucket_name != merged_bucket
                ):
                    del getattr(profile, matched_bucket_name)[matched_index]
                    await memory_mgr.v3_store.save_profile(profile)
                await memory_mgr.v3_store.upsert_profile_item(
                    user_id, merged_bucket, sanitized
                )
                _upsert_profile_item_in_memory(merged_bucket, sanitized)
                route_progress.saved_count += 1
                aggregate_progress.saved_profile_count += 1
                if action in {"pending", "pending_conflict"}:
                    route_progress.pending_ids.append(sanitized.id)
                    aggregate_progress.pending_ids.append(sanitized.id)

    async def _save_working_memory_items(
        *,
        user_id: str,
        session_id: str,
        plan_snapshot: TravelPlanState,
        working_memory_items: list[Any],
        policy: MemoryPolicy,
        now: str,
        route_progress: MemoryRouteSaveProgress,
        aggregate_progress: MemoryExtractionProgress,
    ) -> None:
        for raw_working_item in working_memory_items:
            sanitized_working = policy.sanitize_working_memory_item(raw_working_item)
            if not sanitized_working.created_at:
                sanitized_working.created_at = now
            await memory_mgr.v3_store.upsert_working_memory_item(
                user_id,
                session_id,
                plan_snapshot.trip_id,
                sanitized_working,
            )
            route_progress.saved_count += 1
            aggregate_progress.saved_working_count += 1

    def _publish_split_memory_task(
        *,
        session_id: str,
        task_id: str,
        kind: str,
        label: str,
        status: str,
        message: str,
        started_at: float,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        ended_at: float | None = None,
    ) -> None:
        publish_memory_task(
            session_id,
            InternalTask(
                id=task_id,
                kind=kind,
                label=label,
                status=status,
                message=message,
                blocking=False,
                scope="background",
                result=result,
                error=error,
                started_at=started_at,
                ended_at=ended_at,
            ),
        )

    async def _do_extract_memory_candidates(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
        routes: dict[str, bool] | None = None,
        turn_id: str | None = None,
        progress: MemoryExtractionProgress | None = None,
    ) -> MemoryExtractionOutcome:
        route_flags = routes or {"profile": True, "working_memory": True}
        run_profile = bool(route_flags.get("profile"))
        run_working = bool(route_flags.get("working_memory"))
        if not run_profile and not run_working:
            return MemoryExtractionOutcome(
                status="skipped",
                message="本轮没有新的可复用记忆",
                item_ids=[],
                reason="no_routes",
            )

        profile = await memory_mgr.v3_store.load_profile(user_id)
        working_memory = await memory_mgr.v3_store.load_working_memory(
            user_id, session_id, plan_snapshot.trip_id
        )
        logger.warning(
            "记忆提取路由开始 session=%s user=%s routes=%s user_messages=%s",
            session_id,
            user_id,
            route_flags,
            len(user_messages),
        )

        policy = MemoryPolicy(
            auto_save_low_risk=config.memory.policy.auto_save_low_risk,
            auto_save_medium_risk=config.memory.policy.auto_save_medium_risk,
        )
        now = now_iso()
        aggregate_progress = progress or MemoryExtractionProgress()
        parsed_profile_count = 0
        parsed_working_count = 0
        route_failures: list[tuple[str, str]] = []
        task_turn_id = turn_id or str(uuid.uuid4())

        if run_profile:
            profile_progress = MemoryRouteSaveProgress()
            profile_task_id = f"profile_memory_extraction:{session_id}:{task_turn_id}"
            profile_started_at = time.time()
            _publish_split_memory_task(
                session_id=session_id,
                task_id=profile_task_id,
                kind="profile_memory_extraction",
                label="长期画像提取",
                status="pending",
                message="正在提取长期画像记忆…",
                started_at=profile_started_at,
            )
            try:
                profile_result = await _extract_profile_memory_items(
                    session_id=session_id,
                    user_id=user_id,
                    user_messages=user_messages,
                    plan_snapshot=plan_snapshot,
                    profile=profile,
                )
                parsed_profile_count = _count_profile_updates(
                    profile_result.profile_updates
                )
                await _save_profile_updates(
                    user_id=user_id,
                    profile_updates=profile_result.profile_updates,
                    policy=policy,
                    now=now,
                    route_progress=profile_progress,
                    aggregate_progress=aggregate_progress,
                )
                profile_status = (
                    "success" if profile_progress.saved_count > 0 else "skipped"
                )
                profile_message = (
                    f"已保存 {profile_progress.saved_count} 条长期画像记忆"
                    if profile_progress.saved_count > 0
                    else "本轮没有新的长期画像记忆"
                )
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=profile_task_id,
                    kind="profile_memory_extraction",
                    label="长期画像提取",
                    status=profile_status,
                    message=profile_message,
                    result={
                        "saved_profile_count": profile_progress.saved_count,
                        "pending_profile_count": len(profile_progress.pending_ids),
                        "pending_profile_ids": list(profile_progress.pending_ids),
                        "parsed_profile_count": parsed_profile_count,
                    },
                    started_at=profile_started_at,
                    ended_at=time.time(),
                )
            except asyncio.CancelledError:
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=profile_task_id,
                    kind="profile_memory_extraction",
                    label="长期画像提取",
                    status="warning",
                    message="长期画像提取已取消，未完成部分将稍后重试。",
                    result={
                        "saved_profile_count": profile_progress.saved_count,
                        "pending_profile_count": len(profile_progress.pending_ids),
                        "pending_profile_ids": list(profile_progress.pending_ids),
                        "parsed_profile_count": parsed_profile_count,
                    },
                    error="profile_memory_extraction_cancelled",
                    started_at=profile_started_at,
                    ended_at=time.time(),
                )
                raise
            except Exception:
                logger.exception(
                    "长期画像记忆提取失败 session=%s user=%s",
                    session_id,
                    user_id,
                )
                route_failures.append(("profile", "profile_memory_extraction_failed"))
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=profile_task_id,
                    kind="profile_memory_extraction",
                    label="长期画像提取",
                    status="error",
                    message="长期画像提取失败，本轮将稍后重试。",
                    result={
                        "saved_profile_count": profile_progress.saved_count,
                        "pending_profile_count": len(profile_progress.pending_ids),
                        "pending_profile_ids": list(profile_progress.pending_ids),
                        "parsed_profile_count": parsed_profile_count,
                    },
                    error="profile_memory_extraction_failed",
                    started_at=profile_started_at,
                    ended_at=time.time(),
                )
        if run_working:
            working_progress = MemoryRouteSaveProgress()
            working_task_id = f"working_memory_extraction:{session_id}:{task_turn_id}"
            working_started_at = time.time()
            _publish_split_memory_task(
                session_id=session_id,
                task_id=working_task_id,
                kind="working_memory_extraction",
                label="工作记忆提取",
                status="pending",
                message="正在提取工作记忆…",
                started_at=working_started_at,
            )
            try:
                working_result = await _extract_working_memory_items(
                    session_id=session_id,
                    user_id=user_id,
                    user_messages=user_messages,
                    plan_snapshot=plan_snapshot,
                    working_memory=working_memory,
                )
                parsed_working_count = len(working_result.working_memory)
                await _save_working_memory_items(
                    user_id=user_id,
                    session_id=session_id,
                    plan_snapshot=plan_snapshot,
                    working_memory_items=working_result.working_memory,
                    policy=policy,
                    now=now,
                    route_progress=working_progress,
                    aggregate_progress=aggregate_progress,
                )
                working_status = (
                    "success" if working_progress.saved_count > 0 else "skipped"
                )
                working_message = (
                    f"已保存 {working_progress.saved_count} 条工作记忆"
                    if working_progress.saved_count > 0
                    else "本轮没有新的工作记忆"
                )
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=working_task_id,
                    kind="working_memory_extraction",
                    label="工作记忆提取",
                    status=working_status,
                    message=working_message,
                    result={
                        "saved_working_count": working_progress.saved_count,
                        "parsed_working_count": parsed_working_count,
                    },
                    started_at=working_started_at,
                    ended_at=time.time(),
                )
            except asyncio.CancelledError:
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=working_task_id,
                    kind="working_memory_extraction",
                    label="工作记忆提取",
                    status="warning",
                    message="工作记忆提取已取消，未完成部分将稍后重试。",
                    result={
                        "saved_working_count": working_progress.saved_count,
                        "parsed_working_count": parsed_working_count,
                    },
                    error="working_memory_extraction_cancelled",
                    started_at=working_started_at,
                    ended_at=time.time(),
                )
                raise
            except Exception:
                logger.exception(
                    "工作记忆提取失败 session=%s user=%s",
                    session_id,
                    user_id,
                )
                route_failures.append(
                    ("working_memory", "working_memory_extraction_failed")
                )
                _publish_split_memory_task(
                    session_id=session_id,
                    task_id=working_task_id,
                    kind="working_memory_extraction",
                    label="工作记忆提取",
                    status="error",
                    message="工作记忆提取失败，本轮将稍后重试。",
                    result={
                        "saved_working_count": working_progress.saved_count,
                        "parsed_working_count": parsed_working_count,
                    },
                    error="working_memory_extraction_failed",
                    started_at=working_started_at,
                    ended_at=time.time(),
                )

        if parsed_profile_count == 0 and parsed_working_count == 0 and not route_failures:
            logger.warning(
                "记忆提取未产生任何结构化结果 session=%s user=%s routes=%s",
                session_id,
                user_id,
                route_flags,
            )
        else:
            logger.warning(
                "记忆提取解析完成 session=%s user=%s profile_items=%s working_items=%s",
                session_id,
                user_id,
                parsed_profile_count,
                parsed_working_count,
            )

        saved_total = (
            aggregate_progress.saved_profile_count
            + aggregate_progress.saved_working_count
        )
        if route_failures:
            failure_errors = [failure_error for _, failure_error in route_failures]
            error = (
                failure_errors[0]
                if len(failure_errors) == 1
                else "multiple_memory_extraction_routes_failed"
            )
            return MemoryExtractionOutcome(
                status="warning",
                message="部分记忆提取失败，本轮将稍后重试。",
                item_ids=list(aggregate_progress.pending_ids),
                saved_profile_count=aggregate_progress.saved_profile_count,
                saved_working_count=aggregate_progress.saved_working_count,
                reason="partial_failure",
                error=error,
            )

        if saved_total == 0:
            return MemoryExtractionOutcome(
                status="skipped",
                message="本轮没有新的可复用记忆",
                item_ids=[],
                reason="no_structured_result",
            )

        pending_count = len(aggregate_progress.pending_ids)
        if pending_count == 0:
            message = f"已提取 {saved_total} 条记忆"
        elif pending_count == saved_total:
            message = f"已提取 {pending_count} 条待确认记忆"
        else:
            message = f"已提取 {saved_total} 条记忆，其中 {pending_count} 条待确认"

        return MemoryExtractionOutcome(
            status="success",
            message=message,
            item_ids=list(aggregate_progress.pending_ids),
            saved_profile_count=aggregate_progress.saved_profile_count,
            saved_working_count=aggregate_progress.saved_working_count,
            reason="saved",
        )

    return MemoryExtractionRuntime(
        decide_memory_extraction=_decide_memory_extraction,
        extract_memory_candidates=_extract_memory_candidates,
    )
