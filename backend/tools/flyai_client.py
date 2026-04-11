# backend/tools/flyai_client.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from typing import Any

logger = logging.getLogger(__name__)


class FlyAIClient:
    """Async wrapper around the flyai Node.js CLI tool (@fly-ai/flyai-cli).

    All public methods return list[dict] (or str for ai_search).
    They never raise exceptions — errors are logged and an empty
    list/string is returned (graceful degradation).
    """

    def __init__(self, timeout: int = 30, api_key: str | None = None) -> None:
        self.timeout = timeout
        self._available = shutil.which("flyai") is not None
        self._env: dict[str, str] | None = None
        if api_key:
            self._env = {**os.environ, "FLYAI_API_KEY": api_key}

    @property
    def available(self) -> bool:
        return self._available

    # -- broad discovery ------------------------------------------------

    async def fast_search(self, query: str) -> list[dict]:
        """keyword-search: cross-category keyword search."""
        return await self._run("keyword-search", query=query)

    async def ai_search(self, query: str) -> str:
        """ai-search: semantic search returning a free-text answer."""
        return await self._run("ai-search", query=query, _raw_data=True)

    # -- category-specific search ----------------------------------------

    async def search_flight(self, origin: str, **kwargs) -> list[dict]:
        return await self._run("search-flight", origin=origin, **kwargs)

    async def search_hotel(self, dest_name: str, **kwargs) -> list[dict]:
        """search-hotel (singular) — replaces old search-hotels."""
        return await self._run("search-hotel", dest_name=dest_name, **kwargs)

    async def search_train(self, origin: str, **kwargs) -> list[dict]:
        return await self._run("search-train", origin=origin, **kwargs)

    async def search_poi(self, city_name: str, **kwargs) -> list[dict]:
        return await self._run("search-poi", city_name=city_name, **kwargs)

    # -- backward compat alias ------------------------------------------

    async def search_hotels(self, dest_name: str, **kwargs) -> list[dict]:
        """Deprecated alias — delegates to search_hotel."""
        return await self.search_hotel(dest_name, **kwargs)

    # -- internals -------------------------------------------------------

    async def _run(self, command: str, *, _raw_data: bool = False, **kwargs) -> Any:
        if not self._available:
            return "" if _raw_data else []

        cmd = ["flyai", command]
        for key, value in kwargs.items():
            if value is not None:
                cmd.extend([f"--{key.replace('_', '-')}", str(value)])

        # Use a temp file for stdout to avoid Node.js pipe flush truncation.
        # Node.js process.stdout.write() is async for pipes and may not flush
        # all data before the process exits.  Shell redirection (>) connects
        # fd 1 directly to a file descriptor, so all data lands on disk.
        fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="flyai_")
        os.close(fd)
        shell_cmd = " ".join(_shell_quote(c) for c in cmd) + f" > {_shell_quote(tmp_path)}"

        try:
            proc = await asyncio.create_subprocess_exec(
                "sh",
                "-c",
                shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            logger.warning("FlyAI CLI timed out for command: %s", command)
            try:
                proc.kill()  # type: ignore[union-attr]
            except ProcessLookupError:
                pass
            _remove_tmp(tmp_path)
            return "" if _raw_data else []
        except Exception as exc:
            logger.warning("FlyAI CLI subprocess error: %s", exc)
            _remove_tmp(tmp_path)
            return "" if _raw_data else []

        try:
            with open(tmp_path, "r", encoding="utf-8") as f:
                raw_output = f.read()
        except OSError as exc:
            logger.warning("FlyAI CLI failed to read temp file: %s", exc)
            return "" if _raw_data else []
        finally:
            _remove_tmp(tmp_path)

        if not raw_output and stdout:
            raw_output = stdout.decode("utf-8", errors="replace")

        try:
            data = json.loads(raw_output)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("FlyAI CLI invalid JSON: %s", exc)
            return "" if _raw_data else []

        if data.get("status") != 0:
            logger.debug("FlyAI CLI non-zero status: %s", data.get("message"))
            return "" if _raw_data else []

        # ai-search returns data as a string, not {itemList: [...]}
        if _raw_data:
            return data.get("data", "")

        return (data.get("data") or {}).get("itemList", [])


def _shell_quote(s: str) -> str:
    """Quote a string for safe shell interpolation."""
    import shlex
    return shlex.quote(s)


def _remove_tmp(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
