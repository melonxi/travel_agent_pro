# backend/tests/test_memory.py
import json
from pathlib import Path

import pytest

from memory.models import UserMemory, Rejection, TripSummary
from memory.manager import MemoryManager


def test_user_memory_defaults():
    mem = UserMemory(user_id="u1")
    assert mem.explicit_preferences == {}
    assert mem.rejections == []
    assert mem.trip_history == []


def test_user_memory_serialization():
    mem = UserMemory(
        user_id="u1",
        explicit_preferences={"no_red_eye": True},
        rejections=[
            Rejection(item="Hotel A", reason="太远", permanent=False, context="Tokyo")
        ],
        trip_history=[
            TripSummary(
                destination="Kyoto", dates="2025-10", satisfaction=4, notes="不错"
            )
        ],
    )
    d = mem.to_dict()
    restored = UserMemory.from_dict(d)
    assert restored.explicit_preferences["no_red_eye"] is True
    assert restored.rejections[0].item == "Hotel A"
    assert not restored.rejections[0].permanent


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(data_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_save_and_load(manager):
    mem = UserMemory(user_id="u1", explicit_preferences={"pace": "relaxed"})
    await manager.save(mem)
    loaded = await manager.load("u1")
    assert loaded.explicit_preferences["pace"] == "relaxed"


@pytest.mark.asyncio
async def test_load_nonexistent_returns_empty(manager):
    mem = await manager.load("u_new")
    assert mem.user_id == "u_new"
    assert mem.explicit_preferences == {}


@pytest.mark.asyncio
async def test_generate_summary(manager):
    mem = UserMemory(
        user_id="u1",
        explicit_preferences={"no_red_eye": True, "private_bathroom": True},
        trip_history=[
            TripSummary(
                destination="Kyoto", dates="2025-10", satisfaction=4, notes="节奏好"
            )
        ],
        rejections=[Rejection(item="红眼航班", reason="不坐", permanent=True)],
    )
    summary = manager.generate_summary(mem)
    assert "红眼" in summary or "no_red_eye" in summary
    assert "Kyoto" in summary
