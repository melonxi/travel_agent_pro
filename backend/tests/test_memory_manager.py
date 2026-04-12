from __future__ import annotations

from pathlib import Path

import pytest

from config import load_config
from memory.manager import MemoryManager
from memory.models import MemoryItem, MemorySource, Rejection, UserMemory
from state.models import TravelPlanState


def make_item(**overrides):
    base = dict(
        id="mem-1",
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        value="节奏轻松",
        scope="global",
        polarity="neutral",
        confidence=0.8,
        status="active",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
    )
    base.update(overrides)
    return MemoryItem(**base)


@pytest.mark.asyncio
async def test_generate_context_includes_active_stored_memory(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
    await manager.store.upsert_item(make_item())

    text = await manager.generate_context("u1", TravelPlanState(session_id="s1"))

    assert "## 核心用户画像" in text
    assert "节奏轻松" in text


@pytest.mark.asyncio
async def test_legacy_load_returns_empty_user_memory_for_missing_user(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))

    memory = await manager.load("missing-user")

    assert memory == UserMemory(user_id="missing-user")


@pytest.mark.asyncio
async def test_legacy_load_reads_v2_legacy_envelope(tmp_path: Path):
    user_dir = tmp_path / "users" / "u1"
    user_dir.mkdir(parents=True)
    (user_dir / "memory.json").write_text(
        """
{
  "schema_version": 2,
  "user_id": "u1",
  "items": [],
  "legacy": {
    "user_id": "u1",
    "explicit_preferences": {"住宿": "民宿"},
    "implicit_preferences": {},
    "trip_history": [],
    "rejections": []
  }
}
""",
        encoding="utf-8",
    )
    manager = MemoryManager(data_dir=str(tmp_path))

    memory = await manager.load("u1")

    assert memory.explicit_preferences == {"住宿": "民宿"}


@pytest.mark.asyncio
async def test_legacy_load_reads_v2_items_when_legacy_empty(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
    await manager.store.upsert_item(
        make_item(key="preferred_pace", value="轻松", status="active")
    )

    memory = await manager.load("u1")

    assert memory.explicit_preferences == {"preferred_pace": "轻松"}


@pytest.mark.asyncio
async def test_legacy_save_preserves_v2_items(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
    await manager.store.upsert_item(
        make_item(key="preferred_pace", value="轻松", status="active")
    )

    await manager.save(
        UserMemory(
            user_id="u1",
            explicit_preferences={"住宿": "民宿"},
            rejections=[Rejection(item="红眼航班", reason="休息不好", permanent=True)],
        )
    )

    items = await manager.store.list_items("u1")
    loaded = await manager.load("u1")
    assert len(items) == 1
    assert loaded.explicit_preferences == {"住宿": "民宿"}
    assert loaded.rejections[0].item == "红眼航班"


def test_travel_plan_state_round_trips_trip_id():
    plan = TravelPlanState(session_id="s1", trip_id="trip-2026-kyoto")

    restored = TravelPlanState.from_dict(plan.to_dict())

    assert restored.trip_id == "trip-2026-kyoto"


def test_memory_config_maps_new_block_and_falls_back_to_legacy_extraction(tmp_path: Path):
    legacy_cfg = tmp_path / "legacy.yaml"
    legacy_cfg.write_text(
        """
memory_extraction:
  enabled: false
  model: legacy-memory-model
""",
        encoding="utf-8",
    )

    legacy = load_config(str(legacy_cfg))

    assert legacy.memory.extraction.enabled is False
    assert legacy.memory.extraction.model == "legacy-memory-model"

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
memory_extraction:
  enabled: false
  model: legacy-memory-model
memory:
  enabled: "true"
  extraction:
    enabled: "true"
    model: structured-memory-model
    trigger: each_turn
    max_user_messages: 4
  policy:
    auto_save_low_risk: "false"
    auto_save_medium_risk: "true"
    require_confirmation_for_high_risk: "false"
  retrieval:
    core_limit: 5
    phase_limit: 3
    include_pending: "true"
  storage:
    backend: json
telemetry:
  enabled: "false"
flyai:
  enabled: "false"
xhs:
  enabled: "false"
guardrails:
  enabled: "false"
parallel_tool_execution: "false"
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_file))

    assert cfg.memory.enabled is True
    assert cfg.memory.extraction.enabled is True
    assert cfg.memory.extraction.model == "structured-memory-model"
    assert cfg.memory.extraction.trigger == "each_turn"
    assert cfg.memory.extraction.max_user_messages == 4
    assert cfg.memory.policy.auto_save_low_risk is False
    assert cfg.memory.policy.auto_save_medium_risk is True
    assert cfg.memory.policy.require_confirmation_for_high_risk is False
    assert cfg.memory.retrieval.core_limit == 5
    assert cfg.memory.retrieval.phase_limit == 3
    assert cfg.memory.retrieval.include_pending is True
    assert cfg.memory.storage.backend == "json"
    assert cfg.telemetry.enabled is False
    assert cfg.flyai.enabled is False
    assert cfg.xhs.enabled is False
    assert cfg.guardrails.enabled is False
    assert cfg.parallel_tool_execution is False
