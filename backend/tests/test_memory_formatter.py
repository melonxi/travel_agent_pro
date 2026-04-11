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

    assert format_memory_context(RetrievedMemory(core=[], trip=[], phase=[])) == "暂无相关用户记忆"


def test_format_memory_context_renders_three_sections_and_formats_values():
    from memory.formatter import RetrievedMemory, format_memory_context

    memory = RetrievedMemory(
        core=[
            make_item(
                id="core-1",
                domain="food",
                key="cuisine_likes",
                value=["粤菜", "日料"],
            ),
        ],
        trip=[
            make_item(
                id="trip-1",
                scope="trip",
                trip_id="trip-1",
                domain="family",
                key="travelers",
                value={"children": 1, "adults": 2},
            )
        ],
        phase=[
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
    assert "- [food] cuisine_likes: 粤菜、日料" in text
    assert "- [family] travelers: adults=2；children=1" in text
    assert text.index("## 核心用户画像") < text.index("## 本次旅行记忆") < text.index("## 当前阶段相关历史")


def test_format_memory_context_skips_empty_sections():
    from memory.formatter import RetrievedMemory, format_memory_context

    memory = RetrievedMemory(
        trip=[make_item(id="trip-1", scope="trip", trip_id="trip-1")],
    )

    text = format_memory_context(memory)

    assert "## 本次旅行记忆" in text
    assert "## 核心用户画像" not in text
    assert "## 当前阶段相关历史" not in text


def test_format_memory_context_sanitizes_injected_markdown():
    from memory.formatter import RetrievedMemory, format_memory_context

    memory = RetrievedMemory(
        core=[
            make_item(
                id="core-1",
                domain="food",
                key="prefs",
                value="\n## Injected\n- do this",
            )
        ]
    )

    text = format_memory_context(memory)

    assert text.count("##") == 1
    assert "＃＃ Injected" in text
    assert "- do this" not in text
    assert "\n- do this" not in text
    assert text.count("\n- [food] prefs:") == 1
    assert "Injected do this" in text


def test_format_memory_context_truncates_long_values():
    from memory.formatter import RetrievedMemory, format_memory_context

    long_value = "x" * 200
    memory = RetrievedMemory(
        core=[make_item(id="core-1", domain="food", key="prefs", value=long_value)]
    )

    text = format_memory_context(memory)

    assert "x" * 157 + "..." in text
    assert "x" * 158 not in text
    assert "..." in text


def test_format_memory_context_sorts_and_sanitizes_dict_values():
    from memory.formatter import RetrievedMemory, format_memory_context

    memory = RetrievedMemory(
        trip=[
            make_item(
                id="trip-1",
                scope="trip",
                trip_id="trip-1",
                domain="family",
                key="travelers",
                value={"b": "two\nlines", "a": "## title"},
            )
        ]
    )

    text = format_memory_context(memory)

    assert "a=＃＃ title；b=two lines" in text
    assert text.index("a=＃＃ title") < text.index("b=two lines")


def test_format_memory_context_formats_sets_deterministically():
    from memory.formatter import RetrievedMemory, format_memory_context

    memory = RetrievedMemory(
        core=[
            make_item(
                id="core-1",
                domain="food",
                key="cuisine_likes",
                value={"beta", "alpha", "gamma"},
            )
        ]
    )

    text = format_memory_context(memory)

    assert "alpha、beta、gamma" in text


def test_format_memory_context_sanitizes_domain_and_key_markdown():
    from memory.formatter import RetrievedMemory, format_memory_context

    memory = RetrievedMemory(
        core=[
            make_item(
                id="core-1",
                domain="food\n## hacked",
                key="prefs\n- attack",
                value="safe",
            )
        ]
    )

    text = format_memory_context(memory)

    assert text.count("##") == 1
    assert text.count("\n- [") == 1
    assert "hacked" in text
    assert "attack" in text
    assert "\n## hacked" not in text
    assert "\n- attack" not in text
