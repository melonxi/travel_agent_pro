from __future__ import annotations

from memory.profile_normalization import (
    merge_profile_item_with_existing,
    normalize_profile_item,
)
from memory.v3_models import MemoryProfileItem


def _item(**overrides):
    base = dict(
        id="",
        domain="food",
        key="dislike_spicy_food",
        value="不吃辣",
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.9,
        status="active",
        context={},
        applicability="",
        recall_hints={"domains": [], "keywords": ["不吃辣"], "aliases": []},
        source_refs=[{"kind": "message", "session_id": "s1", "quote": "我不吃辣"}],
        created_at="",
        updated_at="",
    )
    base.update(overrides)
    return MemoryProfileItem(**base)


def test_normalize_profile_item_canonicalizes_key_and_hints():
    item = normalize_profile_item("stable_preferences", _item())

    assert item.key == "avoid_spicy"
    assert item.recall_hints == {
        "domains": ["food"],
        "keywords": ["不吃辣"],
        "aliases": ["不吃辣", "不能吃辣", "避开辣味"],
    }
    assert item.applicability == "适用于大多数旅行。"


def test_normalize_profile_item_keeps_existing_applicability_when_present():
    item = normalize_profile_item(
        "constraints",
        _item(key="avoid_red_eye", domain="flight", applicability="适用于所有旅行。"),
    )

    assert item.applicability == "适用于所有旅行。"


def test_normalize_profile_item_handles_scalar_recall_hint_fields():
    item = normalize_profile_item(
        "constraints",
        _item(
            key="avoid_red_eye",
            domain="flight",
            recall_hints={
                "domains": "flight",
                "keywords": "红眼航班",
                "aliases": "夜间航班",
            },
        ),
    )

    assert item.recall_hints == {
        "domains": ["flight"],
        "keywords": ["红眼航班"],
        "aliases": ["夜间航班", "红眼航班"],
    }


def test_merge_profile_item_promotes_repeated_hypothesis_to_stable():
    existing = _item(
        id="preference_hypotheses:food:avoid_spicy:{}",
        key="avoid_spicy",
        stability="soft_constraint",
        confidence=0.82,
        context={"observation_count": 1},
        source_refs=[{"kind": "message", "session_id": "s1", "quote": "我不吃辣"}],
    )
    incoming = _item(
        key="avoid_spicy",
        stability="soft_constraint",
        confidence=0.95,
        source_refs=[{"kind": "message", "session_id": "s2", "quote": "以后都不吃辣"}],
    )

    merged_bucket, merged = merge_profile_item_with_existing(
        bucket="preference_hypotheses",
        incoming=incoming,
        existing_items=[existing],
    )

    assert merged_bucket == "stable_preferences"
    assert merged.stability == "pattern_observed"
    assert merged.confidence == 0.95
    assert merged.context["observation_count"] == 2
    assert merged.source_refs == [
        {"kind": "message", "session_id": "s1", "quote": "我不吃辣"},
        {"kind": "message", "session_id": "s2", "quote": "以后都不吃辣"},
    ]


def test_merge_profile_item_merges_same_slot_even_when_value_changes():
    existing = _item(
        id="constraints:food:avoid_spicy",
        key="avoid_spicy",
        value="不吃辣",
        stability="explicit_declared",
        confidence=0.84,
        context={"note": "previous", "observation_count": 1},
        source_refs=[
            {"kind": "message", "session_id": "s1", "quote": "我不吃辣"},
            {"kind": "message", "session_id": "s1", "quote": "我不吃辣"},
        ],
    )
    incoming = _item(
        key="avoid_spicy",
        value="尽量少辣",
        stability="explicit_declared",
        confidence=0.91,
        context={"fresh": "new"},
        source_refs=[
            {"kind": "message", "session_id": "s1", "quote": "我不吃辣"},
            {"kind": "message", "session_id": "s2", "quote": "这次尽量少辣"},
        ],
    )

    merged_bucket, merged = merge_profile_item_with_existing(
        bucket="constraints",
        incoming=incoming,
        existing_items=[existing],
    )

    assert merged_bucket == "constraints"
    assert merged.value == "尽量少辣"
    assert merged.context == {"note": "previous", "fresh": "new", "observation_count": 2}
    assert merged.source_refs == [
        {"kind": "message", "session_id": "s1", "quote": "我不吃辣"},
        {"kind": "message", "session_id": "s2", "quote": "这次尽量少辣"},
    ]


def test_merge_profile_item_dedupes_source_refs_with_deterministic_order():
    existing = _item(
        key="avoid_spicy",
        value="不吃辣",
        context={"observation_count": 1},
        source_refs=[
            {"kind": "message", "session_id": "s1", "quote": "我不吃辣"},
            {"kind": "message", "session_id": "s2", "quote": "也别太辣"},
        ],
    )
    incoming = _item(
        key="avoid_spicy",
        value="少辣",
        source_refs=[
            {"kind": "message", "session_id": "s2", "quote": "也别太辣"},
            {"kind": "message", "session_id": "s3", "quote": "尽量少辣"},
            {"kind": "message", "session_id": "s1", "quote": "我不吃辣"},
        ],
    )

    _, merged = merge_profile_item_with_existing(
        bucket="constraints",
        incoming=incoming,
        existing_items=[existing],
    )

    assert merged.source_refs == [
        {"kind": "message", "session_id": "s1", "quote": "我不吃辣"},
        {"kind": "message", "session_id": "s2", "quote": "也别太辣"},
        {"kind": "message", "session_id": "s3", "quote": "尽量少辣"},
    ]


def test_merge_profile_item_ignores_incoming_observation_count():
    existing = _item(
        key="prefer_quiet_room",
        value=True,
        polarity="prefer",
        stability="soft_constraint",
        confidence=0.66,
        context={"observation_count": 1},
    )
    incoming = _item(
        key="prefer_quiet_room",
        value=True,
        polarity="prefer",
        stability="soft_constraint",
        confidence=0.8,
        context={"observation_count": 99, "source": "incoming"},
    )

    merged_bucket, merged = merge_profile_item_with_existing(
        bucket="preference_hypotheses",
        incoming=incoming,
        existing_items=[existing],
    )

    assert merged_bucket == "stable_preferences"
    assert merged.context == {"observation_count": 2, "source": "incoming"}


def test_merge_profile_item_keeps_single_observation_as_hypothesis():
    incoming = _item(
        key="prefer_quiet_room",
        value=True,
        polarity="prefer",
        confidence=0.7,
        source_refs=[{"kind": "message", "session_id": "s1", "quote": "想要安静房间"}],
    )

    merged_bucket, merged = merge_profile_item_with_existing(
        bucket="preference_hypotheses",
        incoming=incoming,
        existing_items=[],
    )

    assert merged_bucket == "preference_hypotheses"
    assert merged.context["observation_count"] == 1
    assert merged.source_refs == [
        {"kind": "message", "session_id": "s1", "quote": "想要安静房间"}
    ]
