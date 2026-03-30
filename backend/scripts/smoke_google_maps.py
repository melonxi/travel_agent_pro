#!/usr/bin/env python3
"""Google Maps API 真实调用冒烟测试。

用法:
    cd backend && python scripts/smoke_google_maps.py

需要 .env 中配置 GOOGLE_MAPS_API_KEY。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# 确保 backend/ 在 sys.path 上，这样 import config 能正常工作
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config

# ── 工具状态 ────────────────────────────
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"
results: list[tuple[str, str, str]] = []  # (name, status, detail)


def report(name: str, status: str, detail: str = ""):
    results.append((name, status, detail))
    print(f"  [{status}] {name}" + (f"  -- {detail}" if detail else ""))


# ── 测试用例 ────────────────────────────


async def test_search_destinations(api_keys):
    """搜索目的地: 用 '东京 旅游' 关键词"""
    from tools.search_destinations import make_search_destinations_tool

    fn = make_search_destinations_tool(api_keys)
    result = await fn(query="东京 旅游")
    dests = result.get("destinations", [])
    if dests:
        names = [d["name"] for d in dests[:3]]
        report("search_destinations", PASS, f"返回 {len(dests)} 个结果: {names}")
    else:
        report("search_destinations", FAIL, "没有返回任何目的地")


async def test_search_accommodations_google(api_keys):
    """搜索住宿 (仅 Google 源): 东京酒店"""
    from tools.search_accommodations import make_search_accommodations_tool

    # 不传 flyai_client, 只走 Google
    fn = make_search_accommodations_tool(api_keys, flyai_client=None)
    result = await fn(destination="东京", check_in="2026-06-01", check_out="2026-06-03")
    accs = result.get("accommodations", [])
    if accs:
        sample = accs[0]
        report(
            "search_accommodations (Google)",
            PASS,
            f"返回 {len(accs)} 家, 首个: {sample.get('name', '?')} "
            f"(source={sample.get('source', '?')})",
        )
    else:
        report("search_accommodations (Google)", FAIL, "没有返回任何住宿")


async def test_get_poi_info_google(api_keys):
    """搜索景点 (仅 Google 源): 金阁寺"""
    from tools.get_poi_info import make_get_poi_info_tool

    fn = make_get_poi_info_tool(api_keys, flyai_client=None)
    result = await fn(query="金阁寺", location="京都")
    pois = result.get("pois", [])
    if pois:
        sample = pois[0]
        report(
            "get_poi_info (Google)",
            PASS,
            f"返回 {len(pois)} 个, 首个: {sample.get('name', '?')} "
            f"rating={sample.get('rating', '?')}",
        )
    else:
        report("get_poi_info (Google)", FAIL, "没有返回任何 POI")


async def test_calculate_route(api_keys):
    """计算路线: 东京站 → 浅草寺 (公共交通)"""
    from tools.calculate_route import make_calculate_route_tool

    fn = make_calculate_route_tool(api_keys)
    result = await fn(
        origin_lat=35.6812,
        origin_lng=139.7671,
        dest_lat=35.7148,
        dest_lng=139.7967,
        mode="transit",
    )
    if result.get("distance") and result.get("duration"):
        report(
            "calculate_route",
            PASS,
            f"距离={result['distance']}, 耗时={result['duration']}, "
            f"{len(result.get('steps', []))} 步骤",
        )
    else:
        report(
            "calculate_route",
            FAIL,
            f"结果不完整: {json.dumps(result, ensure_ascii=False)[:200]}",
        )


# ── 主入口 ──────────────────────────────


async def main():
    print("=" * 60)
    print("Google Maps API 冒烟测试")
    print("=" * 60)

    cfg = load_config()
    api_keys = cfg.api_keys

    if not api_keys.google_maps:
        print(f"  [{SKIP}] GOOGLE_MAPS_API_KEY 未配置，跳过全部测试")
        sys.exit(1)

    print(f"  API Key: {api_keys.google_maps[:8]}...{api_keys.google_maps[-4:]}")
    print()

    for test_fn in [
        test_search_destinations,
        test_search_accommodations_google,
        test_get_poi_info_google,
        test_calculate_route,
    ]:
        try:
            await test_fn(api_keys)
        except Exception as e:
            report(test_fn.__name__, FAIL, f"异常: {type(e).__name__}: {e}")

    # 汇总
    print()
    print("-" * 60)
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    print(f"结果: {passed} 通过, {failed} 失败, 共 {len(results)} 项")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
