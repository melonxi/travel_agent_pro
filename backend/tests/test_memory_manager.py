from __future__ import annotations

from pathlib import Path

import pytest

from config import load_config
from memory.manager import MemoryManager
from memory.models import MemoryItem, MemorySource, Rejection, TripSummary, UserMemory
from memory.v3_models import EpisodeSlice, MemoryProfileItem
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
async def test_generate_context_includes_fixed_profile_and_slice_recall(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
    await manager.v3_store.upsert_profile_item(
        "u1",
        "stable_preferences",
        MemoryProfileItem(
            id="stable_preferences:pace:preferred_pace",
            domain="pace",
            key="preferred_pace",
            value="节奏轻松",
            polarity="prefer",
            stability="stable",
            confidence=0.9,
            status="active",
            context={},
            applicability="适用于大多数旅行。",
            recall_hints={"domains": ["pace"], "keywords": ["节奏"]},
            source_refs=[],
            created_at="2026-04-19T00:00:00",
            updated_at="2026-04-19T00:00:00",
        ),
    )
    await manager.v3_store.append_episode_slice(
        EpisodeSlice(
            id="slice_1",
            user_id="u1",
            source_episode_id="ep_1",
            source_trip_id="trip_kyoto_old",
            slice_type="accommodation_decision",
            domains=["hotel", "accommodation"],
            entities={"destination": "京都"},
            keywords=["住宿", "酒店"],
            content="上次京都住四条附近的町屋。",
            applicability="仅供住宿选择参考。",
            created_at="2026-04-19T00:00:00",
        )
    )

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_kyoto_now"),
        user_message="我上次去京都住哪里？",
    )

    assert "## 长期用户画像" in text
    assert "## 本轮请求命中的历史记忆" in text
    assert "## 本次旅行记忆" not in text
    assert "上次京都住四条附近的町屋。" in text
    assert recall.sources["profile_fixed"] == 1
    assert recall.sources["query_profile"] == 0
    assert recall.sources["episode_slice"] == 1
    assert recall.sources["working_memory"] == 0
    assert recall.profile_ids == ["stable_preferences:pace:preferred_pace"]
    assert recall.slice_ids == ["slice_1"]
    assert recall.matched_reasons


@pytest.mark.asyncio
async def test_generate_context_skips_slice_recall_for_current_trip_question(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
    await manager.v3_store.upsert_profile_item(
        "u1",
        "constraints",
        MemoryProfileItem(
            id="constraints:flight:avoid_red_eye",
            domain="flight",
            key="avoid_red_eye",
            value=True,
            polarity="avoid",
            stability="explicit_declared",
            confidence=0.95,
            status="active",
            context={},
            applicability="适用于所有旅行。",
            recall_hints={"domains": ["flight"], "keywords": ["红眼航班"]},
            source_refs=[],
            created_at="2026-04-19T00:00:00",
            updated_at="2026-04-19T00:00:00",
        ),
    )
    await manager.v3_store.append_episode_slice(
        EpisodeSlice(
            id="slice_1",
            user_id="u1",
            source_episode_id="ep_1",
            source_trip_id="trip_kyoto_old",
            slice_type="accommodation_decision",
            domains=["hotel"],
            entities={"destination": "京都"},
            keywords=["住宿"],
            content="上次京都住町屋。",
            applicability="仅供住宿选择参考。",
            created_at="2026-04-19T00:00:00",
        )
    )

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_kyoto_now"),
        user_message="这次预算多少？",
    )

    assert "## 长期用户画像" in text
    assert "## 本轮请求命中的历史记忆" not in text
    assert recall.sources["profile_fixed"] == 1
    assert recall.sources["query_profile"] == 0
    assert recall.sources["episode_slice"] == 0
    assert recall.slice_ids == []
    assert recall.matched_reasons == []


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


def test_memory_config_maps_new_block_and_falls_back_to_legacy_extraction(
    tmp_path: Path,
):
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


# --- 以下测试迁移自 test_memory.py（原文件已删除）---


def test_user_memory_defaults():
    mem = UserMemory(user_id="u1")
    assert mem.explicit_preferences == {}
    assert mem.rejections == []
    assert mem.trip_history == []


def test_generate_summary(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
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
