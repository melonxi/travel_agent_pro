from memory.recall_query import RecallRetrievalPlan
from memory.recall_query_adapter import plan_to_legacy_recall_query


def test_plan_to_legacy_recall_query_merges_keywords_and_aliases():
    plan = RecallRetrievalPlan(
        source="profile",
        buckets=["stable_preferences", "constraints"],
        domains=["hotel"],
        keywords=["住宿"],
        aliases=["住哪里", "住宿偏好"],
        strictness="soft",
        top_k=8,
        reason="reuse accommodation preference",
    )

    query = plan_to_legacy_recall_query(plan)

    assert query.include_profile is True
    assert query.include_slices is False
    assert query.allowed_buckets == ["stable_preferences", "constraints"]
    assert query.strictness == "soft"
    assert query.keywords == ["住宿", "住哪里", "住宿偏好"]


def test_plan_to_legacy_recall_query_uses_conservative_buckets_for_invalid_values():
    plan = RecallRetrievalPlan(
        source="profile",
        buckets=["not_a_bucket"],
        domains=["hotel"],
        keywords=["住宿"],
        aliases=[],
        strictness="soft",
        top_k=5,
        reason="reuse accommodation preference",
    )

    query = plan_to_legacy_recall_query(plan)

    assert query.allowed_buckets == ["constraints", "rejections", "stable_preferences"]


def test_plan_to_legacy_recall_query_keeps_explicit_preference_hypotheses_bucket():
    plan = RecallRetrievalPlan(
        source="profile",
        buckets=["constraints", "preference_hypotheses"],
        domains=["flight"],
        keywords=["红眼航班"],
        aliases=[],
        strictness="soft",
        top_k=5,
        reason="reuse flight preference",
    )

    query = plan_to_legacy_recall_query(plan)

    assert query.allowed_buckets == ["constraints", "preference_hypotheses"]
