# backend/tools/flyai_client.py
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

logger = logging.getLogger(__name__)


class FlyAIClient:
    """Async wrapper around the flyai Node.js CLI tool.

    All public methods return list[dict]. They never raise exceptions —
    errors are logged and an empty list is returned (graceful degradation).
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

    async def fast_search(self, query: str) -> list[dict]:
        return await self._run("fliggy-fast-search", query=query)

    async def search_flight(self, origin: str, **kwargs) -> list[dict]:
        return await self._run("search-flight", origin=origin, **kwargs)

    async def search_hotels(self, dest_name: str, **kwargs) -> list[dict]:
        return await self._run("search-hotels", dest_name=dest_name, **kwargs)

    async def search_poi(self, city_name: str, **kwargs) -> list[dict]:
        return await self._run("search-poi", city_name=city_name, **kwargs)

    async def _run(self, command: str, **kwargs) -> list[dict]:
        if not self._available:
            return []

        cmd = ["flyai", command]
        for key, value in kwargs.items():
            if value is not None:
                cmd.extend([f"--{key.replace('_', '-')}", str(value)])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
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
            return []
        except Exception as exc:
            logger.warning("FlyAI CLI subprocess error: %s", exc)
            return []

        try:
            data = json.loads(stdout.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("FlyAI CLI invalid JSON: %s", exc)
            return []

        if data.get("status") != 0:
            logger.warning("FlyAI CLI non-zero status: %s", data.get("message"))
            return []

        return data.get("data", {}).get("itemList", [])
