from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from config import (
    MemoryRetrievalConfig,
    Stage3LaneConfig,
    Stage3RecallConfig,
    Stage3SemanticConfig,
    load_config,
)
from memory.manager import MemoryManager
from memory.recall_gate import apply_recall_short_circuit
from memory.recall_query import RecallRetrievalPlan
from memory.recall_reranker import RecallRerankResult
from memory.recall_stage3_models import Stage3RecallResult, Stage3Telemetry
from memory.v3_models import EpisodeSlice, MemoryProfileItem
from state.models import TravelPlanState


@pytest.mark.asyncio
async def test_generate_context_uses_retrieval_plan_for_profile_recall(tmp_path: Path):
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
            destination="",
            keywords=["住宿"],
            top_k=5,
            reason="reuse accommodation preference",
        ),
    )

    assert "京都住四条附近" in text
    assert recall.sources["query_profile"] == 1
    assert recall.final_recall_decision == "query_recall_enabled"


@pytest.mark.asyncio
async def test_generate_context_uses_retrieval_plan_for_slice_recall(tmp_path: Path):
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

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_kyoto_now"),
        user_message="还记得去年京都订的那家旅馆吗？",
        recall_gate=True,
        short_circuit="force_recall",
        retrieval_plan=RecallRetrievalPlan(
            source="episode_slice",
            buckets=[],
            domains=["hotel"],
            destination="京都",
            keywords=["住宿"],
            top_k=5,
            reason="reuse accommodation preference",
        ),
    )

    assert "上次京都住四条附近的町屋。" in text
    assert recall.sources["episode_slice"] == 1
    assert recall.slice_ids == ["slice_1"]


@pytest.mark.asyncio
async def test_generate_context_applies_top_k_to_episode_slice_candidates(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
    for idx in range(3):
        await manager.v3_store.append_episode_slice(
            EpisodeSlice(
                id=f"slice_{idx}",
                user_id="u1",
                source_episode_id=f"ep_{idx}",
                source_trip_id=f"trip_old_{idx}",
                slice_type="accommodation_decision",
                domains=["hotel"],
                entities={"destination": "京都"},
                keywords=["住宿", "酒店"],
                content=f"上次京都住处 {idx}",
                applicability="仅供住宿选择参考。",
                created_at=f"2026-04-19T00:00:0{idx}",
            )
        )

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_kyoto_now"),
        user_message="我上次去京都住哪里？",
        recall_gate=True,
        short_circuit="force_recall",
        retrieval_plan=RecallRetrievalPlan(
            source="episode_slice",
            buckets=[],
            domains=["hotel"],
            destination="京都",
            keywords=["住宿"],
            top_k=1,
            reason="past_trip_experience_recall -> Kyoto hotel slice lookup",
        ),
    )

    assert recall.candidate_count == 1
    assert len(recall.slice_ids) == 1
    assert text.count("source=episode_slice") == 1


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

    assert "长期用户画像" not in text
    assert "## 本轮请求命中的历史记忆" in text
    assert "## 本次旅行记忆" not in text
    assert "上次京都住四条附近的町屋。" in text
    assert "query_profile" in recall.sources
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

    assert "长期用户画像" not in text
    assert "## 本轮请求命中的历史记忆" not in text
    assert "query_profile" in recall.sources
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

    def fail_retrieve_recall_candidates(*args, **kwargs):
        raise AssertionError("retrieve_recall_candidates should not run when recall gate blocks")

    monkeypatch.setattr(
        "memory.manager.retrieve_recall_candidates",
        fail_retrieve_recall_candidates,
    )

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_kyoto_now"),
        user_message="我上次去京都住哪里？",
        recall_gate=False,
        short_circuit="skip_recall",
    )

    assert "长期用户画像" not in text
    assert "## 本轮请求命中的历史记忆" not in text
    assert "query_profile" in recall.sources
    assert recall.sources["query_profile"] == 0
    assert recall.sources["episode_slice"] == 0
    assert recall.profile_ids == []
    assert recall.gate_needs_recall is False
    assert recall.stage0_decision == "skip_recall"
    assert recall.final_recall_decision == "no_recall_applied"


