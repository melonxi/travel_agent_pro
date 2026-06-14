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
