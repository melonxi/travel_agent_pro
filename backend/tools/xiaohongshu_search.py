# backend/tools/xiaohongshu_search.py
from __future__ import annotations

from typing import Any

from config import XhsConfig
from tools.base import ToolError, tool
from tools.xiaohongshu_cli import XiaohongshuCliClient, extract_xsec_token

_SEARCH_NOTES_PARAMETERS = {
    "type": "object",
    "properties": {
        "keyword": {
            "type": "string",
            "description": "搜索关键词。应尽量具体，可用于目的地、景点、餐厅、酒店、交通、购物、玩法、季节、排队、避坑等主题。",
        },
        "sort": {
            "type": "string",
            "enum": ["general", "popular", "latest"],
            "description": "排序方式。做旅行地推荐和候选发现时，优先 general 或 popular；latest 更适合追近期热度或当季内容。",
        },
        "note_type": {
            "type": "string",
            "enum": ["all", "video", "image"],
            "description": "笔记类型。默认 all，除非你明确需要只看视频或图文。",
        },
        "page": {
            "type": "integer",
            "description": "页码。小于 1 时会按 1 处理。旅行推荐场景优先先看第 1 页，只有结果明显不够时再谨慎翻页。",
        },
        "max_results": {
            "type": "integer",
            "description": "期望返回的笔记数量。工具层自动限制在 1 到 10，并截断返回结果。",
        },
    },
    "required": ["keyword"],
}

_READ_NOTE_PARAMETERS = {
    "type": "object",
    "properties": {
        "note_ref": {
            "type": "string",
            "description": "笔记 URL 或 note_id。优先使用 search 结果里的 url。",
        },
        "xsec_token": {
            "type": "string",
            "description": "可选显式 xsec_token。若 note_ref 是完整 URL，当前实现会优先尝试从 URL 自动提取。",
        },
    },
    "required": ["note_ref"],
}

_GET_COMMENTS_PARAMETERS = {
    "type": "object",
    "properties": {
        "note_ref": {
            "type": "string",
            "description": "笔记 URL 或 note_id。优先使用 search 结果里的 url。",
        },
        "cursor": {
            "type": "string",
            "description": "评论分页 cursor。",
        },
        "xsec_token": {
            "type": "string",
            "description": "可选显式 xsec_token。若 note_ref 是完整 URL，当前实现会优先尝试从 URL 自动提取。",
        },
        "fetch_all": {
            "type": "boolean",
            "description": "是否拉取全部评论。应谨慎使用，避免高频大批量评论抓取。",
        },
    },
    "required": ["note_ref"],
}


def _build_xhs_dependencies(
    xhs_config: XhsConfig | None = None,
    xhs_client: XiaohongshuCliClient | Any | None = None,
) -> tuple[XhsConfig, XiaohongshuCliClient | Any]:
    config = xhs_config or XhsConfig()
    client = xhs_client or XiaohongshuCliClient(
        cli_bin=config.cli_bin,
        timeout=config.cli_timeout,
    )
    return config, client


def _ensure_enabled(config: XhsConfig) -> None:
    if not config.enabled:
        raise ToolError(
            "Xiaohongshu tool is disabled",
            error_code="SERVICE_UNAVAILABLE",
            suggestion="Enable xhs in config.yaml before using this tool.",
        )


def make_xiaohongshu_search_notes_tool(
    xhs_config: XhsConfig | None = None,
    xhs_client: XiaohongshuCliClient | Any | None = None,
):
    config, client = _build_xhs_dependencies(xhs_config, xhs_client)

    @tool(
        name="xiaohongshu_search_notes",
        description="""小红书笔记搜索工具。用于搜索旅行推荐、灵感发现、目的地/景点/餐厅/住宿的真实体验、避坑、氛围、玩法口碑等内容。
返回笔记列表和可继续读取的 url。标题和热度只适合定位笔记，不足以支撑最终判断；需要正文时继续调用 xiaohongshu_read_note。""",
        phases=[1, 3, 5, 7],
        parameters=_SEARCH_NOTES_PARAMETERS,
        human_label="翻小红书找灵感",
    )
    async def xiaohongshu_search_notes(
        keyword: str = "",
        sort: str = "general",
        note_type: str = "all",
        page: int = 1,
        max_results: int = 5,
    ) -> dict[str, Any]:
        _ensure_enabled(config)
        if not keyword.strip():
            raise ToolError(
                "`keyword` is required",
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
            "operation": "search_notes",
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

    return xiaohongshu_search_notes


def make_xiaohongshu_read_note_tool(
    xhs_config: XhsConfig | None = None,
    xhs_client: XiaohongshuCliClient | Any | None = None,
):
    config, client = _build_xhs_dependencies(xhs_config, xhs_client)

    @tool(
        name="xiaohongshu_read_note",
        description="""小红书笔记正文读取工具。用于读取 search 结果中的具体笔记，提取真实体验、路线安排、实用细节、排队时间、适合人群和避坑信息。
标题不足以支撑判断时必须读取正文；主观评价仍不充分时再调用 xiaohongshu_get_comments。""",
        phases=[1, 3, 5, 7],
        parameters=_READ_NOTE_PARAMETERS,
        human_label="读小红书笔记",
    )
    async def xiaohongshu_read_note(
        note_ref: str = "",
        xsec_token: str = "",
    ) -> dict[str, Any]:
        _ensure_enabled(config)
        if not note_ref.strip():
            raise ToolError(
                "`note_ref` is required",
                error_code="INVALID_INPUT",
                suggestion="Pass a Xiaohongshu note URL or note_id.",
            )

        resolved_token = xsec_token or extract_xsec_token(note_ref)
        data = await client.read_note(note_ref=note_ref, xsec_token=resolved_token)
        return {
            "operation": "read_note",
            "note": _normalize_note_detail(data),
            "_metadata": {
                "source": "xiaohongshu_cli",
                "note": _extract_note_detail_metadata(data),
            },
        }

    return xiaohongshu_read_note


def make_xiaohongshu_get_comments_tool(
    xhs_config: XhsConfig | None = None,
    xhs_client: XiaohongshuCliClient | Any | None = None,
):
    config, client = _build_xhs_dependencies(xhs_config, xhs_client)

    @tool(
        name="xiaohongshu_get_comments",
        description="""小红书评论区读取工具。用于获取评论区多元观点，判断值不值得去、排队强度、真实口碑、避坑、替代玩法和正文没有覆盖的细节。
应在已有明确 note_ref 后使用；不要把它当搜索工具。""",
        phases=[1, 3, 5, 7],
        parameters=_GET_COMMENTS_PARAMETERS,
        human_label="看小红书评论",
    )
    async def xiaohongshu_get_comments(
        note_ref: str = "",
        cursor: str = "",
        xsec_token: str = "",
        fetch_all: bool = False,
    ) -> dict[str, Any]:
        _ensure_enabled(config)
        if not note_ref.strip():
            raise ToolError(
                "`note_ref` is required",
                error_code="INVALID_INPUT",
                suggestion="Pass a Xiaohongshu note URL or note_id.",
            )

        resolved_token = xsec_token or extract_xsec_token(note_ref)
        data = await client.get_comments(
            note_ref=note_ref,
            cursor=cursor,
            xsec_token=resolved_token,
            fetch_all=fetch_all,
        )
        return {
            "operation": "get_comments",
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

    return xiaohongshu_get_comments


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
