from __future__ import annotations

from state.models import TravelPlanState
from tools.base import ToolDef

from .append_tools import (
    make_add_constraints_tool,
    make_add_destination_candidate_tool,
    make_add_preferences_tool,
    make_set_destination_candidates_tool,
)
from .backtrack import make_request_backtrack_tool
from .daily_plans import make_append_day_plan_tool, make_replace_daily_plans_tool
from .phase3_tools import (
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
from .trip_basics import make_update_trip_basics_tool

PLAN_WRITER_TOOL_NAMES = {
    "update_plan_state",
    "update_trip_basics",
    "set_trip_brief",
    "set_candidate_pool",
    "set_shortlist",
    "set_skeleton_plans",
    "select_skeleton",
    "set_transport_options",
    "select_transport",
    "set_accommodation_options",
    "set_accommodation",
    "set_risks",
    "set_alternatives",
    "add_preferences",
    "add_constraints",
    "add_destination_candidate",
    "set_destination_candidates",
    "append_day_plan",
    "replace_daily_plans",
    "request_backtrack",
}


def make_all_plan_tools(plan: TravelPlanState) -> list[ToolDef]:
    return [
        make_add_constraints_tool(plan),
        make_add_destination_candidate_tool(plan),
        make_add_preferences_tool(plan),
        make_set_destination_candidates_tool(plan),
        make_request_backtrack_tool(plan),
        make_append_day_plan_tool(plan),
        make_replace_daily_plans_tool(plan),
        make_select_skeleton_tool(plan),
        make_select_transport_tool(plan),
        make_set_accommodation_options_tool(plan),
        make_set_accommodation_tool(plan),
        make_set_alternatives_tool(plan),
        make_set_candidate_pool_tool(plan),
        make_set_risks_tool(plan),
        make_set_shortlist_tool(plan),
        make_set_skeleton_plans_tool(plan),
        make_set_transport_options_tool(plan),
        make_set_trip_brief_tool(plan),
        make_update_trip_basics_tool(plan),
    ]


__all__ = [
    "PLAN_WRITER_TOOL_NAMES",
    "make_all_plan_tools",
    "make_add_constraints_tool",
    "make_add_destination_candidate_tool",
    "make_add_preferences_tool",
    "make_set_destination_candidates_tool",
    "make_request_backtrack_tool",
    "make_append_day_plan_tool",
    "make_replace_daily_plans_tool",
    "make_select_skeleton_tool",
    "make_select_transport_tool",
    "make_set_accommodation_options_tool",
    "make_set_accommodation_tool",
    "make_set_alternatives_tool",
    "make_set_candidate_pool_tool",
    "make_set_risks_tool",
    "make_set_shortlist_tool",
    "make_set_skeleton_plans_tool",
    "make_set_transport_options_tool",
    "make_set_trip_brief_tool",
    "make_update_trip_basics_tool",
]
