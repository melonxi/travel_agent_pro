import asyncio

import pytest

from types import SimpleNamespace

from api.orchestration.chat.stream import _has_frozen_phase4_deliverables
from api.orchestration.chat.stream_runtime import run_timeout


@pytest.mark.asyncio
async def test_run_timeout_raises_after_budget():
    with pytest.raises(TimeoutError):
        async with run_timeout(0.01):
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_run_timeout_can_be_disabled():
    async with run_timeout(None):
        await asyncio.sleep(0)


def test_has_frozen_phase4_deliverables_requires_phase4_and_deliverables():
    assert _has_frozen_phase4_deliverables(
        SimpleNamespace(phase=4, deliverables={"travel_plan_md": "travel_plan.md"})
    )
    assert not _has_frozen_phase4_deliverables(
        SimpleNamespace(phase=3, deliverables={"travel_plan_md": "travel_plan.md"})
    )
    assert not _has_frozen_phase4_deliverables(
        SimpleNamespace(phase=4, deliverables=None)
    )