@pytest.mark.asyncio
async def test_generate_context_uses_stage0_style_heuristic_when_query_plan_missing(
    tmp_path: Path,
):
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
            recall_hints={"domains": ["flight"], "keywords": ["机票", "航班"]},
            source_refs=[],
            created_at="2026-04-19T00:00:00",
            updated_at="2026-04-19T00:00:00",
        ),
    )
    stage0 = apply_recall_short_circuit("老样子给我订机票")

    text, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="老样子给我订机票",
        recall_gate=True,
        short_circuit=stage0.decision,
        retrieval_plan=None,
        stage0_matched_rule=stage0.matched_rule,
        stage0_signals={name: list(hits) for name, hits in stage0.signals},
        query_plan_source="heuristic_fallback",
        query_plan_fallback="query_plan_timeout",
    )

    assert "avoid_red_eye" in text
    assert recall.sources["query_profile"] == 1
    assert recall.stage0_matched_rule == "P1"
    assert recall.stage0_signals["style"] == ["老样子"]
    assert recall.query_plan_source == "heuristic_fallback"
    assert recall.query_plan_fallback == "query_plan_timeout"
    assert recall.recall_attempted_but_zero_hit is False


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
    assert recall.reranker_per_item_reason == {
        candidate_id: "selected by test" for candidate_id in selected_ids
    }


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


def test_memory_manager_does_not_expose_legacy_store_api(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))

    assert not hasattr(manager, "store")
    assert not hasattr(manager, "load")
    assert not hasattr(manager, "save")
    assert not hasattr(manager, "generate_summary")


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
    reranker:
      small_candidate_set_threshold: 2
      profile_top_n: 5
      slice_top_n: 4
      hybrid_top_n: 6
      hybrid_profile_top_n: 3
      hybrid_slice_top_n: 2
      recency_half_life_days: 90
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
    assert cfg.memory.retrieval.reranker.small_candidate_set_threshold == 2
    assert cfg.memory.retrieval.reranker.profile_top_n == 5
    assert cfg.memory.retrieval.reranker.slice_top_n == 4
    assert cfg.memory.retrieval.reranker.hybrid_top_n == 6
    assert cfg.memory.retrieval.reranker.hybrid_profile_top_n == 3
    assert cfg.memory.retrieval.reranker.hybrid_slice_top_n == 2
    assert cfg.memory.retrieval.reranker.recency_half_life_days == 90
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


@pytest.mark.asyncio
async def test_generate_context_attaches_stage3_telemetry(tmp_path: Path):
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
            recall_hints={"domains": ["hotel"], "keywords": ["住宿"]},
            source_refs=[],
            created_at="2026-04-19T00:00:00",
            updated_at="2026-04-19T00:00:00",
        ),
    )

    _, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="住宿按我习惯",
        recall_gate=True,
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="",
            keywords=["住宿"],
            top_k=5,
            reason="test",
        ),
    )

    assert recall.stage3["lanes_attempted"] == ["symbolic"]
    assert recall.stage3["zero_hit"] is False


@pytest.mark.asyncio
async def test_generate_context_passes_active_plan_to_reranker_when_plan_is_heuristic(
    tmp_path: Path,
    monkeypatch,
):
    manager = MemoryManager(data_dir=str(tmp_path))
    seen = {}

    async def fake_select_recall_candidates(**kwargs):
        seen["retrieval_plan"] = kwargs["retrieval_plan"]
        return kwargs["candidates"], RecallRerankResult(
            selected_item_ids=[candidate.item_id for candidate in kwargs["candidates"]],
            final_reason="fake",
            per_item_reason={},
            fallback_used="none",
        )

    monkeypatch.setattr("memory.manager.select_recall_candidates", fake_select_recall_candidates)
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
            recall_hints={"domains": ["hotel"], "keywords": ["住宿"]},
            source_refs=[],
            created_at="2026-04-19T00:00:00",
            updated_at="2026-04-19T00:00:00",
        ),
    )

    await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="住宿按我常规偏好来",
        recall_gate=True,
        short_circuit="force_recall",
        retrieval_plan=None,
    )

    assert seen["retrieval_plan"] is not None
    assert seen["retrieval_plan"].source == "profile"


