import pytest

from state.models import DateRange, TravelPlanState, infer_phase3_step_from_state
from tools.base import ToolError
from tools.plan_tools.phase3_tools import (
    make_select_skeleton_tool,
    make_set_skeleton_plans_tool,
)


@pytest.fixture
def plan():
    return TravelPlanState(session_id="s1")


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
