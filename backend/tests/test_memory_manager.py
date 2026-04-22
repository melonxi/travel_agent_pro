from __future__ import annotations

from pathlib import Path

import pytest

from config import load_config
from memory.manager import MemoryManager
from memory.models import MemoryItem, MemorySource, Rejection, TripSummary, UserMemory
from memory.recall_query import RecallRetrievalPlan
from memory.recall_reranker import RecallRerankResult
from memory.symbolic_recall import RecallQuery
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
async def test_generate_context_prefers_retrieval_plan_over_legacy_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = MemoryManager(data_dir=str(tmp_path))
    await manager.v3_store.upsert_profile_item(
        "u1",
        "stable_preferences",
        MemoryProfileItem(
            id="stable_preferences:hotel:preferred_area",
            domain="hotel",
            key="preferred_area",
            value="京都住四条附近",
            polarity="prefer",
            stability="stable",
            confidence=0.9,
            status="active",
            context={},
            applicability="适用于大多数住宿选择。",
            recall_hints={"domains": ["hotel"], "keywords": ["住宿", "住哪里"]},
            source_refs=[],
            created_at="2026-04-19T00:00:00",
            updated_at="2026-04-19T00:00:00",
        ),
    )

    def fake_build_recall_query(message: str) -> RecallQuery:
        assert message == "住宿还是按我常规偏好来"
        return RecallQuery(
            needs_memory=True,
            domains=["food"],
            entities={},
            keywords=["辣"],
            include_profile=False,
            include_slices=False,
            include_working_memory=False,
            matched_reason="legacy query should not drive profile recall here",
        )

    monkeypatch.setattr("memory.manager.build_recall_query", fake_build_recall_query)

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="住宿还是按我常规偏好来",
        recall_gate=True,
        short_circuit="undecided",
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["stable_preferences"],
            domains=["hotel"],
            keywords=["住宿"],
            aliases=["住哪里"],
            strictness="soft",
            top_k=5,
            reason="reuse accommodation preference",
        ),
    )

    assert "京都住四条附近" in text
    assert recall.sources["query_profile"] == 1
    assert recall.final_recall_decision == "query_recall_enabled"


@pytest.mark.asyncio
async def test_generate_context_keeps_legacy_slice_recall_when_retrieval_plan_is_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = MemoryManager(data_dir=str(tmp_path))
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

    def fake_build_recall_query(message: str) -> RecallQuery:
        assert message == "还记得去年京都订的那家旅馆吗？"
        return RecallQuery(
            needs_memory=True,
            domains=["hotel", "accommodation"],
            entities={"destination": "京都"},
            keywords=["旅馆"],
            include_profile=True,
            include_slices=True,
            include_working_memory=False,
            matched_reason="legacy slice recall query",
        )

    monkeypatch.setattr("memory.manager.build_recall_query", fake_build_recall_query)

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_kyoto_now"),
        user_message="还记得去年京都订的那家旅馆吗？",
        recall_gate=True,
        short_circuit="force_recall",
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["stable_preferences"],
            domains=["hotel"],
            keywords=["住宿"],
            aliases=["住哪里"],
            strictness="soft",
            top_k=5,
            reason="reuse accommodation preference",
        ),
    )

    assert "上次京都住四条附近的町屋。" in text
    assert recall.sources["episode_slice"] == 1
    assert recall.slice_ids == ["slice_1"]


@pytest.mark.asyncio
async def test_generate_context_uses_slice_recall_without_fixed_profile_injection(tmp_path: Path):
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

    assert "## 长期用户画像" not in text
    assert "## 本轮请求命中的历史记忆" in text
    assert "## 本次旅行记忆" not in text
    assert "上次京都住四条附近的町屋。" in text
    assert recall.sources["profile_fixed"] == 0
    assert recall.sources["query_profile"] == 0
    assert recall.sources["episode_slice"] == 1
    assert recall.sources["working_memory"] == 0
    assert recall.profile_ids == []
    assert recall.slice_ids == ["slice_1"]
    assert recall.matched_reasons


