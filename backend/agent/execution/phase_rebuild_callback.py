"""Phase rebuild callback invocation helper.

Extracted from agent/loop.py to keep that file under the size guard threshold
while keeping the callback flush semantics close to the rebuild call sites.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from agent.types import Message

logger = logging.getLogger(__name__)


async def invoke_phase_rebuild_callback(
    callback: Callable[..., Awaitable[None]] | None,
    *,
    messages: list[Message],
    from_phase: int,
    from_step: str | None,
) -> None:
    """Flush messages with pre-rebuild phase tag. Failures are non-fatal.

    Note: callback receives a shallow copy of messages; mutating that list does
    not affect the subsequent rebuild. Message objects themselves are not
    deep-copied.
    """
    if callback is None:
        return
    try:
        await callback(
            messages=list(messages),
            from_phase=from_phase,
            from_step=from_step,
        )
    except Exception as exc:  # noqa: BLE001 - persistence failures must not block rebuild
        logger.warning(
            "on_phase_rebuild callback failed (from_phase=%s, from_step=%s, n_messages=%d): %s",
            from_phase,
            from_step,
            len(messages),
            exc,
            exc_info=True,
        )
