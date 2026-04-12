# backend/tests/test_state_manager.py
import json
from pathlib import Path

import pytest

from state.manager import StateManager
from state.models import TravelPlanState, DateRange, Accommodation, DayPlan


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


@pytest.fixture
def manager(data_dir):
    return StateManager(data_dir=str(data_dir))


@pytest.mark.asyncio
async def test_create_session(manager):
    plan = await manager.create_session()
    assert plan.session_id
    assert plan.trip_id == f"trip_{plan.session_id.removeprefix('sess_')}"
    assert plan.phase == 1


@pytest.mark.asyncio
async def test_save_and_load(manager):
    plan = await manager.create_session()
    plan.trip_id = None
    plan.destination = "Kyoto"
    await manager.save(plan)

    loaded = await manager.load(plan.session_id)
    assert loaded.destination == "Kyoto"
    assert loaded.trip_id == f"trip_{plan.session_id.removeprefix('sess_')}"


@pytest.mark.asyncio
async def test_save_increments_version(manager):
    plan = await manager.create_session()
    assert plan.version == 1
    await manager.save(plan)
    assert plan.version == 2


@pytest.mark.asyncio
async def test_save_snapshot(manager):
    plan = await manager.create_session()
    plan.destination = "Tokyo"
    await manager.save(plan)

    snapshot_path = await manager.save_snapshot(plan)
    assert Path(snapshot_path).exists()

    snapshot_data = json.loads(Path(snapshot_path).read_text())
    assert snapshot_data["destination"] == "Tokyo"


@pytest.mark.asyncio
async def test_load_nonexistent_raises(manager):
    with pytest.raises(FileNotFoundError):
        await manager.load("sess_000000000000")


@pytest.mark.asyncio
async def test_load_invalid_session_id_raises(manager):
    with pytest.raises(ValueError):
        await manager.load("nonexistent")


@pytest.mark.asyncio
async def test_save_tool_result(manager):
    plan = await manager.create_session()
    data = {"flights": [{"airline": "MU", "price": 2340}]}
    path = await manager.save_tool_result(plan.session_id, "flight-search", data)
    assert Path(path).exists()
    assert json.loads(Path(path).read_text()) == data
