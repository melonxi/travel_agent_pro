#!/usr/bin/env python3
"""融合工具 (双源并行) 真实调用冒烟测试。

用法:
    cd backend && python scripts/smoke_fusion.py

同时调用 Google Maps + FlyAI，验证双源合并逻辑。
需要 .env 中配置 GOOGLE_MAPS_API_KEY，且 flyai CLI 已安装。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"
WARN = "\033[33mWARN\033[0m"
results: list[tuple[str, str, str]] = []


def report(name: str, status: str, detail: str = ""):
    results.append((name, status, detail))
    print(f"  [{status}] {name}" + (f"  -- {detail}" if detail else ""))


def source_breakdown(items: list[dict]) -> str:
    """统计各 source 数量"""
    counts: dict[str, int] = {}
    for item in items:
        src = item.get("source", "unknown")
        counts[src] = counts.get(src, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


async def test_accommodations_fusion(api_keys, flyai_client):
    """住宿融合: 东京酒店 (Google + FlyAI)"""
    from tools.search_accommodations import make_search_accommodations_tool

    check_in = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    check_out = (datetime.now() + timedelta(days=9)).strftime("%Y-%m-%d")

    fn = make_search_accommodations_tool(api_keys, flyai_client=flyai_client)
    result = await fn(destination="东京", check_in=check_in, check_out=check_out)
    accs = result.get("accommodations", [])
    if accs:
        breakdown = source_breakdown(accs)
        report(
            "search_accommodations 融合",
            PASS,
            f"返回 {len(accs)} 家, 来源分布: {breakdown}",
        )
    else:
        report("search_accommodations 融合", FAIL, "没有返回任何住宿")


async def test_poi_fusion(api_keys, flyai_client):
    """景点融合: 京都金阁寺 (Google + FlyAI)"""
    from tools.get_poi_info import make_get_poi_info_tool

    fn = make_get_poi_info_tool(api_keys, flyai_client=flyai_client)
    result = await fn(query="金阁寺", location="京都")
    pois = result.get("pois", [])
    if pois:
        breakdown = source_breakdown(pois)
        report(
            "get_poi_info 融合",
            PASS,
            f"返回 {len(pois)} 个, 来源分布: {breakdown}",
        )
    else:
        report("get_poi_info 融合", FAIL, "没有返回任何 POI")


async def test_flights_fusion(api_keys, flyai_client):
    """航班融合: 上海→东京 (Amadeus + FlyAI)

    注: Amadeus key 不可用时，FlyAI 侧仍应返回结果。
    """
    from tools.search_flights import make_search_flights_tool

    dep_date = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")

    fn = make_search_flights_tool(api_keys, flyai_client=flyai_client)
    result = await fn(origin="PVG", destination="NRT", date=dep_date)
    flights = result.get("flights", [])
    if flights:
        breakdown = source_breakdown(flights)
        report(
            "search_flights 融合",
            PASS,
            f"返回 {len(flights)} 条, 来源分布: {breakdown}",
        )
    else:
        report("search_flights 融合", FAIL, "没有返回任何航班")


async def test_quick_travel_search(flyai_client):
    """FlyAI 独占: 快速搜索 '日本签证'"""
    from tools.quick_travel_search import make_quick_travel_search_tool

    fn = make_quick_travel_search_tool(flyai_client)
    result = await fn(query="日本签证")
    items = result.get("results", [])
    if items:
        sample = items[0]
        report(
            "quick_travel_search",
            PASS,
            f"返回 {len(items)} 条, 首条: {sample.get('title', '?')[:40]}",
        )
    else:
        report("quick_travel_search", FAIL, "没有返回任何结果")


async def test_search_travel_services(flyai_client):
    """FlyAI 独占: 旅行服务搜索 — 日本 WiFi/电话卡"""
    from tools.search_travel_services import make_search_travel_services_tool

    fn = make_search_travel_services_tool(flyai_client)
    result = await fn(destination="日本", service_type="sim_card")
    items = result.get("services", [])
    if items:
        sample = items[0]
        report(
            "search_travel_services (sim_card)",
            PASS,
            f"返回 {len(items)} 条, 首条: {sample.get('title', '?')[:40]}",
        )
    else:
        report("search_travel_services (sim_card)", FAIL, "没有返回任何结果")


async def main():
    print("=" * 60)
    print("融合工具冒烟测试 (Google + FlyAI 双源)")
    print("=" * 60)

    cfg = load_config()
    api_keys = cfg.api_keys

    from tools.flyai_client import FlyAIClient

    flyai_client = FlyAIClient(timeout=30)

    google_ok = bool(api_keys.google_maps)
    flyai_ok = flyai_client.available

    print(f"  Google Maps API Key: {'OK' if google_ok else 'MISSING'}")
    print(f"  FlyAI CLI: {'OK' if flyai_ok else 'NOT INSTALLED'}")

    if not google_ok and not flyai_ok:
        print(f"\n  [{SKIP}] 两个数据源都不可用，跳过全部测试")
        sys.exit(1)

    if not google_ok:
        print(f"  [{WARN}] Google 不可用，融合测试仅含 FlyAI 侧结果")
    if not flyai_ok:
        print(f"  [{WARN}] FlyAI 不可用，融合测试仅含 Google 侧结果")

    print()

    # 融合工具（需要两个源都尽量可用）
    for test_fn in [
        test_accommodations_fusion,
        test_poi_fusion,
        test_flights_fusion,
    ]:
        try:
            await test_fn(api_keys, flyai_client)
        except Exception as e:
            report(test_fn.__name__, FAIL, f"异常: {type(e).__name__}: {e}")

    # FlyAI 独占工具
    if flyai_ok:
        for test_fn in [test_quick_travel_search, test_search_travel_services]:
            try:
                await test_fn(flyai_client)
            except Exception as e:
                report(test_fn.__name__, FAIL, f"异常: {type(e).__name__}: {e}")
    else:
        report("quick_travel_search", SKIP, "flyai 不可用")
        report("search_travel_services", SKIP, "flyai 不可用")

    # 汇总
    print()
    print("-" * 60)
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    skipped = sum(1 for _, s, _ in results if s == SKIP)
    print(f"结果: {passed} 通过, {failed} 失败, {skipped} 跳过, 共 {len(results)} 项")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
