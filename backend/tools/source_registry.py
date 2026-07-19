"""Session-scoped registry of tool-fetched sources.

web_search 等检索工具把每条带 URL 的结果登记为 source_id；EvidenceRecord
通过 source_ref 引用它。校验层据此把「LLM 自填的来源」升级为「可回溯到
真实工具调用的来源」：伪造的 source_ref 无法通过校验（fail closed）。

- 文件形态与 Phase3CandidateStore 一致：<root>/<session_id>.jsonl，append-only。
- source_id 由 (session_id, url) 决定性哈希生成：同一 URL 重复登记幂等，
  并行 Worker 并发登记同一来源最多产生重复行，不产生歧义 id。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

SOURCE_ID_PATTERN = re.compile(r"^src_[0-9a-f]{10}$")


def source_id_for(session_id: str, url: str) -> str:
    digest = hashlib.sha1(f"{session_id}\n{url}".encode("utf-8")).hexdigest()
    return f"src_{digest[:10]}"


@dataclass(frozen=True)
class SourceRegistry:
    root: Path | str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    def _session_file(self, session_id: str) -> Path:
        if not _SAFE_SEGMENT_RE.match(session_id or ""):
            raise ValueError(f"unsafe session_id segment: {session_id!r}")
        return Path(self.root) / f"{session_id}.jsonl"

    def register(
        self,
        session_id: str,
        *,
        url: str,
        title: str = "",
        tool_name: str = "",
    ) -> str:
        """登记一条来源并返回 source_id；同一 URL 幂等。"""
        if not isinstance(url, str) or not url.strip().startswith(
            ("http://", "https://")
        ):
            raise ValueError(f"source url must be http(s), got: {url!r}")
        normalized_url = url.strip()
        source_id = source_id_for(session_id, normalized_url)
        if self.lookup(session_id, source_id) is not None:
            return source_id
        record = {
            "source_id": source_id,
            "url": normalized_url,
            "title": str(title or ""),
            "tool_name": str(tool_name or ""),
            "registered_at": datetime.now().isoformat(),
        }
        path = self._session_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return source_id

    def lookup(self, session_id: str, source_id: str) -> dict[str, Any] | None:
        """按 source_id 查回登记记录；不存在返回 None。"""
        path = self._session_file(session_id)
        if not path.exists():
            return None
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("source_id") == source_id:
                return record
        return None
