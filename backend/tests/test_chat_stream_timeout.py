import asyncio

import pytest

from api.orchestration.chat.stream import _run_timeout


@pytest.mark.asyncio
async def test_run_timeout_raises_after_budget():
    with pytest.raises(TimeoutError):
        async with _run_timeout(0.01):
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_run_timeout_can_be_disabled():
    async with _run_timeout(None):
        await asyncio.sleep(0)
