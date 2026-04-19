from __future__ import annotations

from memory.models import MemoryCandidate, MemoryItem, MemorySource
from memory.policy import MemoryMerger, MemoryPolicy
from memory.v3_models import MemoryProfileItem, WorkingMemoryItem


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
        applicability="适用于所有旅行",
        recall_hints={"keywords": ["红眼航班"]},
        source_refs=[],
        created_at="2026-04-19T00:00:00",
        updated_at="2026-04-19T00:00:00",
    )
    base.update(overrides)
    return MemoryProfileItem(**base)


def make_working_item(**overrides):
    base = dict(
        id="wm_1",
        phase=3,
        kind="temporary_rejection",
        domains=["attraction"],
        content="先别考虑迪士尼",
        reason="当前候选筛选需要避让",
        status="active",
        expires={"on_trip_change": True},
        created_at="2026-04-19T00:00:00",
    )
    base.update(overrides)
    return WorkingMemoryItem(**base)


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
    policy = MemoryPolicy()

    status = policy.classify(make_candidate(risk="low", confidence=0.7))

    assert status == "auto_save"


def test_low_risk_auto_save_item_becomes_active():
    policy = MemoryPolicy()
    candidate = make_candidate(risk="low", confidence=0.7)

    item = policy.to_item(candidate, user_id="u1", session_id="s1", now="2026-04-11T00:00:00")

    assert item.status == "active"


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


def test_candidate_to_item_rejects_drop_actions():
    policy = MemoryPolicy()
    candidate = make_candidate(domain="payment")

    try:
        policy.to_item(candidate, user_id="u1", session_id="s1", now="2026-04-11T00:00:00")
        raised = False
    except ValueError:
        raised = True

    assert raised is True


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


def test_rejection_ids_include_value():
    policy = MemoryPolicy()
    first = make_candidate(
        type="rejection",
        domain="flight",
        key="avoid",
        scope="global",
        value="red_eye",
        risk="low",
        confidence=0.7,
    )
    second = make_candidate(
        type="rejection",
        domain="flight",
        key="avoid",
        scope="global",
        value="long_layover",
        risk="low",
        confidence=0.7,
    )

    first_item = policy.to_item(first, user_id="u1", session_id="s1", now="2026-04-11T00:00:00")
    second_item = policy.to_item(second, user_id="u1", session_id="s1", now="2026-04-11T00:00:00")

    assert first_item.id != second_item.id


def test_document_phrases_are_dropped():
    policy = MemoryPolicy()

    assert policy.classify(make_candidate(value="护照号 E12345678")) == "drop"
    assert policy.classify(make_candidate(value="passport number E12345678")) == "drop"
    assert policy.classify(make_candidate(value="身份证 X")) == "drop"
    assert policy.classify(make_candidate(value="id number X")) == "drop"


def test_pii_in_evidence_reason_or_attributes_is_dropped():
    policy = MemoryPolicy()

    assert (
        policy.classify(
            make_candidate(
                value="需要检查签证",
                evidence="我的护照号是 123456789",
            )
        )
        == "drop"
    )
    assert (
        policy.classify(
            make_candidate(
                value="需要检查签证",
                reason="用户提到 passport number E12345678",
            )
        )
        == "drop"
    )
    assert (
        policy.classify(
            make_candidate(
                value="需要检查签证",
                attributes={"document": {"number": "123456789"}},
            )
        )
        == "drop"
    )
    assert (
        policy.classify(
            make_candidate(
                value="需要联系我",
                evidence="我的邮箱 user@example.com",
            )
        )
        == "drop"
    )
    assert (
        policy.classify(
            make_candidate(
                value="需要联系我",
                reason="用户电话 138-0000-0000",
            )
        )
        == "drop"
    )


