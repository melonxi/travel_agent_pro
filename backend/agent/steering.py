"""D4: 运行中引导 (steering) — queue drain 与 day 目标解析。

跨请求信号挂在 session["_steer_queue"]，与 cancel_event 同构。
在安全点 drain 后注入 runtime notice / worker repair_hint，不插进
tool_calls 与 tool 响应之间。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from agent.tagged_context import runtime_notice_message
from agent.types import Message
from llm.types import ChunkType, LLMChunk

logger = logging.getLogger(__name__)

# R4：单次 drain 只保留最近 N 条，丢弃更旧的并打 log。
MAX_STEER_KEEP = 5

_DAY_RE = re.compile(r"第\s*(\d+)\s*天")


def drain_steer_queue(
    queue: asyncio.Queue | None,
    *,
    max_keep: int = MAX_STEER_KEEP,
) -> list[str]:
    """非阻塞 drain；空队列返回 []。洪水时只保留最近 max_keep 条。"""
    if queue is None:
        return []
    collected: list[str] = []
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        text = _normalize_steer_item(item)
        if text:
            collected.append(text)
    if not collected:
        return []
    if len(collected) > max_keep:
        dropped = len(collected) - max_keep
        logger.warning(
            "Steering flood: dropped %d older message(s), keeping last %d",
            dropped,
            max_keep,
        )
        collected = collected[-max_keep:]
    return collected


def _normalize_steer_item(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        text = item.get("text")
        if isinstance(text, str):
            return text.strip()
    return str(item).strip() if item is not None else ""


def parse_steer_day_targets(text: str) -> list[int]:
    """从引导文案解析「第 N 天」目标；无匹配返回空列表。"""
    days: list[int] = []
    for match in _DAY_RE.finditer(text or ""):
        day = int(match.group(1))
        if day > 0 and day not in days:
            days.append(day)
    return days


def format_steer_notice(text: str) -> str:
    return f"[用户运行中引导] {text}"


def inject_steer_into_messages(messages: list[Message], texts: list[str]) -> int:
    """在安全点把 steering 注入 runtime messages（runtime_notice）。"""
    for text in texts:
        messages.append(
            runtime_notice_message("steering", format_steer_notice(text))
        )
    return len(texts)


def steering_ack_chunk(text: str, *, days: list[int] | None = None) -> LLMChunk:
    payload: dict[str, Any] = {
        "stage": "steering_ack",
        "message": "已收到引导，将在下一步调整",
        "text": text,
    }
    if days:
        payload["days"] = days
    return LLMChunk(type=ChunkType.AGENT_STATUS, agent_status=payload)


def make_steer_envelope(text: str) -> dict[str, Any]:
    return {"text": text, "ts": time.time()}


def drain_and_inject_steering(
    queue: asyncio.Queue | None,
    messages: list[Message],
) -> list[LLMChunk]:
    """AgentLoop / 其他调用方共用：drain + 注入 messages + 返回 ack chunks。"""
    texts = drain_steer_queue(queue)
    if not texts:
        return []
    inject_steer_into_messages(messages, texts)
    return [
        steering_ack_chunk(t, days=parse_steer_day_targets(t) or None)
        for t in texts
    ]