@pytest.mark.asyncio
async def test_generate_context_filters_slices_by_normalized_destination_before_stage3(
    tmp_path: Path,
    monkeypatch,
):
    retrieval_config = MemoryRetrievalConfig(
        stage3=replace(Stage3RecallConfig(), destination_normalization_enabled=True)
    )
    manager = MemoryManager(data_dir=str(tmp_path), retrieval_config=retrieval_config)
    await manager.v3_store.append_episode_slice(
        EpisodeSlice(
            id="slice_kyoto_alias",
            user_id="u1",
            source_episode_id="ep_kyoto",
            source_trip_id="trip_kyoto_old",
            slice_type="accommodation_decision",
            domains=["hotel"],
            entities={"destination": "Kyoto"},
            keywords=["住宿"],
            content="上次京都住在四条。",
            applicability="仅供住宿选择参考。",
            created_at="2026-04-19T00:00:00",
        )
    )
    await manager.v3_store.append_episode_slice(
        EpisodeSlice(
            id="slice_paris",
            user_id="u1",
            source_episode_id="ep_paris",
            source_trip_id="trip_paris_old",
            slice_type="accommodation_decision",
            domains=["hotel"],
            entities={"destination": "Paris"},
            keywords=["住宿"],
            content="上次巴黎住在左岸。",
            applicability="仅供住宿选择参考。",
            created_at="2026-04-19T00:00:01",
        )
    )

    seen = {}

    def fake_retrieve_recall_candidates(**kwargs):
        seen["slice_ids"] = [slice_.id for slice_ in kwargs["slices"]]
        return Stage3RecallResult(
            candidates=[],
            evidence_by_id={},
            telemetry=Stage3Telemetry(
                lanes_attempted=["symbolic"],
                lanes_succeeded=["symbolic"],
                zero_hit=True,
            ),
        )

    monkeypatch.setattr(
        "memory.manager.retrieve_recall_candidates",
        fake_retrieve_recall_candidates,
    )

    await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="还记得去年京都订的那家旅馆吗？",
        recall_gate=True,
        retrieval_plan=RecallRetrievalPlan(
            source="episode_slice",
            buckets=[],
            domains=["hotel"],
            destination="京都",
            keywords=["住宿"],
            top_k=5,
            reason="test",
        ),
    )

    assert seen["slice_ids"] == ["slice_kyoto_alias"]


@pytest.mark.asyncio
async def test_generate_context_reports_semantic_lane_error_when_embedding_provider_init_fails(
    tmp_path: Path,
    monkeypatch,
):
    retrieval_config = MemoryRetrievalConfig(
        stage3=replace(
            Stage3RecallConfig(),
            symbolic=Stage3LaneConfig(enabled=False),
            semantic=Stage3SemanticConfig(enabled=True, min_score=0.7, top_k=5),
        )
    )
    manager = MemoryManager(data_dir=str(tmp_path), retrieval_config=retrieval_config)
    attempts = {"count": 0}

    class RaisingFastEmbedProvider:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            attempts["count"] += 1
            raise RuntimeError("boom")

    monkeypatch.setattr("memory.manager.FastEmbedProvider", RaisingFastEmbedProvider)

    for _ in range(2):
        _, recall = await manager.generate_context(
            "u1",
            TravelPlanState(session_id="s1", trip_id="trip_now"),
            user_message="住宿按我习惯",
            recall_gate=True,
            retrieval_plan=RecallRetrievalPlan(
                source="profile",
                buckets=["stable_preferences"],
                domains=["hotel"],
                destination="",
                keywords=["住宿"],
                top_k=5,
                reason="test",
            ),
        )

        assert recall.stage3["lane_errors"]["semantic"] == "embedding_provider_missing"

    assert attempts["count"] == 2
