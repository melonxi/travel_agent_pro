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


@pytest.mark.asyncio
async def test_save_and_read_deliverable(manager):
    plan = await manager.create_session()
    path = await manager.save_deliverable(
        plan.session_id,
        "travel_plan.md",
        "# 东京 5 日旅行计划\n",
    )

    assert Path(path).exists()
    assert await manager.read_deliverable(plan.session_id, "travel_plan.md") == (
        "# 东京 5 日旅行计划\n"
    )


@pytest.mark.asyncio
async def test_save_deliverable_rejects_non_whitelisted_name(manager):
    plan = await manager.create_session()

    with pytest.raises(ValueError):
        await manager.save_deliverable(plan.session_id, "../etc/passwd", "x")

    with pytest.raises(ValueError):
        await manager.read_deliverable(plan.session_id, "notes.txt")


@pytest.mark.asyncio
async def test_clear_deliverables_is_idempotent(manager):
    plan = await manager.create_session()
    await manager.save_deliverable(plan.session_id, "travel_plan.md", "# plan\n")
    await manager.save_deliverable(plan.session_id, "checklist.md", "# list\n")

    await manager.clear_deliverables(plan.session_id)
    await manager.clear_deliverables(plan.session_id)

    deliverables_dir = Path(manager._session_dir(plan.session_id)) / "deliverables"
    assert not (deliverables_dir / "travel_plan.md").exists()
    assert not (deliverables_dir / "checklist.md").exists()


@pytest.mark.asyncio
async def test_save_and_read_deliverable(manager):
    plan = await manager.create_session()
    path = await manager.save_deliverable(
        plan.session_id,
        "travel_plan.md",
        "# 东京 5 日旅行计划\n",
    )

    assert Path(path).exists()
    assert await manager.read_deliverable(plan.session_id, "travel_plan.md") == (
        "# 东京 5 日旅行计划\n"
    )


@pytest.mark.asyncio
async def test_save_deliverable_rejects_non_whitelisted_name(manager):
    plan = await manager.create_session()

    with pytest.raises(ValueError):
        await manager.save_deliverable(plan.session_id, "../etc/passwd", "x")

    with pytest.raises(ValueError):
        await manager.read_deliverable(plan.session_id, "notes.txt")


@pytest.mark.asyncio
async def test_clear_deliverables_is_idempotent(manager):
    plan = await manager.create_session()
    await manager.save_deliverable(plan.session_id, "travel_plan.md", "# plan\n")
    await manager.save_deliverable(plan.session_id, "checklist.md", "# list\n")

    await manager.clear_deliverables(plan.session_id)
    await manager.clear_deliverables(plan.session_id)

    deliverables_dir = Path(manager._session_dir(plan.session_id)) / "deliverables"
    assert not (deliverables_dir / "travel_plan.md").exists()
    assert not (deliverables_dir / "checklist.md").exists()


@pytest.mark.asyncio
async def test_save_and_read_deliverable(manager):
    plan = await manager.create_session()
    path = await manager.save_deliverable(
        plan.session_id,
        "travel_plan.md",
        "# 东京 5 日旅行计划\n",
    )

    assert Path(path).exists()
    assert await manager.read_deliverable(plan.session_id, "travel_plan.md") == (
        "# 东京 5 日旅行计划\n"
    )


@pytest.mark.asyncio
async def test_save_deliverable_rejects_non_whitelisted_name(manager):
    plan = await manager.create_session()

    with pytest.raises(ValueError):
        await manager.save_deliverable(plan.session_id, "../etc/passwd", "x")

    with pytest.raises(ValueError):
        await manager.read_deliverable(plan.session_id, "notes.txt")


@pytest.mark.asyncio
async def test_clear_deliverables_is_idempotent(manager):
    plan = await manager.create_session()
    await manager.save_deliverable(plan.session_id, "travel_plan.md", "# plan\n")
    await manager.save_deliverable(plan.session_id, "checklist.md", "# list\n")

    await manager.clear_deliverables(plan.session_id)
    await manager.clear_deliverables(plan.session_id)

    deliverables_dir = Path(manager._session_dir(plan.session_id)) / "deliverables"
    assert not (deliverables_dir / "travel_plan.md").exists()
    assert not (deliverables_dir / "checklist.md").exists()
