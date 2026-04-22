from __future__ import annotations

from memory.profile_normalization import normalize_profile_item
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
