#!/usr/bin/env python3
"""OpenWeather API 真实调用冒烟测试。

用法:
    cd backend && python scripts/smoke_openweather.py

需要 .env 中配置 OPENWEATHER_API_KEY。
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
results: list[tuple[str, str, str]] = []


def report(name: str, status: str, detail: str = ""):
    results.append((name, status, detail))
    print(f"  [{status}] {name}" + (f"  -- {detail}" if detail else ""))


async def test_check_weather(api_keys):
    """查询天气: 明天的东京天气"""
    from tools.check_weather import make_check_weather_tool

    fn = make_check_weather_tool(api_keys)
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    result = await fn(city="Tokyo", date=tomorrow)
    forecast = result.get("forecast", {})
    if forecast.get("temp") is not None:
        report(
            "check_weather (Tokyo)",
            PASS,
            f"日期={tomorrow}, 温度={forecast['temp']}C, "
            f"天气={forecast.get('description', '?')}",
        )
    else:
        report(
            "check_weather (Tokyo)",
            FAIL,
            f"返回: {json.dumps(result, ensure_ascii=False)[:200]}",
        )


async def test_check_weather_chinese_city(api_keys):
    """查询天气: 中文城市名 '北京'"""
    from tools.check_weather import make_check_weather_tool

    fn = make_check_weather_tool(api_keys)
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    result = await fn(city="北京", date=tomorrow)
    forecast = result.get("forecast", {})
    if forecast.get("temp") is not None:
        report(
            "check_weather (北京)",
            PASS,
            f"温度={forecast['temp']}C, 天气={forecast.get('description', '?')}",
        )
    else:
        report(
            "check_weather (北京)",
            FAIL,
            f"返回: {json.dumps(result, ensure_ascii=False)[:200]}",
        )


async def main():
    print("=" * 60)
    print("OpenWeather API 冒烟测试")
    print("=" * 60)

    cfg = load_config()
    api_keys = cfg.api_keys

    if not api_keys.openweather:
        print(f"  [{SKIP}] OPENWEATHER_API_KEY 未配置，跳过全部测试")
        sys.exit(1)

    print(f"  API Key: {api_keys.openweather[:8]}...{api_keys.openweather[-4:]}")
    print()

    for test_fn in [test_check_weather, test_check_weather_chinese_city]:
        try:
            await test_fn(api_keys)
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
