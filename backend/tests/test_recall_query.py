from memory.recall_query import (
    ALLOWED_RECALL_DOMAINS,
    RecallRetrievalPlan,
    fallback_retrieval_plan,
    parse_recall_query_tool_arguments,
)
from main import _build_recall_query_tool


def test_parse_recall_query_tool_arguments_honors_tightened_schema_fields():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences", "constraints"],
            "domains": ["hotel", "accommodation"],
            "destination": "京都",
            "keywords": ["住宿", "酒店"],
            "top_k": 8,
            "reason": "profile_preference_recall -> hotel domain -> stable_preferences",
        }
    )

    assert plan == RecallRetrievalPlan(
        source="profile",
        buckets=["stable_preferences", "constraints"],
        domains=["hotel", "accommodation"],
        destination="京都",
        keywords=["住宿", "酒店"],
        top_k=8,
        reason="profile_preference_recall -> hotel domain -> stable_preferences",
        fallback_used="none",
    )


def test_parse_recall_query_tool_arguments_rejects_unknown_domains():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["lodging", "hotel"],
            "destination": "",
            "keywords": ["住宿"],
            "top_k": 5,
            "reason": "bad domain",
        }
    )

    assert plan.fallback_used == "invalid_query_plan"
    assert plan.reason == "invalid_query_plan"


def test_parse_recall_query_tool_arguments_rejects_non_destination_entity_shape():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "episode_slice",
            "buckets": [],
            "domains": ["hotel"],
            "entities": {"city": "京都"},
            "keywords": ["住宿"],
            "top_k": 5,
            "reason": "bad destination shape",
        }
    )

    assert plan.fallback_used == "invalid_query_plan"
    assert plan.reason == "invalid_query_plan"


def test_parse_recall_query_tool_arguments_does_not_accept_removed_aliases_or_strictness():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["hotel"],
            "destination": "",
            "keywords": ["住宿"],
            "aliases": ["住哪里"],
            "strictness": "soft",
            "top_k": 5,
            "reason": "removed fields should invalidate the payload",
        }
    )

    assert plan.fallback_used == "invalid_query_plan"
    assert plan.reason == "invalid_query_plan"


def test_parse_recall_query_tool_arguments_rejects_bool_top_k():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["hotel"],
            "destination": "",
            "keywords": ["住宿"],
            "top_k": True,
            "reason": "bool top_k",
        }
    )

    assert plan.fallback_used == "invalid_query_plan"


def test_parse_recall_query_tool_arguments_clamps_large_top_k():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["hotel"],
            "destination": "",
            "keywords": ["住宿"],
            "top_k": 999999,
            "reason": "huge int top_k",
        }
    )

    assert plan.top_k == 10


def test_parse_recall_query_tool_arguments_requires_reason_string():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["hotel"],
            "destination": "",
            "keywords": ["住宿"],
            "top_k": 3,
            "reason": 123,
        }
    )

    assert plan.fallback_used == "invalid_query_plan"


def test_parse_recall_query_tool_arguments_allows_source_specific_buckets():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "episode_slice",
            "domains": ["hotel"],
            "destination": "京都",
            "keywords": ["住宿"],
            "top_k": 4,
            "reason": "past_trip_experience_recall -> hotel domain -> destination Kyoto",
        }
    )

    assert plan.source == "episode_slice"
    assert plan.buckets == []
    assert plan.destination == "京都"
    assert plan.top_k == 4


def test_allowed_recall_domains_matches_system_contract():
    assert ALLOWED_RECALL_DOMAINS == (
        "itinerary",
        "pace",
        "food",
        "hotel",
        "accommodation",
        "flight",
        "train",
        "budget",
        "family",
        "accessibility",
        "planning_style",
        "documents",
        "general",
    )


def test_fallback_retrieval_plan_is_conservative():
    plan = fallback_retrieval_plan()

    assert plan.source == "hybrid_history"
    assert plan.buckets == ["constraints", "rejections", "stable_preferences"]
    assert plan.destination == ""
    assert plan.domains == []
    assert plan.keywords == []
    assert plan.top_k == 5


def test_query_tool_schema_uses_source_aware_branches():
    tool = _build_recall_query_tool()
    parameters = tool["parameters"]

    assert "oneOf" in parameters
    branches = parameters["oneOf"]
    assert len(branches) == 3

    profile_branch = next(
        branch
        for branch in branches
        if branch["properties"]["source"]["const"] == "profile"
    )
    episode_branch = next(
        branch
        for branch in branches
        if branch["properties"]["source"]["const"] == "episode_slice"
    )

    assert "buckets" in profile_branch["required"]
    assert "buckets" not in episode_branch["properties"]
