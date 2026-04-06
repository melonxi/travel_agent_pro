from __future__ import annotations

import re

import httpx

from config import ApiKeysConfig
from tools.base import ToolError, tool

_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "用户的目的地意图、主题或地域线索。支持抽象需求和具体地名，"
                "如 '海边放松'、'日本文化'、'东南亚避暑'、'东京'。"
                "适合传目的地层级的需求，不适合传景点名、酒店名。"
                "像免签、预算、亲子友好、是否折腾这类条件最多只会作为松散线索，不会被严格校验。"
            ),
        },
        "preferences": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "用户明确表达的偏好标签，会参与抽象意图扩展。"
                "建议传 2-5 个，如 ['海滩', '美食', '文化']。"
                "这些标签只做简单关键词触发和扩展辅助，不是严格过滤条件。"
            ),
        },
    },
    "required": ["query"],
}

_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
_TRAVEL_NOISE = (
    "旅游",
    "旅行",
    "度假",
    "目的地",
    "推荐",
    "想去",
    "想找",
    "适合",
    "哪里",
    "去哪",
)
_DESTINATION_TYPES = {
    "locality",
    "country",
    "administrative_area_level_1",
    "administrative_area_level_2",
    "postal_town",
    "colloquial_area",
    "natural_feature",
    "sublocality",
}
_REGION_HINT_SEEDS: list[tuple[list[str], list[str]]] = [
    (["日本", "japan"], ["东京", "京都", "大阪", "奈良", "北海道"]),
    (["东南亚", "southeast asia"], ["新加坡", "曼谷", "巴厘岛", "吉隆坡", "普吉岛"]),
    (["欧洲", "europe"], ["巴黎", "罗马", "巴塞罗那", "阿姆斯特丹", "布拉格"]),
    (["中国", "国内", "in china"], ["杭州", "成都", "厦门", "青岛", "西安"]),
]
_THEME_SEEDS: list[tuple[str, list[str], list[str]]] = [
    (
        "beach_relax",
        ["海边", "海岛", "看海", "海景", "沙滩", "放松", "relax", "beach", "island"],
        ["三亚", "冲绳", "巴厘岛", "普吉岛", "厦门"],
    ),
    (
        "cool_escape",
        ["避暑", "凉快", "清凉", "纳凉", "summer", "cool"],
        ["昆明", "贵阳", "青岛", "北海道", "伊宁"],
    ),
    (
        "culture_history",
        ["文化", "历史", "古城", "寺庙", "博物馆", "culture", "history"],
        ["京都", "西安", "北京", "奈良", "伊斯坦布尔"],
    ),
    (
        "food",
        ["美食", "好吃", "夜市", "海鲜", "food", "cuisine"],
        ["成都", "大阪", "广州", "曼谷", "台南"],
    ),
    (
        "family",
        ["亲子", "带娃", "儿童", "乐园", "family", "kids"],
        ["新加坡", "大阪", "东京", "香港", "上海"],
    ),
    (
        "weekend_getaway",
        ["周末", "散心", "短途", "轻松", "weekend", "getaway"],
        ["杭州", "苏州", "厦门", "青岛", "长沙"],
    ),
    (
        "nature",
        ["自然", "风景", "徒步", "山水", "nature", "hiking"],
        ["张家界", "九寨沟", "喀纳斯", "清迈", "皇后镇"],
    ),
]


def _normalize_query(query: str) -> str:
    normalized = query.strip()
    for token in _TRAVEL_NOISE:
        normalized = normalized.replace(token, " ")
    normalized = re.sub(r"\s+", " ", normalized).strip(" ，,。.;；")
    return normalized or query.strip()


def _extract_seed_destinations(
    query: str,
    preferences: list[str] | None,
) -> tuple[list[str], list[str]]:
    combined = " ".join([query, *(preferences or [])]).lower()
    matched_themes: list[str] = []
    seeds: list[str] = []

    for keywords, region_seeds in _REGION_HINT_SEEDS:
        if any(keyword in combined for keyword in keywords):
            for seed in region_seeds:
                if seed not in seeds:
                    seeds.append(seed)

    for theme_name, keywords, theme_seeds in _THEME_SEEDS:
        if any(keyword in combined for keyword in keywords):
            matched_themes.append(theme_name)
            for seed in theme_seeds:
                if seed not in seeds:
                    seeds.append(seed)

    return seeds[:8], matched_themes


