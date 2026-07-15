from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def run_timeout(seconds: object):
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        yield
        return
    async with asyncio.timeout(float(seconds)):
        yield


def resolve_run_timeout_seconds(config: object, phase: object) -> object:
    """选择本轮 agent.run 的超时预算。

    Phase 3 是长时并行/串行编排，单 worker 预算即达 1200s×天数，用全局
    run_timeout 包住会在 commit 前超时丢弃全部成果。该阶段改用独立的编排预算
    (phase3_parallel.orchestration_timeout_seconds，默认 None=豁免 run 级超时)。
    """
    if phase == 3:
        return getattr(
            getattr(config, "phase3_parallel", None),
            "orchestration_timeout_seconds",
            None,
        )
    return getattr(config, "run_timeout_seconds", None)

