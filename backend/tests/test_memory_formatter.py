from memory.retrieval_candidates import RecallCandidate
from memory.v3_models import EpisodeSlice, MemoryProfileItem, WorkingMemoryItem


def make_working_memory_item(**overrides):
    base = dict(
        id="wm-1",
        phase=3,
        kind="temporary_rejection",
        domains=["hotel"],
        content="先别考虑青旅。",
        reason="当前候选筛选需要避让。",
        status="active",
        expires={"on_trip_change": True},
        created_at="2026-04-19T00:00:00",
    )
    base.update(overrides)
    return WorkingMemoryItem(**base)


def make_slice(**overrides):
    base = dict(
        id="slice-1",
        user_id="u1",
        source_episode_id="ep-1",
        source_trip_id="trip-1",
        slice_type="accommodation_decision",
        domains=["hotel"],
        entities={"destination": "京都"},
        keywords=["住宿", "酒店"],
        content="上次京都选择町屋。",
        applicability="仅供住宿偏好参考。",
        created_at="2026-04-19T00:00:00",
    )
    base.update(overrides)
    return EpisodeSlice(**base)


def test_format_v3_memory_context_returns_empty_message_for_no_memory():
    from memory.formatter import format_v3_memory_context

    assert (
        format_v3_memory_context([], [])
        == "暂无相关用户记忆"
    )


def test_format_v3_memory_context_renders_only_working_and_recall_sections():
    from memory.formatter import format_v3_memory_context

    text = format_v3_memory_context(
        working_items=[make_working_memory_item()],
        recall_candidates=[
            RecallCandidate(
                source="episode_slice",
                item_id="slice-1",
                bucket="accommodation_decision",
                score=1.0,
                matched_reason=["exact destination match on 京都"],
                content_summary="上次京都选择町屋。",
                domains=["hotel"],
                applicability="仅供住宿偏好参考。",
            )
        ],
    )

    assert "长期用户画像" not in text
    assert "## 当前会话工作记忆" in text
    assert "## 本轮请求命中的历史记忆" in text
    assert "## 本次旅行记忆" not in text
    assert "matched reason=exact destination match on 京都" in text
    assert "上次京都选择町屋。" in text
    assert "仅供住宿偏好参考。" in text


def test_format_v3_memory_context_sanitizes_injected_markdown():
    from memory.formatter import format_v3_memory_context

    text = format_v3_memory_context(
        working_items=[
            make_working_memory_item(
                domains=["food\n## hacked"],
                content="\n## Injected\n- do this",
            )
        ],
        recall_candidates=[],
    )

    assert text.count("##") == 1
    assert "＃＃ hacked" in text
    assert "Injected do this" in text
    assert "\n## hacked" not in text
    assert "\n- attack" not in text


def test_format_v3_memory_context_renders_unified_recall_candidates():
    from memory.formatter import format_v3_memory_context

    text = format_v3_memory_context(
        working_items=[],
        recall_candidates=[
            RecallCandidate(
                source="profile",
                item_id="constraints:flight:avoid_red_eye",
                bucket="constraints",
                score=1.0,
                matched_reason=["exact domain match on flight", "keyword match on 红眼航班"],
                content_summary="flight:avoid_red_eye=true",
                domains=["flight"],
                applicability="适用于所有旅行。",
            ),
            RecallCandidate(
                source="episode_slice",
                item_id="slice-1",
                bucket="accommodation_decision",
                score=0.5,
                matched_reason=["exact destination match on 京都"],
                content_summary="上次京都选择町屋。",
                domains=["hotel"],
                applicability="仅供住宿偏好参考。",
            ),
        ],
    )

    assert "## 本轮请求命中的历史记忆" in text
    assert "source=profile bucket=constraints" in text
    assert "source=episode_slice bucket=accommodation_decision" in text
    assert "matched reason=exact domain match on flight；keyword match on 红眼航班" in text
    assert "[flight] avoid_red_eye: true" in text
    assert "content: flight:avoid_red_eye=true" not in text
    assert "content: 上次京都选择町屋。" in text


def test_memory_recall_telemetry_to_dict_preserves_fields():
    from memory.formatter import MemoryRecallTelemetry

    telemetry = MemoryRecallTelemetry(
        sources={"query_profile": 1, "working_memory": 1, "episode_slice": 1},
        profile_ids=["profile-1"],
        working_memory_ids=["wm-1"],
        slice_ids=["slice-1"],
        matched_reasons=["exact destination match on 京都"],
    )

    assert telemetry.to_dict() == {
        "sources": {"query_profile": 1, "working_memory": 1, "episode_slice": 1},
        "profile_ids": ["profile-1"],
        "working_memory_ids": ["wm-1"],
        "slice_ids": ["slice-1"],
        "matched_reasons": ["exact destination match on 京都"],
        "stage0_decision": "undecided",
        "stage0_reason": "",
        "gate_needs_recall": None,
        "gate_intent_type": "",
        "gate_confidence": None,
        "gate_reason": "",
        "final_recall_decision": "",
        "fallback_used": "none",
        "query_plan": {},
        "query_plan_fallback": "none",
        "candidate_count": 0,
        "reranker_selected_ids": [],
        "reranker_final_reason": "",
        "reranker_fallback": "none",
    }


def test_memory_recall_telemetry_to_dict_includes_gate_fields():
    from memory.formatter import MemoryRecallTelemetry

    telemetry = MemoryRecallTelemetry(
        sources={
            "query_profile": 0,
            "working_memory": 0,
            "episode_slice": 0,
        },
        profile_ids=["profile-1"],
        matched_reasons=["query profile recall"],
        stage0_decision="force_recall",
        stage0_reason="history_phrase",
        gate_needs_recall=True,
        gate_intent_type="profile_preference_recall",
        gate_confidence=0.88,
        gate_reason="user asks to reuse prior preference",
        final_recall_decision="query_recall_enabled",
        fallback_used="none",
    )

    assert telemetry.to_dict()["stage0_decision"] == "force_recall"
    assert telemetry.to_dict()["gate_needs_recall"] is True
    assert telemetry.to_dict()["final_recall_decision"] == "query_recall_enabled"


def test_memory_recall_telemetry_to_dict_keeps_only_active_sources_by_default():
    from memory.formatter import MemoryRecallTelemetry

    payload = MemoryRecallTelemetry().to_dict()

    assert payload["sources"] == {
        "query_profile": 0,
        "working_memory": 0,
        "episode_slice": 0,
    }


def test_memory_recall_telemetry_to_dict_includes_reranker_fields():
    from memory.formatter import MemoryRecallTelemetry

    telemetry = MemoryRecallTelemetry(
        candidate_count=4,
        reranker_selected_ids=["profile_1", "slice_2"],
        reranker_final_reason="two items directly answer the user's question",
        reranker_fallback="none",
    )

    payload = telemetry.to_dict()

    assert payload["candidate_count"] == 4
    assert payload["reranker_selected_ids"] == ["profile_1", "slice_2"]
    assert payload["reranker_final_reason"] == "two items directly answer the user's question"
    assert payload["reranker_fallback"] == "none"
