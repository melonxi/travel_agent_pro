# backend/tests/test_flyai_client.py
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_which_found():
    with patch("shutil.which", return_value="/usr/local/bin/flyai"):
        yield


@pytest.fixture
def mock_which_missing():
    with patch("shutil.which", return_value=None):
        yield


def _make_proc_mock(stdout_data: bytes, stderr_data: bytes = b"", returncode: int = 0):
    """Create a mock subprocess that returns given stdout/stderr."""
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout_data, stderr_data))
    proc.returncode = returncode
    return proc


@pytest.mark.asyncio
async def test_search_flight_success(mock_which_found):
    from tools.flyai_client import FlyAIClient

    payload = json.dumps(
        {
            "status": 0,
            "message": "success",
            "data": {"itemList": [{"title": "MU5101", "price": "1200"}]},
        }
    ).encode()

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc_mock(payload)):
        client = FlyAIClient(timeout=10)
        assert client.available is True
        result = await client.search_flight(
            origin="上海", destination="北京", date="2026-05-01"
        )
        assert len(result) == 1
        assert result[0]["title"] == "MU5101"


@pytest.mark.asyncio
async def test_not_installed(mock_which_missing):
    from tools.flyai_client import FlyAIClient

    client = FlyAIClient()
    assert client.available is False
    result = await client.search_flight(
        origin="上海", destination="北京", date="2026-05-01"
    )
    assert result == []


@pytest.mark.asyncio
async def test_timeout(mock_which_found):
    from tools.flyai_client import FlyAIClient

    async def slow_communicate():
        await asyncio.sleep(100)
        return (b"", b"")

    proc = AsyncMock()
    proc.communicate = slow_communicate
    proc.kill = MagicMock()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        client = FlyAIClient(timeout=0.01)
        result = await client.search_flight(
            origin="上海", destination="北京", date="2026-05-01"
        )
        assert result == []


@pytest.mark.asyncio
async def test_nonzero_status(mock_which_found):
    from tools.flyai_client import FlyAIClient

    payload = json.dumps({"status": 1, "message": "error", "data": {}}).encode()

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc_mock(payload)):
        client = FlyAIClient(timeout=10)
        result = await client.fast_search(query="杭州三日游")
        assert result == []


@pytest.mark.asyncio
async def test_empty_item_list(mock_which_found):
    from tools.flyai_client import FlyAIClient

    payload = json.dumps(
        {"status": 0, "message": "success", "data": {"itemList": []}}
    ).encode()

    with patch("asyncio.create_subprocess_exec", return_value=_make_proc_mock(payload)):
        client = FlyAIClient(timeout=10)
        result = await client.search_hotels(dest_name="东京")
        assert result == []
