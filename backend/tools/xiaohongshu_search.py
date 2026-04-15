# backend/tools/xiaohongshu_search.py
from __future__ import annotations

from typing import Any

from config import XhsConfig
from tools.base import ToolError, tool
from tools.xiaohongshu_cli import XiaohongshuCliClient, extract_xsec_token

_OPERATIONS = [
    "search_notes",
    "read_note",
    "get_comments",
]

_PARAMETERS = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": _OPERATIONS,
            "description": "要执行的小红书操作。只支持 search_notes、read_note、get_comments 三种。",
        },
        "keyword": {
            "type": "string",
            "description": (
                "仅在 search_notes 时使用。搜索关键词应尽量具体，"
                "可用于目的地、景点、餐厅、酒店、交通、购物、玩法、季节、排队、避坑等主题。"
                "也适合直接搜索推荐型需求，如 '东南亚 海岛 亲子 推荐'、'日本 文化体验 旅行地 推荐'、"
                "'求推荐旅行目的地'、'五一 去哪玩 求推荐'。"
                "为降低风控风险，优先使用精确关键词而不是宽泛高频搜索。"
                "推荐优先使用“目的地/主题 + 约束 + 推荐”这类搜索词。"
            ),
        },
        "note_ref": {
            "type": "string",
            "description": "read_note 和 get_comments 时必填。可传笔记 URL 或 note_id。",
        },
        "xsec_token": {
            "type": "string",
            "description": "可选显式 xsec_token。若 note_ref 是完整 URL，当前实现会优先尝试从 URL 自动提取。",
        },
        "sort": {
            "type": "string",
            "enum": ["general", "popular", "latest"],
            "description": "search_notes 的排序方式。做旅行地推荐和候选发现时，优先 general 或 popular；latest 更适合追近期热度或当季内容。",
        },
        "note_type": {
            "type": "string",
            "enum": ["all", "video", "image"],
            "description": "search_notes 的笔记类型：all、video、image。默认 all，除非你明确需要只看视频或图文。",
        },
        "page": {
            "type": "integer",
            "description": "search_notes 的页码。小于 1 时会按 1 处理。旅行推荐场景优先先看第 1 页，只有结果明显不够时再谨慎翻页，以降低风控风险。",
        },
        "max_results": {
            "type": "integer",
            "description": "仅在 search_notes 时使用。期望返回的笔记数量。当前实现会在工具层自动限制在 1 到 10，并截断返回结果。",
        },
        "cursor": {
            "type": "string",
            "description": "get_comments 的评论分页 cursor。",
        },
        "fetch_all": {
            "type": "boolean",
            "description": "get_comments 是否拉取全部评论。应谨慎使用，避免高频大批量评论抓取。",
        },
    },
    "required": ["operation"],
}


def make_xiaohongshu_search_tool(
    xhs_config: XhsConfig | None = None,
    xhs_client: XiaohongshuCliClient | Any | None = None,
):
    config = xhs_config or XhsConfig()
    client = xhs_client or XiaohongshuCliClient(
        cli_bin=config.cli_bin,
        timeout=config.cli_timeout,
    )

    @tool(
        name="xiaohongshu_search",
        description="""小红书内容搜索工具。可搜索笔记、读取笔记正文、获取评论，覆盖旅行推荐、灵感发现、目的地/景点/餐厅/住宿的真实体验、避坑、氛围、玩法口碑等内容。
提供用户生成内容视角的经验信号；标题不足以支撑判断时应读取正文，评论观点会明显影响判断时再获取评论。
Supported operations:
  - search_notes: 关键词搜索笔记列表（适合发现和初筛）
  - read_note: 读取笔记正文详情（适合在初筛后对候选笔记进行深入阅读，提炼正文信息）
  - get_comments: 获取评论区内容（适合通过评论区的观点来判断氛围、口碑、避坑等主观维度，或挖掘玩法细节等正文未覆盖的信息）
        返回归一化的小红书数据结构。""",
        phases=[1, 3, 5, 7],
        parameters=_PARAMETERS,
        human_label="翻小红书找灵感",
    )
    async def xiaohongshu_search(
        operation: str,
        keyword: str = "",
        note_ref: str = "",
        xsec_token: str = "",
        sort: str = "general",
        note_type: str = "all",
        page: int = 1,
        max_results: int = 5,
        cursor: str = "",
        fetch_all: bool = False,
    ) -> dict[str, Any]:
        if not config.enabled:
            raise ToolError(
                "Xiaohongshu tool is disabled",
                error_code="SERVICE_UNAVAILABLE",
                suggestion="Enable xhs in config.yaml before using this tool.",
            )

        if operation not in _OPERATIONS:
            raise ToolError(
                f"Unsupported operation: {operation}",
                error_code="INVALID_INPUT",
                suggestion=f"Use one of: {', '.join(_OPERATIONS)}.",
            )

        if operation == "search_notes":
            if not keyword.strip():
                raise ToolError(
                    "`keyword` is required for search_notes",
                    error_code="INVALID_INPUT",
                    suggestion="Pass a Xiaohongshu search keyword.",
                )
            max_results = max(1, min(10, max_results))
            data = await client.search_notes(
                keyword=keyword,
                sort=sort,
                note_type=note_type,
                page=max(1, page),
            )
            items = data.get("items", [])[:max_results]
            return {
                "operation": operation,
                "keyword": keyword,
                "has_more": bool(data.get("has_more", False)),
                "items": [_normalize_search_item(item) for item in items],
                "_metadata": {
                    "page": max(1, page),
                    "max_results": max_results,
                    "source": "xiaohongshu_cli",
                    "items": [_extract_search_item_metadata(item) for item in items],
                },
            }

        if not note_ref.strip():
            raise ToolError(
                "`note_ref` is required for this operation",
                error_code="INVALID_INPUT",
                suggestion="Pass a Xiaohongshu note URL or note_id.",
            )

        if operation == "read_note":
            resolved_token = xsec_token or extract_xsec_token(note_ref)
            data = await client.read_note(note_ref=note_ref, xsec_token=resolved_token)
            return {
                "operation": operation,
                "note": _normalize_note_detail(data),
                "_metadata": {
                    "source": "xiaohongshu_cli",
                    "note": _extract_note_detail_metadata(data),
                },
            }

        resolved_token = xsec_token or extract_xsec_token(note_ref)
        if operation == "get_comments":
            data = await client.get_comments(
                note_ref=note_ref,
                cursor=cursor,
                xsec_token=resolved_token,
                fetch_all=fetch_all,
            )
            return {
                "operation": operation,
                "comments": [
                    _normalize_comment(comment) for comment in data.get("comments", [])
                ],
                "has_more": bool(data.get("has_more", False)),
                "cursor": data.get("cursor", ""),
                "_metadata": {
                    "note_ref": note_ref,
                    "total_fetched": data.get("total_fetched"),
                    "pages_fetched": data.get("pages_fetched"),
                    "source": "xiaohongshu_cli",
                    "comments": [
                        _extract_comment_metadata(comment)
                        for comment in data.get("comments", [])
                    ],
                },
            }

    return xiaohongshu_search


