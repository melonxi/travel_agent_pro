from __future__ import annotations

from memory.policy import MemoryPolicy
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


def test_redact_for_storage_masks_pii_defensively():
    policy = MemoryPolicy()

    assert policy._redact_for_storage("证件 123456789") == "证件 [REDACTED]"
    assert policy._redact_for_storage("邮箱 user@example.com") == "邮箱 [REDACTED]"
    assert policy._redact_for_storage("电话 138-0000-0000") == "电话 [REDACTED]"
    assert policy._redact_for_storage({"document": {"number": "123456789"}}) == {
        "document": {"number": "[REDACTED]"}
    }


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
