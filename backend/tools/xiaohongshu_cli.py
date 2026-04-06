# backend/tools/xiaohongshu_cli.py
from __future__ import annotations

import asyncio
import json
import os
from asyncio.subprocess import PIPE
from typing import Any
from urllib.parse import parse_qs, urlparse

from tools.base import ToolError


class XiaohongshuCliClient:
    def __init__(self, cli_bin: str = "xhs", timeout: int = 30) -> None:
        self.cli_bin = cli_bin
        self.timeout = timeout

    async def ensure_authenticated(self) -> dict[str, Any]:
        payload = await self._run_json("status")
        data = payload.get("data", {})
        if not data.get("authenticated"):
            raise ToolError(
                "Xiaohongshu CLI is not authenticated",
                error_code="NOT_AUTHENTICATED",
                suggestion="Run `xhs login` or `xhs login --qrcode` first.",
            )
        return data

    async def search_notes(
        self,
        keyword: str,
        sort: str = "general",
        note_type: str = "all",
        page: int = 1,
    ) -> dict[str, Any]:
        await self.ensure_authenticated()
        return await self._run_data(
            "search",
            keyword,
            "--sort",
            sort,
            "--type",
            note_type,
            "--page",
            str(page),
        )

    async def read_note(self, note_ref: str, xsec_token: str = "") -> dict[str, Any]:
        await self.ensure_authenticated()
        args = ["read", note_ref]
        if xsec_token:
            args.extend(["--xsec-token", xsec_token])
        return await self._run_data(*args)

    async def get_comments(
        self,
        note_ref: str,
        cursor: str = "",
        xsec_token: str = "",
        fetch_all: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_authenticated()
        args = ["comments", note_ref]
        if cursor:
            args.extend(["--cursor", cursor])
        if xsec_token:
            args.extend(["--xsec-token", xsec_token])
        if fetch_all:
            args.append("--all")
        return await self._run_data(*args)

    async def _run_data(self, *args: str) -> dict[str, Any]:
        payload = await self._run_json(*args)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ToolError(
                "Xiaohongshu CLI returned an unexpected payload",
                error_code="INVALID_CLI_RESPONSE",
                suggestion="Check the xhs CLI output format and upgrade the tool if needed.",
            )
        return data

    async def _run_json(self, *args: str) -> dict[str, Any]:
        cmd = (self.cli_bin, *args, "--json")
        env = os.environ.copy()
        env["OUTPUT"] = "json"

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=PIPE,
                stderr=PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                f"Xiaohongshu CLI not found: {self.cli_bin}",
                error_code="CLI_NOT_FOUND",
                suggestion="Install it with `uv tool install xiaohongshu-cli` or set XHS_CLI_BIN.",
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ToolError(
                f"Xiaohongshu CLI timed out after {self.timeout}s",
                error_code="TIMEOUT",
                suggestion="Retry later or increase xhs.cli_timeout in config.yaml.",
            ) from exc

        output = stdout.decode("utf-8", errors="ignore").strip()
        stderr_text = stderr.decode("utf-8", errors="ignore").strip()
        raw = output or stderr_text

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"Invalid JSON from Xiaohongshu CLI: {raw[:200]}",
                error_code="INVALID_CLI_RESPONSE",
                suggestion="Check xhs CLI installation or upgrade it to the latest version.",
            ) from exc

        if payload.get("ok") is False:
            self._raise_cli_error(payload.get("error", {}))

        return payload

    def _raise_cli_error(self, error: dict[str, Any]) -> None:
        code = str(error.get("code", "api_error"))
        message = str(error.get("message", "Unknown Xiaohongshu CLI error"))

        if code == "not_authenticated":
            raise ToolError(
                message,
                error_code="NOT_AUTHENTICATED",
                suggestion="Run `xhs login` or `xhs login --qrcode` first.",
            )
        if code == "verification_required":
            raise ToolError(
                message,
                error_code="VERIFICATION_REQUIRED",
                suggestion="Open Xiaohongshu in a browser, complete the captcha, then retry.",
            )
        if code == "ip_blocked":
            raise ToolError(
                message,
                error_code="IP_BLOCKED",
                suggestion="Switch network and retry later.",
            )
        if code == "api_error" and (
            '"code": -104' in message
            or '"code":-104' in message
            or "没有权限访问" in message
        ):
            raise ToolError(
                f"Xiaohongshu search permission denied: {message}",
                error_code="PERMISSION_DENIED",
                suggestion="Log in with a full-access Xiaohongshu account and retry.",
            )

        raise ToolError(
            message,
            error_code="API_ERROR" if code == "api_error" else code.upper(),
            suggestion="Retry later or upgrade the xiaohongshu-cli tool.",
        )


def extract_xsec_token(note_ref: str) -> str:
    if "xiaohongshu.com" not in note_ref:
        return ""

    parsed = urlparse(note_ref)
    query = parse_qs(parsed.query)
    return query.get("xsec_token", [""])[0]
