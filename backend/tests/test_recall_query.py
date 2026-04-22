from memory.recall_query import fallback_retrieval_plan, parse_recall_query_tool_arguments


def test_parse_recall_query_tool_arguments_honors_schema_fields():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences", "constraints"],
            "domains": ["hotel", "accommodation"],
            "keywords": ["住宿", "酒店"],
            "aliases": ["住哪里", "住宿偏好"],
            "strictness": "soft",
            "top_k": 8,
            "reason": "user wants to reuse accommodation preference",
        }
    )

    assert plan.source == "profile"
    assert plan.buckets == ["stable_preferences", "constraints"]
    assert plan.domains == ["hotel", "accommodation"]
    assert plan.aliases == ["住哪里", "住宿偏好"]
    assert plan.strictness == "soft"
    assert plan.top_k == 8


def test_parse_recall_query_tool_arguments_rejects_invalid_strictness():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["hotel"],
            "keywords": ["住宿"],
            "aliases": ["住哪里"],
            "strictness": "aggressive",
            "top_k": 8,
            "reason": "bad strictness",
        }
    )

    assert plan.strictness == "soft"


def test_parse_recall_query_tool_arguments_rejects_non_profile_source():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "episode_slice",
            "buckets": ["stable_preferences"],
            "domains": [],
            "keywords": [],
            "aliases": [],
            "strictness": "soft",
            "top_k": 5,
            "reason": "bad source",
        }
    )

    assert plan.fallback_used == "invalid_query_plan"
    assert plan.reason == "invalid_query_plan"
    assert plan.source == "profile"
    assert plan.buckets == ["constraints", "rejections", "stable_preferences"]
    assert plan.domains == []
    assert plan.keywords == []
    assert plan.aliases == []
    assert plan.strictness == "soft"
    assert plan.top_k == 5


def test_parse_recall_query_tool_arguments_uses_fallback_for_none_payload():
    plan = parse_recall_query_tool_arguments(None)

    assert plan == fallback_retrieval_plan()


def test_parse_recall_query_tool_arguments_uses_safe_default_for_invalid_top_k():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["hotel"],
            "keywords": ["住宿"],
            "aliases": ["住哪里"],
            "strictness": "soft",
            "top_k": "abc",
            "reason": "bad top_k",
        }
    )

    assert plan.top_k == 5


def test_parse_recall_query_tool_arguments_rejects_bool_top_k():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["hotel"],
            "keywords": ["住宿"],
            "aliases": ["住哪里"],
            "strictness": "soft",
            "top_k": True,
            "reason": "bool top_k",
        }
    )

    assert plan.top_k == 5


def test_parse_recall_query_tool_arguments_rejects_non_positive_top_k():
    zero_plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["hotel"],
            "keywords": ["住宿"],
            "aliases": ["住哪里"],
            "strictness": "soft",
            "top_k": 0,
            "reason": "zero top_k",
        }
    )
    negative_plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["hotel"],
            "keywords": ["住宿"],
            "aliases": ["住哪里"],
            "strictness": "soft",
            "top_k": -1,
            "reason": "negative top_k",
        }
    )

    assert zero_plan.top_k == 5
    assert negative_plan.top_k == 5


def test_parse_recall_query_tool_arguments_clamps_large_top_k():
    huge_int_plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["hotel"],
            "keywords": ["住宿"],
            "aliases": ["住哪里"],
            "strictness": "soft",
            "top_k": 999999,
            "reason": "huge int top_k",
        }
    )
    huge_string_plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences"],
            "domains": ["hotel"],
            "keywords": ["住宿"],
            "aliases": ["住哪里"],
            "strictness": "soft",
            "top_k": "1000000",
            "reason": "huge string top_k",
        }
    )

    assert huge_int_plan.top_k == 10
    assert huge_string_plan.top_k == 10


def test_parse_recall_query_tool_arguments_ignores_invalid_collection_types():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": "stable_preferences",
            "domains": ["hotel"],
            "keywords": ["住宿"],
            "aliases": {"name": "住哪里"},
            "strictness": "soft",
            "top_k": 8,
            "reason": "bad collection types",
        }
    )

    assert plan.buckets == []
    assert plan.aliases == []


def test_parse_recall_query_tool_arguments_rejects_mixed_type_lists():
    plan = parse_recall_query_tool_arguments(
        {
            "source": "profile",
            "buckets": ["stable_preferences", 123],
            "domains": ["hotel"],
            "keywords": ["住宿"],
            "aliases": ["住哪里", 123],
            "strictness": "soft",
            "top_k": 8,
            "reason": "mixed type lists",
        }
    )

    assert plan.buckets == []
    assert plan.aliases == []


def test_fallback_retrieval_plan_is_conservative():
    plan = fallback_retrieval_plan()

    assert plan.source == "profile"
    assert plan.buckets == ["constraints", "rejections", "stable_preferences"]
    assert plan.strictness == "soft"
    assert plan.top_k == 5
