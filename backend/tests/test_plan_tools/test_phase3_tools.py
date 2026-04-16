"""Unit tests for Category A Phase 3 tools."""

from __future__ import annotations

import pytest

from state.models import Accommodation, DateRange, TravelPlanState, infer_phase3_step_from_state
from tools.base import ToolError
from tools.plan_tools.phase3_tools import (
    make_select_skeleton_tool,
    make_select_transport_tool,
    make_set_accommodation_options_tool,
    make_set_accommodation_tool,
    make_set_alternatives_tool,
    make_set_candidate_pool_tool,
    make_set_risks_tool,
    make_set_shortlist_tool,
    make_set_skeleton_plans_tool,
    make_set_transport_options_tool,
    make_set_trip_brief_tool,
)


@pytest.fixture
def plan():
    return TravelPlanState(session_id="s1")


@pytest.mark.parametrize(
    (
        "factory",
        "expected_name",
        "expected_label",
        "expected_required",
        "expected_properties",
        "expected_phases",
    ),
    [
        (
            make_set_skeleton_plans_tool,
            "set_skeleton_plans",
            "写入骨架方案",
            ["plans"],
            {"plans"},
            [3],
        ),
        (
            make_select_skeleton_tool,
            "select_skeleton",
            "锁定骨架方案",
            ["id"],
            {"id"},
            [3],
        ),
        (
            make_set_candidate_pool_tool,
            "set_candidate_pool",
            "写入候选池",
            ["pool"],
            {"pool"},
            [3],
        ),
        (
            make_set_shortlist_tool,
            "set_shortlist",
            "写入候选短名单",
            ["items"],
            {"items"},
            [3],
        ),
        (
            make_set_transport_options_tool,
            "set_transport_options",
            "写入交通候选",
            ["options"],
            {"options"},
            [3],
        ),
        (
            make_select_transport_tool,
            "select_transport",
            "锁定交通方案",
            ["choice"],
            {"choice"},
            [3],
        ),
        (
            make_set_accommodation_options_tool,
            "set_accommodation_options",
            "写入住宿候选",
            ["options"],
            {"options"},
            [3],
        ),
        (
            make_set_accommodation_tool,
            "set_accommodation",
            "锁定住宿",
            ["area"],
            {"area", "hotel"},
            [3, 5],
        ),
        (
            make_set_risks_tool,
            "set_risks",
            "写入风险点",
            ["list"],
            {"list"},
            [3, 5],
        ),
        (
            make_set_alternatives_tool,
            "set_alternatives",
            "写入备选方案",
            ["list"],
            {"list"},
            [3, 5],
        ),
        (
            make_set_trip_brief_tool,
            "set_trip_brief",
            "更新旅行画像",
            ["fields"],
            {"fields"},
            [3],
        ),
    ],
)
def test_phase3_tool_metadata(
    plan,
    factory,
    expected_name,
    expected_label,
    expected_required,
    expected_properties,
    expected_phases,
):
    tool_fn = factory(plan)

    assert tool_fn.name == expected_name
    assert tool_fn.side_effect == "write"
    assert tool_fn.human_label == expected_label
    assert tool_fn.phases == expected_phases
    assert tool_fn.parameters["type"] == "object"
    assert tool_fn.parameters["required"] == expected_required
    assert set(tool_fn.parameters["properties"]) == expected_properties


@pytest.mark.asyncio
async def test_set_skeleton_plans_records_counts(plan):
    tool_fn = make_set_skeleton_plans_tool(plan)
    plan.skeleton_plans = [{"id": "legacy", "name": "旧方案"}]

    result = await tool_fn(
        plans=[
            {"id": " plan-a ", "name": " 轻松版 ", "days": [], "tradeoffs": {}},
            {"id": "plan-b", "name": "平衡版", "days": [], "tradeoffs": {}},
        ]
    )

    assert result == {
        "updated_field": "skeleton_plans",
        "count": 2,
        "previous_count": 1,
    }
    assert plan.skeleton_plans == [
        {"id": "plan-a", "name": "轻松版", "days": [], "tradeoffs": {}},
        {"id": "plan-b", "name": "平衡版", "days": [], "tradeoffs": {}},
    ]


