# backend/tools/web_search.py
from __future__ import annotations

import httpx
from tools.base import ToolError, tool
from tools.source_registry import SourceRegistry

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "需要搜索的实时问题,建议写成完整意图,"
                "如 '东京迪士尼门票价格 2026'、'日本签证最新政策'、"
            ),
        },
        "search_depth": {
            "type": "string",
            "enum": ["basic", "advanced"],
            "description": "搜索深度提示。建议使用 basic 或 advanced。当前实现会原样透传给搜索服务,默认 basic。",
        },
        "max_results": {
            "type": "integer",
            "description": "期望返回的结果数量。当前实现会自动限制在 1 到 10,默认 5。",
        },
        "include_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "限定搜索结果只来自这些域名,最多 5 个。"
                "查 UGC 攻略/真实体验时用 ['xiaohongshu.com', 'mafengwo.cn', 'qyer.com'];"
                "查官方信息时可限定官网域名。缺省不限制。"
            ),
        },
        "exclude_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "从结果中排除这些域名,最多 5 个。缺省不排除。",
        },
    },
    "required": ["query"],
}

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"

_MAX_DOMAIN_FILTERS = 5


def _normalize_domains(value: list[str] | None) -> list[str]:
    if not value:
        return []
    domains = [d.strip() for d in value if isinstance(d, str) and d.strip()]
    return domains[:_MAX_DOMAIN_FILTERS]


def make_web_search_tool(
    api_keys,
    *,
    source_registry: SourceRegistry | None = None,
    session_id: str | None = None,
) -> object:
    tavily_key = api_keys.tavily if api_keys else ""

    @tool(
        name="web_search",
        description="""通用实时网络搜索工具,用于公开信息检索,支持域内搜索。
Use when:
  - 你需要最新价格、政策变动、开放变化、新闻型更新或通用攻略信息。
  - 你需要 UGC 真实体验、攻略、排队/避坑信息:用 include_domains 限定
    ['xiaohongshu.com', 'mafengwo.cn', 'qyer.com'] 做站内搜索,替代原小红书工具。
  - 现有专项工具不能直接回答,或者你需要补充更通用的外部公开信息。
  - check_availability 或 get_poi_info 返回无效信息(如:缺少营业时间、票价缺失、POI 不存在、开放状态未知、数据明显过时)时,用 web_search 搜索补充。
  - 查询航班价格带、火车票价(search_trains 不含票价)时,用它补齐并提示用户以官方平台为准。
Important:
  - include_domains / exclude_domains 各最多 5 个域名。
  - max_results 会自动限制在 1 到 10。
  - 每条结果带 source_id;写入证据(visit_info.evidence)时把它原样复制到 source_ref,不能自己编造。
        返回 Tavily 的简答和结果列表,包含标题、链接、摘要、分数和 source_id。对于推荐型 query,它经常能直接给出可用的候选结论。""",
        phases=[1, 2, 3, 4],
        parameters=_PARAMETERS,
        human_label="上网查资料",
    )
    async def web_search(
        query: str,
        search_depth: str = "basic",
        max_results: int = 5,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> dict:
        if not tavily_key:
            raise ToolError(
                "Tavily API key not configured",
                error_code="MISSING_API_KEY",
                suggestion="Set TAVILY_API_KEY in .env or config.yaml.",
            )

        max_results = max(1, min(10, max_results))

        payload = {
            "api_key": tavily_key,
            "query": query,
            "search_depth": search_depth,
            "max_results": max_results,
            "include_answer": True,
        }
        included = _normalize_domains(include_domains)
        excluded = _normalize_domains(exclude_domains)
        if included:
            payload["include_domains"] = included
        if excluded:
            payload["exclude_domains"] = excluded

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(_TAVILY_SEARCH_URL, json=payload)
            if resp.status_code != 200:
                raise ToolError(
                    f"Tavily API error: {resp.status_code}",
                    error_code="API_ERROR",
                    suggestion="Check TAVILY_API_KEY or try again later.",
                )
            data = resp.json()

        results = []
        for item in data.get("results", []):
            entry = {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
                "score": item.get("score"),
            }
            # 铸造 source_id：证据链 source_ref 的唯一合法来源。
            if (
                source_registry is not None
                and session_id
                and entry["url"].startswith(("http://", "https://"))
            ):
                entry["source_id"] = source_registry.register(
                    session_id,
                    url=entry["url"],
                    title=entry["title"],
                    tool_name="web_search",
                )
            results.append(entry)

        if not results and included:
            raise ToolError(
                f"No results within domains {included} for: {query}",
                error_code="NO_RESULTS",
                suggestion=(
                    "站内搜索无结果。可放宽或去掉 include_domains 重搜,"
                    "或改写更具体的关键词。"
                ),
            )

        return {
            "query": query,
            "answer": data.get("answer", ""),
            "results": results,
            "include_domains": included or None,
            "source": "tavily",
        }

    return web_search
