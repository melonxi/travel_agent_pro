from __future__ import annotations

from pathlib import Path

from tools.ai_travel_search import make_ai_travel_search_tool
from tools.assemble_day_plan import make_assemble_day_plan_tool
from tools.calculate_route import make_calculate_route_tool
from tools.check_availability import make_check_availability_tool
from tools.check_weather import make_check_weather_tool
from tools.engine import ToolEngine
from tools.generate_summary import make_generate_summary_tool
from tools.get_poi_info import make_get_poi_info_tool
from tools.optimize_day_route import make_optimize_day_route_tool
from tools.plan_tools import make_all_plan_tools
from tools.quick_travel_search import make_quick_travel_search_tool
from tools.search_accommodations import make_search_accommodations_tool
from tools.search_flights import make_search_flights_tool
from tools.search_trains import make_search_trains_tool
from tools.search_travel_services import make_search_travel_services_tool
from tools.source_registry import SourceRegistry
from tools.web_search import make_web_search_tool
from tools.xiaohongshu_search import (
    make_xiaohongshu_get_comments_tool,
    make_xiaohongshu_read_note_tool,
    make_xiaohongshu_search_notes_tool,
)


def build_source_registry(config) -> SourceRegistry:
    """来源注册表落在 data_dir/sources；与 phase3 candidate store 同级约定。"""
    return SourceRegistry(Path(config.data_dir) / "sources")


def build_tool_engine(*, config, plan) -> ToolEngine:
    tool_engine = ToolEngine()
    source_registry = build_source_registry(config)

    flyai_client = None
    if config.flyai.enabled:
        from tools.flyai_client import FlyAIClient

        flyai_client = FlyAIClient(
            timeout=config.flyai.cli_timeout,
            api_key=config.flyai.api_key,
        )

    for plan_tool in make_all_plan_tools(plan, source_registry=source_registry):
        tool_engine.register(plan_tool)
    # P2-8：search_flights 仅依赖 flyai（Amadeus sandbox 分支已删除）。
    # flyai 不可用时不注册，由 lock 阶段 prompt 引导用 web_search 查航班价格带。
    if flyai_client is not None:
        tool_engine.register(make_search_flights_tool(config.api_keys, flyai_client))
    # search_trains 走 12306 直连,不依赖 flyai。
    tool_engine.register(make_search_trains_tool())
    # 以下工具的唯一数据源是 flyai,client 不可用时不注册。
    if flyai_client is not None:
        tool_engine.register(make_ai_travel_search_tool(flyai_client))
        tool_engine.register(make_quick_travel_search_tool(flyai_client))
        tool_engine.register(make_search_travel_services_tool(flyai_client))
    tool_engine.register(make_search_accommodations_tool(config.api_keys, flyai_client))
    tool_engine.register(make_get_poi_info_tool(config.api_keys, flyai_client))
    tool_engine.register(make_calculate_route_tool(config.api_keys))
    tool_engine.register(make_assemble_day_plan_tool())
    tool_engine.register(make_optimize_day_route_tool())
    tool_engine.register(make_check_availability_tool(config.api_keys))
    tool_engine.register(make_check_weather_tool(config.api_keys))
    tool_engine.register(make_generate_summary_tool(plan))
    tool_engine.register(
        make_web_search_tool(
            config.api_keys,
            source_registry=source_registry,
            session_id=plan.session_id,
        )
    )
    if config.xhs.enabled:
        tool_engine.register(make_xiaohongshu_search_notes_tool(config.xhs))
        tool_engine.register(make_xiaohongshu_read_note_tool(config.xhs))
        tool_engine.register(make_xiaohongshu_get_comments_tool(config.xhs))

    return tool_engine