@pytest.mark.asyncio
async def test_set_skeleton_plans_rejects_non_list_input(plan):
    tool_fn = make_set_skeleton_plans_tool(plan)

    with pytest.raises(ToolError, match="plans") as exc_info:
        await tool_fn(plans="not a list")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_skeleton_plans_rejects_non_dict_entries(plan):
    tool_fn = make_set_skeleton_plans_tool(plan)

    with pytest.raises(ToolError, match=r"plans\[0\]") as exc_info:
        await tool_fn(plans=["plan-a"])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_skeleton_plans_rejects_missing_name(plan):
    tool_fn = make_set_skeleton_plans_tool(plan)

    with pytest.raises(ToolError, match="name") as exc_info:
        await tool_fn(plans=[{"id": "plan-a"}])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [None, "", "   "])
async def test_set_skeleton_plans_rejects_invalid_id_values(plan, bad_id):
    tool_fn = make_set_skeleton_plans_tool(plan)

    with pytest.raises(ToolError, match="id") as exc_info:
        await tool_fn(plans=[{"id": bad_id, "name": "Valid"}])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_name", [None, "", "   "])
async def test_set_skeleton_plans_rejects_invalid_name_values(plan, bad_name):
    tool_fn = make_set_skeleton_plans_tool(plan)

    with pytest.raises(ToolError, match="name") as exc_info:
        await tool_fn(plans=[{"id": "plan-a", "name": bad_name}])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_skeleton_plans_rejects_duplicate_ids(plan):
    tool_fn = make_set_skeleton_plans_tool(plan)

    with pytest.raises(ToolError, match="重复") as exc_info:
        await tool_fn(
            plans=[
                {"id": "dup", "name": "A"},
                {"id": "dup", "name": "B"},
            ]
        )

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_skeleton_plans_normalizes_trimmed_id_and_name(plan):
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": " plan-a ", "name": " 轻松版 "}])

    assert plan.skeleton_plans == [{"id": "plan-a", "name": "轻松版"}]


