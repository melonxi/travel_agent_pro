#!/usr/bin/env python3
"""web_search 真实联网质量测试脚本。

用法:
    cd backend && python scripts/smoke_web_search.py
    cd backend && python scripts/smoke_web_search.py --query "东京迪士尼 官网 门票价格 只看官方"
    cd backend && python scripts/smoke_web_search.py --query "2026 日本签证 中国护照 短期旅游 最新政策" --depth advanced --max-results 5
    cd backend && python scripts/smoke_web_search.py --preset all

用途:
    - 真实调用 Tavily，验证 web_search 在当前环境下的可用性。
    - 粗看结果质量，而不只是验证接口通不通。
    - 帮助判断某类 query 更适合 search，还是必须继续做 fetch/人工复核。

说明:
    - 这是 smoke/quality 脚本，不是 pytest 单测。
    - 质量判断使用启发式规则，只用于快速筛查，不代表最终事实正确性。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_config
from tools.web_search import make_web_search_tool

PASS = "\033[32mPASS\033[0m"
WARN = "\033[33mWARN\033[0m"
FAIL = "\033[31mFAIL\033[0m"

PRESETS: dict[str, list[str]] = {
    "all": [
        "2026 日本签证 中国护照 短期旅游 最新政策",
        "5月 亲子 海岛 东南亚 不贵 推荐",
        "东京迪士尼 官网 门票价格 只看官方",
        "清迈泼水节 过去30天 酒店价格走势",
    ],
    "travel": [
        "5月 亲子 海岛 东南亚 不贵 推荐",
        "京都和大阪 哪个更适合第一次去日本",
    ],
    "fact": [
        "2026 日本签证 中国护照 短期旅游 最新政策",
        "东京迪士尼 官网 门票价格 只看官方",
    ],
}

AGGREGATOR_HINTS = (
    "trip.com",
    "ctrip",
    "skyscanner",
    "kayak",
    "facebook.com",
    "sohu.com",
    "qq.com",
    "163.com",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="web_search 真实联网质量测试")
    parser.add_argument(
        "--query",
        action="append",
        help="要测试的 query。可传多次；不传时使用默认样例。",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS.keys()),
        default=None,
        help="使用内置样例集。",
    )
    parser.add_argument(
        "--depth",
        default="advanced",
        help="search_depth，默认 advanced。",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=5,
        help="max_results，默认 5。",
    )
    parser.add_argument(
        "--show-content",
        action="store_true",
        help="显示每条结果的摘要内容。",
    )
    parser.add_argument(
        "--show-raw",
        action="store_true",
        help="显示 Tavily 原始返回 JSON。",
    )
    return parser.parse_args()


def status_line(status: str, title: str, detail: str = "") -> None:
    print(f"[{status}] {title}" + (f"  -- {detail}" if detail else ""))


def normalize_domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        return netloc[4:]
    return netloc


def is_official_like(domain: str) -> bool:
    if domain.endswith(".gov") or domain.endswith(".gov.cn"):
        return True
    if "embassy" in domain or "consulate" in domain:
        return True
    if domain.endswith(".go.jp") or domain.endswith(".go.th"):
        return True
    return False


def result_looks_official(item: dict[str, Any]) -> bool:
    domain = normalize_domain(item.get("url", ""))
    title = (item.get("title") or "").lower()
    if is_official_like(domain):
        return True
    return "官方" in (item.get("title") or "") or "official" in title


def needs_official_bias(query: str) -> bool:
    flags = ("官网", "官方", "official", "政策", "签证", "票价", "门票", "开放时间")
    query_lower = query.lower()
    return any(flag in query or flag in query_lower for flag in flags)


def needs_time_filter(query: str) -> bool:
    flags = ("最近", "过去", "近30天", "过去30天", "趋势", "走势", "recent", "trend")
    query_lower = query.lower()
    return any(flag in query or flag in query_lower for flag in flags)


def summarize_quality(query: str, results: list[dict[str, Any]]) -> tuple[str, list[str]]:
    warnings: list[str] = []
    domains = [normalize_domain(item.get("url", "")) for item in results if item.get("url")]
    counts = Counter(domains)
    unique_domains = len(counts)
    official_hits = sum(1 for item in results if result_looks_official(item))
    aggregator_hits = sum(
        1 for domain in domains if any(hint in domain for hint in AGGREGATOR_HINTS)
    )

    if not results:
        return FAIL, ["没有返回任何结果"]

    if unique_domains <= 2 and len(results) >= 4:
        warnings.append("结果来源集中度较高，可能缺少交叉验证")

    if aggregator_hits >= max(2, len(results) // 2):
        warnings.append("结果中聚合站/媒体站占比较高，适合继续 fetch 或人工复核")

    if needs_official_bias(query) and official_hits == 0:
        warnings.append("这是高风险事实型 query，但结果里没有明显官方/政府域名")

    if needs_time_filter(query):
        warnings.append("query 带时间窗口/趋势语义，但当前 tool 没有硬时间过滤能力")

    return (WARN if warnings else PASS), warnings


def format_answer(answer: str, limit: int = 220) -> str:
    text = " ".join((answer or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def print_results(results: list[dict[str, Any]], *, show_content: bool) -> None:
    for idx, item in enumerate(results, 1):
        title = " ".join((item.get("title") or "").split())
        url = item.get("url", "")
        domain = normalize_domain(url)
        score = item.get("score")
        print(f"  {idx}. [{domain}] score={score} {title[:90]}")
        print(f"     {url}")
        if show_content:
            content = " ".join((item.get("content") or "").split())
            if content:
                print(f"     {content[:180]}")


async def run_query(
    tool_fn,
    query: str,
    *,
    depth: str,
    max_results: int,
    show_content: bool,
    show_raw: bool,
) -> bool:
    print("=" * 72)
    print(f"Query: {query}")
    print(f"Params: search_depth={depth}, max_results={max_results}")

    try:
        result = await tool_fn(
            query=query,
            search_depth=depth,
            max_results=max_results,
        )
    except Exception as exc:
        status_line(FAIL, "web_search 调用失败", f"{type(exc).__name__}: {exc}")
        return False

    answer = result.get("answer", "")
    results = result.get("results", [])
    domains = [normalize_domain(item.get("url", "")) for item in results if item.get("url")]
    domain_counts = Counter(domains)

    status, warnings = summarize_quality(query, results)
    status_line(status, "质量初判", "; ".join(warnings))
    print(f"Answer: {format_answer(answer)}")
    print(f"Result count: {len(results)}")
    print(
        "Domains: "
        + (
            ", ".join(f"{domain} x{count}" for domain, count in domain_counts.items())
            if domain_counts
            else "(none)"
        )
    )
    print_results(results, show_content=show_content)
    if show_raw:
        print("Raw JSON:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    print()
    return status != FAIL


async def main() -> None:
    args = parse_args()
    cfg = load_config()

    if not cfg.api_keys.tavily:
        status_line(FAIL, "TAVILY_API_KEY 未配置")
        sys.exit(1)

    tool_fn = make_web_search_tool(cfg.api_keys)

    queries: list[str] = []
    if args.query:
        queries.extend(args.query)
    if args.preset:
        queries.extend(PRESETS[args.preset])
    if not queries:
        queries.extend(PRESETS["all"])

    print("=" * 72)
    print("web_search 真实联网质量测试")
    print("=" * 72)
    print(f"API Key: {cfg.api_keys.tavily[:8]}...{cfg.api_keys.tavily[-4:]}")
    print(f"Queries: {len(queries)}")
    print()

    passed = 0
    for query in queries:
        ok = await run_query(
            tool_fn,
            query,
            depth=args.depth,
            max_results=args.max_results,
            show_content=args.show_content,
            show_raw=args.show_raw,
        )
        if ok:
            passed += 1

    total = len(queries)
    print("-" * 72)
    print(f"完成: {passed}/{total} 个 query 成功返回结果")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