def _normalize_search_item(item: dict[str, Any]) -> dict[str, Any]:
    note_card = item.get("note_card", item) if isinstance(item, dict) else {}
    user = note_card.get("user", {}) if isinstance(note_card, dict) else {}
    interact = note_card.get("interact_info", {}) if isinstance(note_card, dict) else {}
    note_id = (
        item.get("id", note_card.get("note_id", "")) if isinstance(item, dict) else ""
    )
    token = (
        item.get("xsec_token", note_card.get("xsec_token", ""))
        if isinstance(item, dict)
        else ""
    )
    return {
        "note_id": note_id,
        "title": str(note_card.get("title", note_card.get("display_title", ""))),
        "liked_count": str(interact.get("liked_count", "")),
        "note_type": "video" if note_card.get("type") == "video" else "image",
        "url": _build_note_url(note_id, token),
    }


def _normalize_note_detail(data: dict[str, Any]) -> dict[str, Any]:
    note = data
    if data.get("items"):
        first = data.get("items", [{}])[0]
        note = first.get("note_card", first)

    user = note.get("user", {}) if isinstance(note, dict) else {}
    interact = note.get("interact_info", {}) if isinstance(note, dict) else {}
    tags = note.get("tag_list", []) if isinstance(note, dict) else []
    note_id = (
        note.get("note_id", data.get("note_id", "")) if isinstance(note, dict) else ""
    )
    token = note.get("xsec_token", "") if isinstance(note, dict) else ""

    return {
        "note_id": note_id,
        "title": note.get("title", note.get("display_title", "Untitled"))
        if isinstance(note, dict)
        else "Untitled",
        "desc": note.get("desc", "") if isinstance(note, dict) else "",
        "liked_count": str(interact.get("liked_count", "0")),
        "collected_count": str(interact.get("collected_count", "0")),
        "comment_count": str(interact.get("comment_count", "0")),
        "tags": [
            tag.get("name", "")
            for tag in tags
            if isinstance(tag, dict) and tag.get("name")
        ],
        "note_type": "video" if note.get("type") == "video" else "image",
        "url": _build_note_url(note_id, token),
    }


def _normalize_comment(comment: dict[str, Any]) -> dict[str, Any]:
    user = comment.get("user_info", {}) if isinstance(comment, dict) else {}
    return {
        "nickname": user.get("nickname", "Unknown"),
        "content": comment.get("content", "") if isinstance(comment, dict) else "",
        "like_count": str(comment.get("like_count", "0"))
        if isinstance(comment, dict)
        else "0",
    }


def _extract_search_item_metadata(item: dict[str, Any]) -> dict[str, Any]:
    note_card = item.get("note_card", item) if isinstance(item, dict) else {}
    user = note_card.get("user", {}) if isinstance(note_card, dict) else {}
    note_id = (
        item.get("id", note_card.get("note_id", "")) if isinstance(item, dict) else ""
    )
    token = (
        item.get("xsec_token", note_card.get("xsec_token", ""))
        if isinstance(item, dict)
        else ""
    )
    return {
        "note_id": note_id,
        "author": user.get("nickname", ""),
        "xsec_token": token,
    }


def _extract_note_detail_metadata(data: dict[str, Any]) -> dict[str, Any]:
    note = data
    if data.get("items"):
        first = data.get("items", [{}])[0]
        note = first.get("note_card", first)

    user = note.get("user", {}) if isinstance(note, dict) else {}
    interact = note.get("interact_info", {}) if isinstance(note, dict) else {}
    token = note.get("xsec_token", "") if isinstance(note, dict) else ""
    return {
        "author": user.get("nickname", "Unknown"),
        "share_count": str(interact.get("share_count", "0")),
        "image_count": len(note.get("image_list", [])) if isinstance(note, dict) else 0,
        "xsec_token": token,
    }


def _extract_comment_metadata(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "comment_id": comment.get("id", comment.get("comment_id", ""))
        if isinstance(comment, dict)
        else "",
        "sub_comment_count": _coerce_int(comment.get("sub_comment_count", 0))
        if isinstance(comment, dict)
        else 0,
    }


def _build_note_url(note_id: str, xsec_token: str) -> str:
    if not note_id:
        return ""
    if not xsec_token:
        return f"https://www.xiaohongshu.com/explore/{note_id}"
    return (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        f"?xsec_token={xsec_token}&xsec_source=pc_search"
    )


def _coerce_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default
