from __future__ import annotations

from state.models import TravelPlanState
from tools.engine import ToolEngine
from tools.plan_tools import make_all_plan_tools


def register_all_plan_tools(engine: ToolEngine, plan: TravelPlanState) -> None:
    for tool in make_all_plan_tools(plan):
        engine.register(tool)

