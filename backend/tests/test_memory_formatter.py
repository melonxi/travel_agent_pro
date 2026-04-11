from memory.models import MemoryItem, MemorySource


def make_item(**overrides):
    base = dict(
        id="mem-1",
        user_id="u1",
        type="preference",
        domain="pace",
        key="preferred_pace",
        value="relaxed",
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


def test_format_memory_context_returns_empty_message_for_no_memory():
    from memory.formatter import RetrievedMemory, format_memory_context

    assert format_memory_context(RetrievedMemory()) == "暂无相关用户记忆"


def test_format_memory_context_renders_three_sections_and_formats_values():
    from memory.formatter import RetrievedMemory, format_memory_context

    memory = RetrievedMemory(
        core_profile=[
            make_item(
                id="core-1",
                domain="food",
                key="cuisine_likes",
                value=["粤菜", "日料"],
            ),
        ],
        trip_memory=[
            make_item(
                id="trip-1",
                scope="trip",
                trip_id="trip-1",
                domain="family",
                key="travelers",
                value={"children": 1, "adults": 2},
            )
        ],
        phase_relevant=[
            make_item(
                id="phase-1",
                domain="budget",
                key="daily_budget",
                value="2000",
            )
        ],
    )

    text = format_memory_context(memory)

    assert "## 核心用户画像" in text
    assert "## 本次旅行记忆" in text
    assert "## 当前阶段相关历史" in text
    assert "粤菜、日料" in text
    assert "adults=2；children=1" in text
    assert text.index("## 核心用户画像") < text.index("## 本次旅行记忆") < text.index("## 当前阶段相关历史")


def test_format_memory_context_skips_empty_sections():
    from memory.formatter import RetrievedMemory, format_memory_context

    memory = RetrievedMemory(
        trip_memory=[make_item(id="trip-1", scope="trip", trip_id="trip-1")],
    )

    text = format_memory_context(memory)

    assert "## 本次旅行记忆" in text
    assert "## 核心用户画像" not in text
    assert "## 当前阶段相关历史" not in text
