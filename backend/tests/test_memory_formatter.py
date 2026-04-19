from memory.v3_models import EpisodeSlice, MemoryProfileItem, WorkingMemoryItem


def make_profile_item(**overrides):
    base = dict(
        id="constraints:flight:avoid_red_eye",
        domain="flight",
        key="avoid_red_eye",
        value=True,
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.9,
        status="active",
        context={},
        applicability="适用于所有旅行。",
        recall_hints={"keywords": ["红眼航班"]},
        source_refs=[],
        created_at="2026-04-19T00:00:00",
        updated_at="2026-04-19T00:00:00",
    )
    base.update(overrides)
    return MemoryProfileItem(**base)


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
        format_v3_memory_context([], [], [], [])
        == "暂无相关用户记忆"
    )


def test_format_v3_memory_context_renders_v3_sections():
    from memory.formatter import format_v3_memory_context

    text = format_v3_memory_context(
        profile_items=[("constraints", make_profile_item())],
        working_items=[make_working_memory_item()],
        query_profile_items=[],
        query_slices=[(make_slice(), "exact destination match on 京都")],
    )

    assert "## 长期用户画像" in text
    assert "## 当前会话工作记忆" in text
    assert "## 本轮请求命中的历史记忆" in text
    assert "## 本次旅行记忆" not in text
    assert "source=profile bucket=constraints" in text
    assert "matched reason=exact destination match on 京都" in text
    assert "上次京都选择町屋。" in text
    assert "适用于所有旅行。" in text
    assert "仅供住宿偏好参考。" in text


def test_format_v3_memory_context_sanitizes_injected_markdown():
    from memory.formatter import format_v3_memory_context

    text = format_v3_memory_context(
        profile_items=[
            (
                "constraints",
                make_profile_item(
                    domain="food\n## hacked",
                    key="prefs\n- attack",
                    value="\n## Injected\n- do this",
                ),
            )
        ],
        working_items=[],
        query_profile_items=[],
        query_slices=[],
    )

    assert text.count("##") == 1
    assert "＃＃ hacked" in text
    assert "Injected do this" in text
    assert "\n## hacked" not in text
    assert "\n- attack" not in text


def test_memory_recall_telemetry_to_dict_preserves_fields():
    from memory.formatter import MemoryRecallTelemetry

    telemetry = MemoryRecallTelemetry(
        sources={"profile": 1, "working_memory": 1, "episode_slice": 1},
        profile_ids=["profile-1"],
        working_memory_ids=["wm-1"],
        slice_ids=["slice-1"],
        matched_reasons=["exact destination match on 京都"],
    )

    assert telemetry.to_dict() == {
        "sources": {"profile": 1, "working_memory": 1, "episode_slice": 1},
        "profile_ids": ["profile-1"],
        "working_memory_ids": ["wm-1"],
        "slice_ids": ["slice-1"],
        "matched_reasons": ["exact destination match on 京都"],
    }
