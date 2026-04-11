from types import SimpleNamespace

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
        trip_id=None,
    )
    base.update(overrides)
    return MemoryItem(**base)


def test_retrieve_core_profile_filters_to_active_global_and_ranks_priority():
    from memory.retriever import MemoryRetriever

    retriever = MemoryRetriever()
    items = [
        make_item(
            id="pref",
            type="preference",
            domain="pace",
            confidence=0.95,
            updated_at="2026-04-11T00:00:00",
        ),
        make_item(
            id="constraint",
            type="constraint",
            domain="budget",
            confidence=0.9,
            updated_at="2026-04-11T00:01:00",
        ),
        make_item(
            id="rejection",
            type="rejection",
            domain="food",
            confidence=0.8,
            updated_at="2026-04-11T00:02:00",
        ),
        make_item(
            id="pending",
            type="constraint",
            domain="family",
            confidence=0.99,
            status="pending",
            updated_at="2026-04-11T00:03:00",
        ),
        make_item(
            id="trip",
            type="constraint",
            domain="hotel",
            confidence=0.97,
            scope="trip",
            trip_id="trip-1",
            updated_at="2026-04-11T00:04:00",
        ),
    ]

    result = retriever.retrieve_core_profile(items, limit=10)

    assert [item.id for item in result] == ["constraint", "rejection", "pref"]


def test_retrieve_trip_memory_requires_matching_trip_id():
    from memory.retriever import MemoryRetriever

    retriever = MemoryRetriever()
    plan = SimpleNamespace(trip_id="trip-1")
    items = [
        make_item(
            id="match",
            scope="trip",
            trip_id="trip-1",
            type="preference",
            domain="food",
        ),
        make_item(
            id="wrong-trip",
            scope="trip",
            trip_id="trip-2",
            type="preference",
            domain="food",
        ),
        make_item(
            id="global",
            scope="global",
            type="preference",
            domain="food",
        ),
        make_item(
            id="pending-match",
            scope="trip",
            trip_id="trip-1",
            type="preference",
            domain="pace",
            status="pending",
        ),
    ]

    result = retriever.retrieve_trip_memory(items, plan)

    assert [item.id for item in result] == ["match"]


def test_retrieve_trip_memory_without_trip_id_returns_empty():
    from memory.retriever import MemoryRetriever

    retriever = MemoryRetriever()
    plan = SimpleNamespace(trip_id=None)

    result = retriever.retrieve_trip_memory([make_item(scope="trip", trip_id="trip-1")], plan)

    assert result == []


def test_retrieve_phase_relevant_filters_domains_and_trip_scope():
    from memory.retriever import MemoryRetriever

    retriever = MemoryRetriever()
    plan = SimpleNamespace(trip_id="trip-1")
    items = [
        make_item(id="pace", domain="pace", confidence=0.9),
        make_item(id="food", domain="food", confidence=0.8),
        make_item(id="budget", domain="budget", confidence=0.85, scope="trip", trip_id="trip-1"),
        make_item(id="family", domain="family", confidence=0.95),
        make_item(id="accessibility", domain="accessibility", confidence=0.7),
        make_item(id="hotel", domain="hotel", confidence=0.99),
        make_item(id="flight", domain="flight", confidence=0.88, scope="trip", trip_id="trip-2"),
        make_item(id="irrelevant", domain="planning_style", confidence=0.77),
    ]

    result = retriever.retrieve_phase_relevant(items, plan, phase=5, limit=8)

    assert [item.id for item in result] == ["family", "pace", "budget", "food", "accessibility"]
