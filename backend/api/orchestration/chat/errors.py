from __future__ import annotations

import asyncio

from api.orchestration.chat.events import event_json


def is_retryable_stream_error(exc: Exception) -> bool:
    network_errors: tuple[type[BaseException], ...] = (
        ConnectionError,
        TimeoutError,
        asyncio.TimeoutError,
    )
    try:
        import httpx

        network_errors += (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.ConnectError,
            httpx.ReadError,
        )
    except ImportError:
        pass
    return isinstance(exc, network_errors)


def agent_stream_error_event(exc: Exception, *, retryable: bool) -> str:
    return event_json(
        {
            "type": "error",
            "error_code": "AGENT_STREAM_ERROR",
            "retryable": retryable,
            "can_continue": False,
            "message": (
                "网络连接异常，请稍后重试。"
                if retryable
                else "系统内部错误，请稍后重试。"
            ),
            "error": str(exc),
        }
    )
