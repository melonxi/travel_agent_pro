#!/usr/bin/env python3
"""FlyAI CLI 真实调用冒烟测试。

用法:
    cd backend && python scripts/smoke_flyai.py

需要 flyai CLI 已安装 (npm i -g @fly-ai/flyai-cli)，无需 API key。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"
results: list[tuple[str, str, str]] = []


def report(name: str, status: str, detail: str = ""):
    results.append((name, status, detail))
    print(f"  [{status}] {name}" + (f"  -- {detail}" if detail else ""))


def truncate(obj, max_len=150) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s[:max_len] + "..." if len(s) > max_len else s


async def test_search_flight(client):
    """搜索航班: 上海 → 北京, 7天后"""
    dep_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    items = await client.search_flight(
        origin="上海", destination="北京", dep_date=dep_date
    )
    if items:
        sample = items[0]
        report(
            "search_flight (上海→北京)",
            PASS,
            f"返回 {len(items)} 条, 首条: {truncate(sample)}",
        )
    else:
        report("search_flight (上海→北京)", FAIL, "返回空列表")


async def test_search_hotels(client):
    """搜索酒店: 杭州, 7天后入住"""
    check_in = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    check_out = (datetime.now() + timedelta(days=9)).strftime("%Y-%m-%d")
    items = await client.search_hotels(
        dest_name="杭州", check_in_date=check_in, check_out_date=check_out
    )
    if items:
        sample = items[0]
        report(
            "search_hotels (杭州)",
            PASS,
            f"返回 {len(items)} 条, 首条: {truncate(sample)}",
        )
    else:
        report("search_hotels (杭州)", FAIL, "返回空列表")


async def test_search_poi(client):
    """搜索景点: 北京"""
    items = await client.search_poi(city_name="北京")
    if items:
        sample = items[0]
        report(
            "search_poi (北京)",
            PASS,
            f"返回 {len(items)} 条, 首条: {truncate(sample)}",
        )
    else:
        report("search_poi (北京)", FAIL, "返回空列表")


async def test_fast_search(client):
    """极速搜索: '杭州三日游'"""
    items = await client.fast_search(query="杭州三日游")
    if items:
        sample = items[0]
        report(
            "fast_search (杭州三日游)",
            PASS,
            f"返回 {len(items)} 条, 首条: {truncate(sample)}",
        )
    else:
        report("fast_search (杭州三日游)", FAIL, "返回空列表")


async def test_search_flight_international(client):
    """搜索国际航班: 上海 → 东京, 14天后"""
    dep_date = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
    items = await client.search_flight(
        origin="上海", destination="东京", dep_date=dep_date
    )
    if items:
        report(
            "search_flight (上海→东京)",
            PASS,
            f"返回 {len(items)} 条, 首条: {truncate(items[0])}",
        )
    else:
        report("search_flight (上海→东京)", FAIL, "返回空列表")


async def main():
    print("=" * 60)
    print("FlyAI CLI 冒烟测试")
    print("=" * 60)

    from tools.flyai_client import FlyAIClient

    client = FlyAIClient(timeout=30)
    if not client.available:
        print(f"  [{SKIP}] flyai CLI 未安装, 请运行: npm i -g @fly-ai/flyai-cli")
        sys.exit(1)

    print("  flyai CLI: 已安装")
    print()

    for test_fn in [
        test_search_flight,
        test_search_hotels,
        test_search_poi,
        test_fast_search,
        test_search_flight_international,
    ]:
        try:
            await test_fn(client)
        except Exception as e:
            report(test_fn.__name__, FAIL, f"异常: {type(e).__name__}: {e}")

    print()
    print("-" * 60)
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    print(f"结果: {passed} 通过, {failed} 失败, 共 {len(results)} 项")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
