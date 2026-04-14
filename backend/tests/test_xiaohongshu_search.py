from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.types import ToolCall
from tools.base import ToolError


class _FakeProcess:
    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        delay: float = 0.0,
    ) -> None:
        self._stdout = stdout.encode("utf-8")
        self._stderr = stderr.encode("utf-8")
        self.returncode = returncode
        self._delay = delay
        self.killed = False

    async def communicate(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_xhs_cli_client_runs_status_then_search(monkeypatch):
    from tools.xiaohongshu_cli import XiaohongshuCliClient

    calls: list[tuple[tuple, dict]] = []
    payloads = [
        _FakeProcess(
            stdout=json.dumps(
                {
                    "ok": True,
                    "schema_version": "1",
                    "data": {"authenticated": True, "user": {"guest": False}},
                }
            )
        ),
        _FakeProcess(
            stdout=json.dumps(
                {
                    "ok": True,
                    "schema_version": "1",
                    "data": {
                        "items": [
                            {
                                "id": "note_1",
                                "xsec_token": "token_1",
                                "note_card": {
                                    "title": "上海旅行",
                                    "type": "image",
                                    "user": {"nickname": "Alice"},
                                    "interact_info": {"liked_count": "120"},
                                },
                            }
                        ],
                        "has_more": True,
                    },
                }
            )
        ),
    ]

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return payloads.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    client = XiaohongshuCliClient(cli_bin="xhs", timeout=3)
    data = await client.search_notes(
        keyword="上海 旅行",
        sort="popular",
        note_type="video",
        page=2,
    )

    assert data["has_more"] is True
    assert len(calls) == 2
    assert calls[0][0] == ("xhs", "status", "--json")
    assert calls[1][0] == (
        "xhs",
        "search",
        "上海 旅行",
        "--sort",
        "popular",
        "--type",
        "video",
        "--page",
        "2",
        "--json",
    )
    assert calls[1][1]["env"]["OUTPUT"] == "json"


@pytest.mark.asyncio
async def test_xhs_cli_client_maps_permission_denied(monkeypatch):
    from tools.xiaohongshu_cli import XiaohongshuCliClient

    payloads = [
        _FakeProcess(
            stdout=json.dumps(
                {
                    "ok": True,
                    "schema_version": "1",
                    "data": {"authenticated": True, "user": {"guest": True}},
                }
            )
        ),
        _FakeProcess(
            stdout=json.dumps(
                {
                    "ok": False,
                    "schema_version": "1",
                    "error": {
                        "code": "api_error",
                        "message": (
                            'API error: {"code": -104, "success": false, '
                            '"msg": "您当前登录的账号没有权限访问", "data": {}}'
                        ),
                    },
                }
            ),
            returncode=1,
        ),
    ]

    async def fake_exec(*args, **kwargs):
        return payloads.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    client = XiaohongshuCliClient(timeout=3)
    with pytest.raises(ToolError, match="没有权限") as exc_info:
        await client.search_notes(keyword="上海 旅行")

    assert exc_info.value.error_code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_xhs_cli_client_missing_binary(monkeypatch):
    from tools.xiaohongshu_cli import XiaohongshuCliClient

    async def fake_exec(*args, **kwargs):
        raise FileNotFoundError("xhs not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    client = XiaohongshuCliClient(cli_bin="/missing/xhs", timeout=3)
    with pytest.raises(ToolError, match="not found") as exc_info:
        await client.search_notes(keyword="上海 旅行")

    assert exc_info.value.error_code == "CLI_NOT_FOUND"


@pytest.mark.asyncio
async def test_xhs_cli_client_invalid_json(monkeypatch):
    from tools.xiaohongshu_cli import XiaohongshuCliClient

    payloads = [
        _FakeProcess(
            stdout=json.dumps(
                {
                    "ok": True,
                    "schema_version": "1",
                    "data": {"authenticated": True, "user": {"guest": False}},
                }
            )
        ),
        _FakeProcess(stdout="not-json"),
    ]

    async def fake_exec(*args, **kwargs):
        return payloads.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    client = XiaohongshuCliClient(timeout=3)
    with pytest.raises(ToolError, match="Invalid JSON") as exc_info:
        await client.search_notes(keyword="上海 旅行")

    assert exc_info.value.error_code == "INVALID_CLI_RESPONSE"


@pytest.mark.asyncio
async def test_xhs_cli_client_timeout(monkeypatch):
    from tools.xiaohongshu_cli import XiaohongshuCliClient

    payloads = [
        _FakeProcess(
            stdout=json.dumps(
                {
                    "ok": True,
                    "schema_version": "1",
                    "data": {"authenticated": True, "user": {"guest": False}},
                }
            )
        ),
        _FakeProcess(delay=0.05),
    ]

    async def fake_exec(*args, **kwargs):
        return payloads.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    client = XiaohongshuCliClient(timeout=0.01)
    with pytest.raises(ToolError, match="timed out"):
        await client.read_note("note_1")


@pytest.mark.asyncio
async def test_xiaohongshu_search_tool_search_notes():
    from tools.xiaohongshu_search import make_xiaohongshu_search_tool

    xhs_client = SimpleNamespace(
        search_notes=AsyncMock(
            return_value={
                "items": [
                    {
                        "id": "note_1",
                        "xsec_token": "token_1",
                        "note_card": {
                            "title": "上海旅行攻略",
                            "type": "image",
                            "user": {"nickname": "Alice"},
                            "interact_info": {"liked_count": "12"},
                        },
                    }
                ],
                "has_more": True,
            }
        ),
        read_note=AsyncMock(),
        get_comments=AsyncMock(),
    )

    tool_fn = make_xiaohongshu_search_tool(xhs_client=xhs_client)
    result = await tool_fn(
        operation="search_notes",
        keyword="上海 旅行",
        sort="popular",
        note_type="image",
        page=2,
    )

    assert result["operation"] == "search_notes"
    assert result["items"][0]["note_id"] == "note_1"
    assert result["items"][0]["url"].startswith(
        "https://www.xiaohongshu.com/explore/note_1"
    )
    assert result["_metadata"]["page"] == 2
    assert result["_metadata"]["items"][0]["author"] == "Alice"
    assert result["_metadata"]["items"][0]["xsec_token"] == "token_1"
    xhs_client.search_notes.assert_awaited_once_with(
        keyword="上海 旅行",
        sort="popular",
        note_type="image",
        page=2,
    )


@pytest.mark.asyncio
async def test_xiaohongshu_search_tool_accepts_max_results_and_truncates_items():
    from tools.xiaohongshu_search import make_xiaohongshu_search_tool

    xhs_client = SimpleNamespace(
        search_notes=AsyncMock(
            return_value={
                "items": [
                    {
                        "id": "note_1",
                        "xsec_token": "token_1",
                        "note_card": {
                            "title": "攻略 1",
                            "type": "image",
                            "user": {"nickname": "Alice"},
                            "interact_info": {"liked_count": "12"},
                        },
                    },
                    {
                        "id": "note_2",
                        "xsec_token": "token_2",
                        "note_card": {
                            "title": "攻略 2",
                            "type": "image",
                            "user": {"nickname": "Bob"},
                            "interact_info": {"liked_count": "9"},
                        },
                    },
                ],
                "has_more": True,
            }
        ),
        read_note=AsyncMock(),
        get_comments=AsyncMock(),
    )

    tool_fn = make_xiaohongshu_search_tool(xhs_client=xhs_client)
    result = await tool_fn(
        operation="search_notes",
        keyword="赛里木湖 夏季 体验",
        max_results=1,
    )

    assert [item["note_id"] for item in result["items"]] == ["note_1"]
    assert [item["note_id"] for item in result["_metadata"]["items"]] == ["note_1"]
    xhs_client.search_notes.assert_awaited_once_with(
        keyword="赛里木湖 夏季 体验",
        sort="general",
        note_type="all",
        page=1,
    )


@pytest.mark.asyncio
async def test_xiaohongshu_search_tool_read_and_comments():
    from tools.xiaohongshu_search import make_xiaohongshu_search_tool

    xhs_client = SimpleNamespace(
        search_notes=AsyncMock(),
        read_note=AsyncMock(
            return_value={
                "note_id": "note_1",
                "title": "上海旅行攻略",
                "desc": "两天一夜路线",
                "type": "normal",
                "user": {"nickname": "Alice"},
                "interact_info": {
                    "liked_count": "18",
                    "collected_count": "9",
                    "comment_count": "3",
                    "share_count": "1",
                },
                "tag_list": [{"name": "citywalk"}],
                "image_list": [{"url": "1.jpg"}],
            }
        ),
        get_comments=AsyncMock(
            return_value={
                "comments": [
                    {
                        "id": "comment_1",
                        "content": "收藏了",
                        "like_count": "5",
                        "sub_comment_count": 1,
                        "user_info": {"nickname": "Bob"},
                    }
                ],
                "has_more": False,
                "cursor": "",
                "total_fetched": 1,
                "pages_fetched": 1,
            }
        ),
    )

    tool_fn = make_xiaohongshu_search_tool(xhs_client=xhs_client)

    read_result = await tool_fn(operation="read_note", note_ref="note_1")
    assert read_result["note"]["title"] == "上海旅行攻略"
    assert read_result["note"]["url"].endswith("/note_1")
    assert read_result["_metadata"]["note"]["author"] == "Alice"

    comments_result = await tool_fn(
        operation="get_comments",
        note_ref="https://www.xiaohongshu.com/explore/note_1?xsec_token=abc",
        fetch_all=True,
    )
    assert comments_result["comments"][0]["nickname"] == "Bob"
    assert comments_result["_metadata"]["comments"][0]["comment_id"] == "comment_1"
    assert comments_result["_metadata"]["total_fetched"] == 1


@pytest.mark.asyncio
async def test_xiaohongshu_search_tool_requires_operation_specific_inputs():
    from tools.xiaohongshu_search import make_xiaohongshu_search_tool

    xhs_client = SimpleNamespace(
        search_notes=AsyncMock(),
        read_note=AsyncMock(),
        get_comments=AsyncMock(),
    )

    tool_fn = make_xiaohongshu_search_tool(xhs_client=xhs_client)

    with pytest.raises(ToolError, match="keyword"):
        await tool_fn(operation="search_notes")

    with pytest.raises(ToolError, match="note_ref"):
        await tool_fn(operation="read_note")


@pytest.mark.asyncio
async def test_xiaohongshu_search_tool_registration(monkeypatch):
    from main import create_app

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    app = create_app()

    sessions = None
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or getattr(endpoint, "__name__", "") != "create_session":
            continue
        for name, cell in zip(
            endpoint.__code__.co_freevars, endpoint.__closure__ or ()
        ):
            if name == "sessions":
                sessions = cell.cell_contents
                break

    assert sessions is not None

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/sessions")

    session_id = resp.json()["session_id"]
    agent = sessions[session_id]["agent"]
    tool_def = agent.tool_engine.get_tool("xiaohongshu_search")
    assert tool_def is not None
    assert tool_def.name == "xiaohongshu_search"
    assert "用户生成内容视角的经验信号" in tool_def.description
    assert "标题不足以支撑判断" in tool_def.description
    assert "评论观点会明显影响判断时" in tool_def.description
    assert "phase 1" not in tool_def.description


@pytest.mark.asyncio
async def test_tool_engine_extracts_xiaohongshu_metadata():
    from tools.engine import ToolEngine
    from tools.xiaohongshu_search import make_xiaohongshu_search_tool

    xhs_client = SimpleNamespace(
        search_notes=AsyncMock(
            return_value={
                "items": [
                    {
                        "id": "note_1",
                        "xsec_token": "token_1",
                        "note_card": {
                            "title": "上海旅行攻略",
                            "type": "image",
                            "user": {"nickname": "Alice"},
                            "interact_info": {"liked_count": "12"},
                        },
                    }
                ],
                "has_more": True,
            }
        ),
        read_note=AsyncMock(),
        get_comments=AsyncMock(),
    )

    engine = ToolEngine()
    engine.register(make_xiaohongshu_search_tool(xhs_client=xhs_client))
    result = await engine.execute(
        ToolCall(
            id="tc_1",
            name="xiaohongshu_search",
            arguments={"operation": "search_notes", "keyword": "上海 旅行"},
        )
    )

    assert result.status == "success"
    assert result.data == {
        "operation": "search_notes",
        "keyword": "上海 旅行",
        "has_more": True,
        "items": [
            {
                "note_id": "note_1",
                "title": "上海旅行攻略",
                "liked_count": "12",
                "note_type": "image",
                "url": "https://www.xiaohongshu.com/explore/note_1?xsec_token=token_1&xsec_source=pc_search",
            }
        ],
    }
    assert result.metadata is not None
    assert result.metadata["duration_ms"] >= 0
    metadata_without_duration = {
        key: value for key, value in result.metadata.items() if key != "duration_ms"
    }
    assert metadata_without_duration == {
        "page": 1,
        "max_results": 5,
        "source": "xiaohongshu_cli",
        "items": [
            {
                "note_id": "note_1",
                "author": "Alice",
                "xsec_token": "token_1",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Tests for bare-ID → URL conversion fix
# ---------------------------------------------------------------------------


def test_ensure_note_url_bare_id_no_token():
    from tools.xiaohongshu_cli import _ensure_note_url

    url = _ensure_note_url("691944970000000005012159")
    assert url == "https://www.xiaohongshu.com/explore/691944970000000005012159"


def test_ensure_note_url_bare_id_with_token():
    from tools.xiaohongshu_cli import _ensure_note_url

    url = _ensure_note_url("note_1", "tok_abc")
    assert "xsec_token=tok_abc" in url
    assert url.startswith("https://www.xiaohongshu.com/explore/note_1?")


def test_ensure_note_url_already_url():
    from tools.xiaohongshu_cli import _ensure_note_url

    original = "https://www.xiaohongshu.com/explore/note_1?xsec_token=abc"
    assert _ensure_note_url(original) == original


def test_ensure_note_url_xhslink():
    from tools.xiaohongshu_cli import _ensure_note_url

    original = "https://xhslink.com/abc123"
    assert _ensure_note_url(original) == original


@pytest.mark.asyncio
async def test_read_note_bare_id_converted_to_url(monkeypatch):
    """read_note should convert a bare ID to a full URL before calling CLI."""
    from tools.xiaohongshu_cli import XiaohongshuCliClient

    calls: list[tuple] = []
    payloads = [
        # status check
        _FakeProcess(
            stdout=json.dumps(
                {"ok": True, "schema_version": "1", "data": {"authenticated": True}}
            )
        ),
        # read response
        _FakeProcess(
            stdout=json.dumps(
                {
                    "ok": True,
                    "schema_version": "1",
                    "data": {"note_id": "note_1", "title": "Test"},
                }
            )
        ),
    ]

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return payloads.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    client = XiaohongshuCliClient(timeout=3)
    await client.read_note("note_1", xsec_token="tok_abc")

    # calls[1] = ("xhs", "read", url, "--xsec-token", "tok_abc", "--json")
    read_args = calls[1]
    assert read_args[1] == "read"
    assert "https://www.xiaohongshu.com/explore/note_1" in read_args[2]
    assert "--xsec-token" in read_args
    assert "tok_abc" in read_args


@pytest.mark.asyncio
async def test_get_comments_bare_id_converted_to_url(monkeypatch):
    """get_comments should convert a bare ID to a full URL before calling CLI."""
    from tools.xiaohongshu_cli import XiaohongshuCliClient

    calls: list[tuple] = []
    payloads = [
        _FakeProcess(
            stdout=json.dumps(
                {"ok": True, "schema_version": "1", "data": {"authenticated": True}}
            )
        ),
        _FakeProcess(
            stdout=json.dumps(
                {
                    "ok": True,
                    "schema_version": "1",
                    "data": {"comments": [], "has_more": False},
                }
            )
        ),
    ]

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return payloads.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    client = XiaohongshuCliClient(timeout=3)
    await client.get_comments("note_1", xsec_token="tok_abc")

    # calls[1] = ("xhs", "comments", url, "--xsec-token", "tok_abc", "--json")
    comments_args = calls[1]
    assert comments_args[1] == "comments"
    assert "https://www.xiaohongshu.com/explore/note_1" in comments_args[2]
    assert "--xsec-token" in comments_args
    assert "tok_abc" in comments_args


@pytest.mark.asyncio
async def test_read_note_extracts_xsec_token_from_url(monkeypatch):
    """read_note should auto-extract xsec_token from a URL when not explicitly passed."""
    from tools.xiaohongshu_cli import XiaohongshuCliClient

    calls: list[tuple] = []
    payloads = [
        _FakeProcess(
            stdout=json.dumps(
                {"ok": True, "schema_version": "1", "data": {"authenticated": True}}
            )
        ),
        _FakeProcess(
            stdout=json.dumps(
                {
                    "ok": True,
                    "schema_version": "1",
                    "data": {"note_id": "note_1", "title": "Test"},
                }
            )
        ),
    ]

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return payloads.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    client = XiaohongshuCliClient(timeout=3)
    url = "https://www.xiaohongshu.com/explore/note_1?xsec_token=tok_from_url&xsec_source=pc_search"
    await client.read_note(url)

    read_args = calls[1]
    assert "--xsec-token" in read_args
    assert "tok_from_url" in read_args


@pytest.mark.asyncio
async def test_search_tool_read_note_extracts_token_from_url():
    """xiaohongshu_search read_note should extract xsec_token from URL before calling client."""
    from tools.xiaohongshu_search import make_xiaohongshu_search_tool

    xhs_client = SimpleNamespace(
        search_notes=AsyncMock(),
        read_note=AsyncMock(
            return_value={
                "note_id": "note_1",
                "title": "Test",
                "desc": "",
                "type": "normal",
                "user": {"nickname": "Alice"},
                "interact_info": {},
                "tag_list": [],
                "image_list": [],
            }
        ),
        get_comments=AsyncMock(),
    )

    tool_fn = make_xiaohongshu_search_tool(xhs_client=xhs_client)
    url = "https://www.xiaohongshu.com/explore/note_1?xsec_token=tok_extracted"
    await tool_fn(operation="read_note", note_ref=url)

    xhs_client.read_note.assert_awaited_once_with(
        note_ref=url, xsec_token="tok_extracted"
    )