@pytest.mark.asyncio
async def test_set_skeleton_plans_reconciles_selected_legacy_id(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [{"id": " plan-a ", "name": " Legacy "}]
    plan.selected_skeleton_id = " plan-a "
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": " plan-a ", "name": " Legacy "}])

    assert plan.selected_skeleton_id == "plan-a"
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "lock"


@pytest.mark.asyncio
async def test_set_skeleton_plans_clears_unproven_selection_without_previous_skeletons(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.selected_skeleton_id = " plan-a "
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": " plan-a ", "name": " Legacy "}])

    assert plan.selected_skeleton_id is None
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "skeleton"


@pytest.mark.asyncio
async def test_set_skeleton_plans_reconciles_selected_legacy_name(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [{"id": "legacy-id", "name": " Legacy "}]
    plan.selected_skeleton_id = " Legacy "
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": " plan-a ", "name": " Legacy "}])

    assert plan.selected_skeleton_id == "plan-a"
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "lock"


@pytest.mark.asyncio
async def test_set_skeleton_plans_clears_ambiguous_previous_id_matches(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [
        {"id": "plan-a", "name": "A"},
        {"id": " plan-a ", "name": "B"},
    ]
    plan.selected_skeleton_id = "plan-a"
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": "plan-a", "name": "New"}])

    assert plan.selected_skeleton_id is None
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "skeleton"


@pytest.mark.asyncio
async def test_set_skeleton_plans_clears_ambiguous_previous_name_matches(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [
        {"id": "a", "name": " Legacy "},
        {"id": "b", "name": "Legacy"},
    ]
    plan.selected_skeleton_id = " Legacy "
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": "plan-a", "name": "Legacy"}])

    assert plan.selected_skeleton_id is None
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "skeleton"


@pytest.mark.asyncio
async def test_set_skeleton_plans_clears_cross_matched_previous_id_and_name(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [
        {"id": "plan-a", "name": "Alpha"},
        {"id": "plan-b", "name": "plan-a"},
    ]
    plan.selected_skeleton_id = "plan-a"
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": "plan-a", "name": "New Alpha"}])

    assert plan.selected_skeleton_id is None
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "skeleton"


@pytest.mark.asyncio
async def test_set_skeleton_plans_reconciles_same_entry_matching_old_id_and_name(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [{"id": "A", "name": "A"}]
    plan.selected_skeleton_id = "A"
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": "B", "name": "A"}])

    assert plan.selected_skeleton_id == "B"
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "lock"


@pytest.mark.asyncio
async def test_set_skeleton_plans_clears_preserved_id_when_rewritten_state_is_ambiguous(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [{"id": "plan-a", "name": "Alpha"}]
    plan.selected_skeleton_id = "plan-a"
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(
        plans=[
            {"id": "plan-a", "name": "New Alpha"},
            {"id": "other", "name": "plan-a"},
        ]
    )

    assert plan.selected_skeleton_id is None
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "skeleton"


@pytest.mark.asyncio
async def test_set_skeleton_plans_clears_canonicalized_id_when_rewritten_state_is_ambiguous(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [{"id": "old", "name": "Legacy"}]
    plan.selected_skeleton_id = "Legacy"
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(
        plans=[
            {"id": "plan-a", "name": "Legacy"},
            {"id": "other", "name": "plan-a"},
        ]
    )

    assert plan.selected_skeleton_id is None
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "skeleton"


@pytest.mark.asyncio
async def test_set_skeleton_plans_does_not_remap_missing_selected_id_by_name(plan):
    plan.skeleton_plans = [{"id": "plan-a", "name": "A"}]
    plan.selected_skeleton_id = "plan-a"
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": "plan-b", "name": "plan-a"}])

    assert plan.selected_skeleton_id is None


@pytest.mark.asyncio
async def test_set_skeleton_plans_does_not_preserve_legacy_name_as_new_id(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [{"id": "old-id", "name": " Legacy "}]
    plan.selected_skeleton_id = " Legacy "
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": " Legacy ", "name": "Other"}])

    assert plan.selected_skeleton_id is None
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "skeleton"


@pytest.mark.asyncio
async def test_set_skeleton_plans_clears_trimmed_legacy_name_when_name_disappears(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [{"id": "old-id", "name": "plan-a"}]
    plan.selected_skeleton_id = "plan-a"
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": "plan-a", "name": "Other"}])

    assert plan.selected_skeleton_id is None
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "skeleton"


@pytest.mark.asyncio
async def test_set_skeleton_plans_clears_stale_unknown_selection(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [{"id": "old-id", "name": "Old"}]
    plan.selected_skeleton_id = "plan-a"
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(plans=[{"id": "plan-a", "name": "New"}])

    assert plan.selected_skeleton_id is None
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "skeleton"


@pytest.mark.asyncio
async def test_set_skeleton_plans_clears_ambiguous_legacy_name_matches(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [{"id": "old-id", "name": " Legacy "}]
    plan.selected_skeleton_id = " Legacy "
    tool_fn = make_set_skeleton_plans_tool(plan)

    await tool_fn(
        plans=[
            {"id": "c", "name": "Legacy"},
            {"id": "d", "name": "Legacy"},
        ]
    )

    assert plan.selected_skeleton_id is None
    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "skeleton"


@pytest.mark.asyncio
async def test_select_skeleton_handles_malformed_legacy_ids(plan):
    plan.skeleton_plans = [{"id": None}, {"id": "valid", "name": "Valid"}]
    tool_fn = make_select_skeleton_tool(plan)

    with pytest.raises(ToolError, match="missing") as exc_info:
        await tool_fn(id="missing")

    assert exc_info.value.error_code == "INVALID_VALUE"
    assert exc_info.value.suggestion == "可选 id: valid"


@pytest.mark.asyncio
async def test_select_skeleton_normalizes_trimmed_input_id(plan):
    plan.skeleton_plans = [{"id": "plan-a", "name": "轻松版"}]
    tool_fn = make_select_skeleton_tool(plan)

    result = await tool_fn(id=" plan-a ")

    assert result["new_value"] == "plan-a"
    assert plan.selected_skeleton_id == "plan-a"


@pytest.mark.asyncio
async def test_select_skeleton_handles_legacy_whitespace_padded_ids(plan):
    plan.phase = 3
    plan.dates = DateRange(start="2026-05-01", end="2026-05-03")
    plan.trip_brief = {"goal": "慢旅行"}
    plan.skeleton_plans = [{"id": " plan-a ", "name": "Legacy"}]
    tool_fn = make_select_skeleton_tool(plan)

    result = await tool_fn(id=" plan-a ")

    assert infer_phase3_step_from_state(
        phase=plan.phase,
        dates=plan.dates,
        trip_brief=plan.trip_brief,
        candidate_pool=plan.candidate_pool,
        shortlist=plan.shortlist,
        skeleton_plans=plan.skeleton_plans,
        selected_skeleton_id=plan.selected_skeleton_id,
        accommodation=plan.accommodation,
    ) == "lock"
    assert result["new_value"] == plan.selected_skeleton_id


@pytest.mark.asyncio
async def test_select_skeleton_rejects_colliding_normalized_legacy_ids(plan):
    plan.skeleton_plans = [
        {"id": "plan-a", "name": "A"},
        {"id": " plan-a ", "name": "Legacy A"},
    ]
    tool_fn = make_select_skeleton_tool(plan)

    with pytest.raises(ToolError, match="冲突") as exc_info:
        await tool_fn(id="plan-a")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_select_skeleton_rejects_duplicate_exact_legacy_ids(plan):
    plan.skeleton_plans = [
        {"id": "dup", "name": "A"},
        {"id": "dup", "name": "B"},
    ]
    tool_fn = make_select_skeleton_tool(plan)

    with pytest.raises(ToolError, match="冲突") as exc_info:
        await tool_fn(id="dup")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_select_skeleton_suggestion_excludes_colliding_legacy_ids(plan):
    plan.skeleton_plans = [
        {"id": "plan-a", "name": "A"},
        {"id": " plan-a ", "name": "Legacy A"},
        {"id": "plan-b", "name": "B"},
    ]
    tool_fn = make_select_skeleton_tool(plan)

    with pytest.raises(ToolError, match="missing") as exc_info:
        await tool_fn(id="missing")

    assert exc_info.value.error_code == "INVALID_VALUE"
    assert exc_info.value.suggestion == "可选 id: plan-b"


@pytest.mark.asyncio
async def test_select_skeleton_handles_all_invalid_legacy_ids(plan):
    plan.skeleton_plans = [{"id": None}, {"name": "missing-id"}]
    tool_fn = make_select_skeleton_tool(plan)

    with pytest.raises(ToolError, match="missing") as exc_info:
        await tool_fn(id="missing")

    assert exc_info.value.error_code == "INVALID_VALUE"
    assert exc_info.value.suggestion == "可选 id: (无已写入骨架)"


@pytest.mark.asyncio
async def test_select_skeleton_records_previous_value(plan):
    plan.skeleton_plans = [
        {"id": "plan-a", "name": "轻松版"},
        {"id": "plan-b", "name": "平衡版"},
    ]
    plan.selected_skeleton_id = "plan-b"
    tool_fn = make_select_skeleton_tool(plan)

    result = await tool_fn(id="plan-a")

    assert result == {
        "updated_field": "selected_skeleton_id",
        "new_value": "plan-a",
        "previous_value": "plan-b",
    }
    assert plan.selected_skeleton_id == "plan-a"


@pytest.mark.asyncio
async def test_select_skeleton_rejects_empty_id(plan):
    tool_fn = make_select_skeleton_tool(plan)

    with pytest.raises(ToolError, match="id") as exc_info:
        await tool_fn(id=" ")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_candidate_pool_replaces_existing_items(plan):
    tool_fn = make_set_candidate_pool_tool(plan)
    plan.candidate_pool = [{"name": "Old"}]

    result = await tool_fn(pool=[{"name": "A"}, {"name": "B"}])

    assert result == {
        "updated_field": "candidate_pool",
        "count": 2,
        "previous_count": 1,
    }
    assert plan.candidate_pool == [{"name": "A"}, {"name": "B"}]


@pytest.mark.asyncio
async def test_set_candidate_pool_rejects_non_list(plan):
    tool_fn = make_set_candidate_pool_tool(plan)

    with pytest.raises(ToolError, match="pool") as exc_info:
        await tool_fn(pool="not a list")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_candidate_pool_rejects_non_dict_elements(plan):
    tool_fn = make_set_candidate_pool_tool(plan)

    with pytest.raises(ToolError, match=r"pool\[0\]") as exc_info:
        await tool_fn(pool=[1, 2, 3])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_shortlist_replaces_existing_items(plan):
    tool_fn = make_set_shortlist_tool(plan)
    plan.shortlist = [{"name": "Old"}]

    result = await tool_fn(items=[{"name": "A"}])

    assert result == {
        "updated_field": "shortlist",
        "count": 1,
        "previous_count": 1,
    }
    assert plan.shortlist == [{"name": "A"}]


@pytest.mark.asyncio
async def test_set_shortlist_rejects_non_list(plan):
    tool_fn = make_set_shortlist_tool(plan)

    with pytest.raises(ToolError, match="items") as exc_info:
        await tool_fn(items="not a list")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_shortlist_rejects_non_dict_elements(plan):
    tool_fn = make_set_shortlist_tool(plan)

    with pytest.raises(ToolError, match=r"items\[0\]") as exc_info:
        await tool_fn(items=["bad"])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_transport_options_replaces_existing_items(plan):
    tool_fn = make_set_transport_options_tool(plan)
    plan.transport_options = [{"type": "train"}]

    result = await tool_fn(options=[{"type": "flight"}, {"type": "train"}])

    assert result == {
        "updated_field": "transport_options",
        "count": 2,
        "previous_count": 1,
    }
    assert plan.transport_options == [{"type": "flight"}, {"type": "train"}]


@pytest.mark.asyncio
async def test_set_transport_options_rejects_non_list(plan):
    tool_fn = make_set_transport_options_tool(plan)

    with pytest.raises(ToolError, match="options") as exc_info:
        await tool_fn(options="flight")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_transport_options_rejects_non_dict_elements(plan):
    tool_fn = make_set_transport_options_tool(plan)

    with pytest.raises(ToolError, match=r"options\[0\]") as exc_info:
        await tool_fn(options=["flight"])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_select_transport_writes_choice_and_previous_value(plan):
    tool_fn = make_select_transport_tool(plan)
    plan.selected_transport = {"type": "train", "price": 800}

    result = await tool_fn(choice={"type": "flight", "price": 1200})

    assert result == {
        "updated_field": "selected_transport",
        "new_value": {"type": "flight", "price": 1200},
        "previous_value": {"type": "train", "price": 800},
    }
    assert plan.selected_transport == {"type": "flight", "price": 1200}


@pytest.mark.asyncio
async def test_select_transport_rejects_non_dict(plan):
    tool_fn = make_select_transport_tool(plan)

    with pytest.raises(ToolError, match="choice") as exc_info:
        await tool_fn(choice="flight")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_accommodation_options_replaces_existing_items(plan):
    tool_fn = make_set_accommodation_options_tool(plan)
    plan.accommodation_options = [{"name": "Hotel Old"}]

    result = await tool_fn(options=[{"name": "Hotel A"}])

    assert result == {
        "updated_field": "accommodation_options",
        "count": 1,
        "previous_count": 1,
    }
    assert plan.accommodation_options == [{"name": "Hotel A"}]


@pytest.mark.asyncio
async def test_set_accommodation_options_rejects_non_list(plan):
    tool_fn = make_set_accommodation_options_tool(plan)

    with pytest.raises(ToolError, match="options") as exc_info:
        await tool_fn(options="hotel")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_accommodation_options_rejects_non_dict_elements(plan):
    tool_fn = make_set_accommodation_options_tool(plan)

    with pytest.raises(ToolError, match=r"options\[0\]") as exc_info:
        await tool_fn(options=["hotel"])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_accommodation_writes_trimmed_area_and_previous_value(plan):
    tool_fn = make_set_accommodation_tool(plan)
    plan.accommodation = Accommodation(area="东京站", hotel="Old Hotel")

    result = await tool_fn(area=" 新宿 ", hotel="Hyatt Regency")

    assert result == {
        "updated_field": "accommodation",
        "new_value": {"area": "新宿", "hotel": "Hyatt Regency"},
        "previous_value": {"area": "东京站", "hotel": "Old Hotel"},
    }
    assert plan.accommodation.area == "新宿"
    assert plan.accommodation.hotel == "Hyatt Regency"


@pytest.mark.asyncio
async def test_set_accommodation_supports_area_only(plan):
    tool_fn = make_set_accommodation_tool(plan)

    result = await tool_fn(area="银座")

    assert result["new_value"] == {"area": "银座", "hotel": None}
    assert plan.accommodation.area == "银座"
    assert plan.accommodation.hotel is None


@pytest.mark.asyncio
async def test_set_accommodation_rejects_empty_area(plan):
    tool_fn = make_set_accommodation_tool(plan)

    with pytest.raises(ToolError, match="area") as exc_info:
        await tool_fn(area="")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_risks_replaces_existing_items(plan):
    tool_fn = make_set_risks_tool(plan)
    plan.risks = [{"type": "budget", "desc": "旧风险"}]

    result = await tool_fn(list=[{"type": "weather", "desc": "台风"}])

    assert result == {
        "updated_field": "risks",
        "count": 1,
        "previous_count": 1,
    }
    assert plan.risks == [{"type": "weather", "desc": "台风"}]


@pytest.mark.asyncio
async def test_set_risks_rejects_non_list(plan):
    tool_fn = make_set_risks_tool(plan)

    with pytest.raises(ToolError, match="list") as exc_info:
        await tool_fn(list="typhoon")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_risks_rejects_non_dict_elements(plan):
    tool_fn = make_set_risks_tool(plan)

    with pytest.raises(ToolError, match=r"list\[0\]") as exc_info:
        await tool_fn(list=["typhoon"])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_alternatives_replaces_existing_items(plan):
    tool_fn = make_set_alternatives_tool(plan)
    plan.alternatives = [{"name": "旧备选"}]

    result = await tool_fn(list=[{"name": "备选A"}, {"name": "备选B"}])

    assert result == {
        "updated_field": "alternatives",
        "count": 2,
        "previous_count": 1,
    }
    assert plan.alternatives == [{"name": "备选A"}, {"name": "备选B"}]


@pytest.mark.asyncio
async def test_set_alternatives_rejects_non_list(plan):
    tool_fn = make_set_alternatives_tool(plan)

    with pytest.raises(ToolError, match="list") as exc_info:
        await tool_fn(list="alt_a")

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_alternatives_rejects_non_dict_elements(plan):
    tool_fn = make_set_alternatives_tool(plan)

    with pytest.raises(ToolError, match=r"list\[0\]") as exc_info:
        await tool_fn(list=["alt_a"])

    assert exc_info.value.error_code == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_set_trip_brief_merges_fields_and_returns_previous_value(plan):
    tool_fn = make_set_trip_brief_tool(plan)
    plan.trip_brief = {"goal": "old"}

    result = await tool_fn(fields={"pace": "relaxed"})

    assert result == {
        "updated_field": "trip_brief",
        "new_value": {"goal": "old", "pace": "relaxed"},
        "previous_value": {"goal": "old"},
    }
    assert plan.trip_brief == {"goal": "old", "pace": "relaxed"}


@pytest.mark.asyncio
async def test_set_trip_brief_adds_new_fields(plan):
    tool_fn = make_set_trip_brief_tool(plan)

    result = await tool_fn(fields={"goal": "慢旅行", "pace": "relaxed"})

    assert result["new_value"] == {"goal": "慢旅行", "pace": "relaxed"}
    assert plan.trip_brief == {"goal": "慢旅行", "pace": "relaxed"}


@pytest.mark.asyncio
async def test_set_trip_brief_rejects_non_dict(plan):
    tool_fn = make_set_trip_brief_tool(plan)

    with pytest.raises(ToolError, match="fields") as exc_info:
        await tool_fn(fields="not a dict")

    assert exc_info.value.error_code == "INVALID_VALUE"