def test_redact_for_storage_masks_pii_defensively():
    policy = MemoryPolicy()

    assert policy._redact_for_storage("证件 123456789") == "证件 [REDACTED]"
    assert policy._redact_for_storage("邮箱 user@example.com") == "邮箱 [REDACTED]"
    assert policy._redact_for_storage("电话 138-0000-0000") == "电话 [REDACTED]"
    assert policy._redact_for_storage({"document": {"number": "123456789"}}) == {
        "document": {"number": "[REDACTED]"}
    }


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


def test_classify_v3_constraint_active_when_explicit_and_low_risk():
    policy = MemoryPolicy()
    item = make_profile_item(
        stability="explicit_declared",
        confidence=0.85,
        status="active",
    )
    assert policy.classify_v3_profile_item("constraints", item) == "active"


def test_classify_v3_constraint_pending_when_low_confidence():
    policy = MemoryPolicy()
    item = make_profile_item(
        stability="explicit_declared",
        confidence=0.6,
        status="active",
    )
    assert policy.classify_v3_profile_item("constraints", item) == "pending"


def test_classify_v3_preference_hypothesis_stays_pending():
    policy = MemoryPolicy()
    item = make_profile_item(
        domain="pace",
        key="preferred_pace",
        value="relaxed",
        polarity="prefer",
        stability="single_observation",
        confidence=0.7,
        status="pending",
    )
    assert policy.classify_v3_profile_item("preference_hypotheses", item) == "pending"


def test_classify_v3_health_item_pending_even_when_explicit():
    policy = MemoryPolicy()
    item = make_profile_item(
        domain="health",
        key="needs_elevator",
        value=True,
        polarity="prefer",
        stability="explicit_declared",
        confidence=0.95,
    )
    assert policy.classify_v3_profile_item("constraints", item) == "pending"


def test_classify_v3_drops_payment_domain():
    policy = MemoryPolicy()
    item = make_profile_item(
        domain="payment",
        key="card_holder",
        value="Zhang",
    )
    assert policy.classify_v3_profile_item("constraints", item) == "drop"


def test_classify_v3_drops_membership_domain():
    policy = MemoryPolicy()
    item = make_profile_item(
        domain="membership",
        key="marriott_gold",
        value=True,
    )
    assert policy.classify_v3_profile_item("constraints", item) == "drop"


def test_classify_v3_drops_pii_value():
    policy = MemoryPolicy()
    item = make_profile_item(
        domain="flight",
        key="passenger_id",
        value="护照号 E12345678",
    )
    assert policy.classify_v3_profile_item("constraints", item) == "drop"


def test_classify_v3_drops_pii_in_source_refs():
    policy = MemoryPolicy()
    item = make_profile_item(
        source_refs=[
            {
                "kind": "message",
                "session_id": "s1",
                "quote": "我的邮箱 user@example.com",
            }
        ]
    )
    assert policy.classify_v3_profile_item("constraints", item) == "drop"


def test_sanitize_v3_profile_item_redacts_pii_text():
    policy = MemoryPolicy()
    item = make_profile_item(
        value="联系电话 138-0000-0000",
        context={"note": "用户邮箱 user@example.com"},
        applicability="联系 138-0000-0000",
        recall_hints={"keywords": ["user@example.com"]},
    )
    sanitized = policy.sanitize_v3_profile_item(item)
    assert "138" not in str(sanitized.value)
    assert "[REDACTED]" in str(sanitized.value)
    assert "[REDACTED]" in sanitized.context["note"]
    assert "[REDACTED]" in sanitized.applicability
    assert "[REDACTED]" in sanitized.recall_hints["keywords"][0]


def test_sanitize_working_memory_item_redacts_text():
    policy = MemoryPolicy()
    item = make_working_item(
        content="联系手机 138-0000-0000",
        reason="用户邮箱 user@example.com",
    )
    sanitized = policy.sanitize_working_memory_item(item)
    assert "[REDACTED]" in sanitized.content
    assert "[REDACTED]" in sanitized.reason