def _result_name(item: dict) -> str:
    components = item.get("address_components", [])
    if components:
        return components[0].get("long_name", "")
    return item.get("formatted_address", "")


def _is_destination_result(item: dict) -> bool:
    item_types = set(item.get("types", []))
    return bool(item_types & _DESTINATION_TYPES)


async def _geocode_candidates(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    *,
    limit: int,
) -> list[dict]:
    if not query.strip():
        return []

    resp = await client.get(
        _GEOCODE_URL,
        params={"address": query, "key": api_key},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    results: list[dict] = []
    for item in data.get("results", []):
        if not _is_destination_result(item):
            continue
        results.append(
            {
                "name": _result_name(item),
                "formatted_address": item.get("formatted_address", ""),
                "rating": None,
                "location": item.get("geometry", {}).get("location", {}),
                "place_types": item.get("types", []),
            }
        )
        if len(results) >= limit:
            break
    return results


def make_search_destinations_tool(api_keys: ApiKeysConfig):
    @tool(
        name="search_destinations",
        description="""根据抽象旅行意图、主题偏好或地域线索，生成一组可进一步比较的地理目的地候选。
Use when:
  - 你已经有了初步候选，需要补地理落点、地址或坐标。
  - 用户表达较抽象、约束较少，你需要先把“想看海 / 想轻松 / 想体验日本文化”这类需求转成 2-5 个可比较地理对象。
  - 你想把其他来源得到的灵感候选，补成更明确的城市/地区对象。
How it works:
  - 先尝试直接地理检索。
  - 如果输入较抽象，再根据内置主题和区域种子扩展为城市/地区候选。
Don't use when:
  - 你要直接做高质量旅行地推荐，尤其当用户带有免签、预算、亲子、小众、远近、路途折腾等复杂约束时。
  - 用户已经明确确认目的地。
  - 你要查的是景点、酒店、机票、签证、开放政策或攻略细节。
Important:
  - 它返回的是“地理候选”，不是完整推荐结论；更适合做候选补全和地理落点，不适合单独作为最终推荐依据。
  - 返回结果可能是混合粒度的地理对象，例如城市、国家、行政区、俗称区域、子区域或自然地物，使用前需要你做粒度和合理性复核。
  - preferences 只用于简单关键词触发和扩展辅助，不是硬过滤条件。
  - 对免签、预算、亲子友好、远近、是否折腾等约束，当前实现不会做严格筛选；这些约束需要你再用其他工具或分析补验证。
返回目的地名称、地址、地理坐标、命中的主题标签和候选种子。""",
        phases=[],
        parameters=_PARAMETERS,
    )
    async def search_destinations(
        query: str, preferences: list[str] | None = None
    ) -> dict:
        if not api_keys.google_maps:
            raise ToolError(
                "Google Maps API key not configured",
                error_code="NO_API_KEY",
                suggestion="Set GOOGLE_MAPS_API_KEY",
            )

        normalized_query = _normalize_query(query)
        seed_destinations, matched_themes = _extract_seed_destinations(
            query, preferences
        )

        results: list[dict] = []
        seen_names: set[str] = set()

        async with httpx.AsyncClient() as client:
            for item in await _geocode_candidates(
                client, api_keys.google_maps, normalized_query, limit=3
            ):
                if item["name"] and item["name"] not in seen_names:
                    seen_names.add(item["name"])
                    results.append(item)

            if len(results) < 3:
                for seed in seed_destinations:
                    candidates = await _geocode_candidates(
                        client, api_keys.google_maps, seed, limit=1
                    )
                    if not candidates:
                        continue
                    item = candidates[0]
                    if item["name"] and item["name"] not in seen_names:
                        seen_names.add(item["name"])
                        results.append(item)
                    if len(results) >= 5:
                        break

        if not results:
            raise ToolError(
                "No destination candidates found for the current intent",
                error_code="NO_RESULTS",
                suggestion=(
                    "Broaden the theme, add concrete preferences, or use web_search/"
                    "xiaohongshu_search for fresh inspiration and trend signals."
                ),
            )

        return {
            "destinations": results[:5],
            "source": "google_geocoding",
            "query": query,
            "normalized_query": normalized_query,
            "matched_themes": matched_themes,
            "candidate_seeds": seed_destinations[:5],
        }

    return search_destinations