@pytest.mark.asyncio
async def test_generate_context_merges_profile_and_slice_candidates(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
    await manager.v3_store.upsert_profile_item(
        "u1",
        "stable_preferences",
        MemoryProfileItem(
            id="stable_preferences:hotel:preferred_area",
            domain="hotel",
            key="preferred_area",
            value="京都住四条附近",
            polarity="prefer",
            stability="stable",
            confidence=0.9,
            status="active",
            context={},
            applicability="适用于大多数住宿选择。",
            recall_hints={"domains": ["hotel"], "keywords": ["住宿", "住哪里"]},
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

    assert "## 本轮请求命中的历史记忆" in text
    assert "京都住四条附近" in text
    assert "上次京都住四条附近的町屋。" in text
    assert recall.sources["query_profile"] == 1
    assert recall.sources["episode_slice"] == 1
    assert recall.profile_ids == ["stable_preferences:hotel:preferred_area"]
    assert recall.slice_ids == ["slice_1"]
    assert recall.matched_reasons


@pytest.mark.asyncio
async def test_generate_context_skips_all_profile_injection_for_current_trip_question(tmp_path: Path):
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

    assert "## 长期用户画像" not in text
    assert "## 本轮请求命中的历史记忆" not in text
    assert recall.sources["profile_fixed"] == 0
    assert recall.sources["query_profile"] == 0
    assert recall.sources["episode_slice"] == 0
    assert recall.profile_ids == []
    assert recall.slice_ids == []
    assert recall.matched_reasons == []


@pytest.mark.asyncio
async def test_generate_context_drops_fixed_profile_when_gate_blocks_query_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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

    def fail_rank_profile_items(*args, **kwargs):
        raise AssertionError("rank_profile_items should not run when recall gate blocks")

    monkeypatch.setattr("memory.manager.rank_profile_items", fail_rank_profile_items)

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_kyoto_now"),
        user_message="我上次去京都住哪里？",
        recall_gate=False,
        short_circuit="skip_recall",
    )

    assert "## 长期用户画像" not in text
    assert "## 本轮请求命中的历史记忆" not in text
    assert recall.sources["profile_fixed"] == 0
    assert recall.sources["query_profile"] == 0
    assert recall.sources["episode_slice"] == 0
    assert recall.profile_ids == []
    assert recall.gate_needs_recall is False
    assert recall.stage0_decision == "skip_recall"
    assert recall.final_recall_decision == "no_recall_applied"


@pytest.mark.asyncio
async def test_generate_context_formats_selected_candidates_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    manager = MemoryManager(data_dir=str(tmp_path))
    await manager.v3_store.upsert_profile_item(
        "u1",
        "stable_preferences",
        MemoryProfileItem(
            id="stable_preferences:hotel:preferred_area",
            domain="hotel",
            key="preferred_area",
            value="京都住四条附近",
            polarity="prefer",
            stability="stable",
            confidence=0.9,
            status="active",
            context={},
            applicability="适用于大多数住宿选择。",
            recall_hints={"domains": ["hotel"], "keywords": ["住宿", "住哪里"]},
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

    selected_ids: list[str] = []

    async def fake_select_candidates(*args, **kwargs):
        candidates = kwargs["candidates"]
        selected = candidates[:1]
        selected_ids.extend(candidate.item_id for candidate in selected)
        return selected, RecallRerankResult(
            selected_item_ids=[candidate.item_id for candidate in selected],
            final_reason="selected_by_test",
            per_item_reason={candidate.item_id: "selected by test" for candidate in selected},
            fallback_used="none",
        )

    monkeypatch.setattr("memory.manager.select_recall_candidates", fake_select_candidates)

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="我上次去京都住哪里？",
    )

    assert selected_ids
    assert "京都住四条附近" in text or "上次京都住四条附近的町屋。" in text
    assert recall.candidate_count >= len(selected_ids)
    assert recall.reranker_selected_ids == selected_ids


@pytest.mark.asyncio
async def test_generate_context_keeps_empty_reranker_fields_when_no_candidates(
    tmp_path: Path,
):
    manager = MemoryManager(data_dir=str(tmp_path))

    _, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="这次预算多少？",
    )

    assert recall.candidate_count == 0
    assert recall.reranker_selected_ids == []
    assert recall.reranker_final_reason == ""


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


def test_memory_config_maps_recall_gate_fields(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
memory:
  enabled: "true"
  retrieval:
    core_limit: 5
    phase_limit: 3
    include_pending: "true"
    recall_gate_enabled: "true"
    recall_gate_model: "gpt-4o-mini"
    recall_gate_timeout_seconds: 6.5
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_file))

    assert cfg.memory.retrieval.recall_gate_enabled is True
    assert cfg.memory.retrieval.recall_gate_model == "gpt-4o-mini"
    assert cfg.memory.retrieval.recall_gate_timeout_seconds == 6.5


def test_memory_config_maps_empty_recall_gate_model_to_empty_string(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
memory:
  retrieval:
    recall_gate_model:
""",
        encoding="utf-8",
    )

    cfg = load_config(str(cfg_file))

    assert cfg.memory.retrieval.recall_gate_model == ""


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
