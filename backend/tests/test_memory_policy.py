from __future__ import annotations

from memory.models import MemoryCandidate, MemoryItem, MemorySource
from memory.policy import MemoryMerger, MemoryPolicy


def make_candidate(**overrides):
    base = dict(
        type="preference",
        domain="general",
        key="preferred_pace",
        value="relaxed",
        scope="global",
        polarity="neutral",
        confidence=0.8,
        risk="low",
        evidence="用户喜欢轻松节奏",
        reason="明确表达",
        attributes={},
    )
    base.update(overrides)
    return MemoryCandidate(**base)


def make_item(**overrides):
    base = dict(
        id="mem1",
        user_id="u1",
        type="preference",
        domain="general",
        key="preferred_pace",
        value="relaxed",
        scope="global",
        polarity="neutral",
        confidence=0.8,
        status="active",
        source=MemorySource(kind="message", session_id="s1"),
        created_at="2026-04-11T00:00:00",
        updated_at="2026-04-11T00:00:00",
        attributes={},
    )
    base.update(overrides)
    return MemoryItem(**base)


def test_low_risk_high_confidence_auto_saves():
    policy = MemoryPolicy(auto_save_low_risk=True)

    status = policy.classify(make_candidate(risk="low", confidence=0.7))

    assert status == "auto_save"


def test_medium_risk_defaults_pending():
    policy = MemoryPolicy()

    status = policy.classify(make_candidate(risk="medium", confidence=0.9))

    assert status == "pending"


def test_high_risk_is_pending():
    policy = MemoryPolicy(auto_save_low_risk=True, auto_save_medium_risk=True)

    status = policy.classify(make_candidate(risk="high", confidence=0.99))

    assert status == "pending"


def test_payment_candidate_is_dropped():
    policy = MemoryPolicy()

    status = policy.classify(make_candidate(domain="payment"))

    assert status == "drop"


def test_number_like_value_is_dropped():
    policy = MemoryPolicy()

    status = policy.classify(
        make_candidate(
            domain="passport",
            value={"passport": {"number": "123456789"}},
        )
    )

    assert status == "drop"


def test_low_risk_below_threshold_is_pending():
    policy = MemoryPolicy(auto_save_low_risk=True)

    status = policy.classify(make_candidate(risk="low", confidence=0.69))

    assert status == "pending"


def test_candidate_to_item_sets_pending_status_and_trims_quote():
    policy = MemoryPolicy()
    evidence = "x" * 130
    candidate = make_candidate(risk="medium", confidence=0.85, evidence=evidence)

    item = policy.to_item(candidate, user_id="u1", session_id="s1", now="2026-04-11T00:00:00")

    assert item.status == "pending"
    assert item.source.kind == "message"
    assert item.source.quote == evidence[:120]


def test_candidate_to_item_uses_trip_id_only_for_trip_scope():
    policy = MemoryPolicy()
    candidate = make_candidate(scope="global")

    item = policy.to_item(
        candidate,
        user_id="u1",
        session_id="s1",
        now="2026-04-11T00:00:00",
        trip_id="trip1",
    )

    assert item.trip_id is None


def test_merge_same_scalar_conflict_obsoletes_existing_and_marks_incoming():
    existing = make_item(value="relaxed", updated_at="2026-04-11T00:00:00")
    incoming = make_item(value="slow", updated_at="2026-04-11T00:01:00")
    merger = MemoryMerger()

    merged = merger.merge([existing], incoming)

    assert len(merged) == 2
    assert merged[0].status == "obsolete"
    assert merged[1].status == "pending_conflict"
    assert merged[1].value == "slow"


def test_merge_list_values_unions_and_uses_max_confidence():
    existing = make_item(value=["a", "b"], confidence=0.7)
    incoming = make_item(value=["b", "c"], confidence=0.9)
    merger = MemoryMerger()

    merged = merger.merge([existing], incoming)

    assert len(merged) == 1
    assert merged[0].value == ["a", "b", "c"]
    assert merged[0].confidence == 0.9


def test_merge_same_scalar_same_value_updates_without_duplicate():
    existing = make_item(value="relaxed", confidence=0.7, updated_at="2026-04-11T00:00:00")
    incoming = make_item(value="relaxed", confidence=0.9, updated_at="2026-04-11T00:01:00")
    merger = MemoryMerger()

    merged = merger.merge([existing], incoming)

    assert len(merged) == 1
    assert merged[0].confidence == 0.9
    assert merged[0].updated_at == "2026-04-11T00:01:00"
