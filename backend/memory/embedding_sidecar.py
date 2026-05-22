from __future__ import annotations

import hashlib
import sqlite3
import struct
import threading
from dataclasses import dataclass
from pathlib import Path


PROFILE_TEXT_BUILDER_VERSION = "profile_item_text:v1"
SLICE_TEXT_BUILDER_VERSION = "episode_slice_text:v1"


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS embedding_index (
    source              TEXT NOT NULL,
    item_id             TEXT NOT NULL,
    text_hash           TEXT NOT NULL,
    text_builder        TEXT NOT NULL,
    embedding_provider  TEXT NOT NULL,
    embedding_model     TEXT NOT NULL,
    dimension           INTEGER NOT NULL,
    vector              BLOB NOT NULL,
    bucket              TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
)
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_embedding_index_source
    ON embedding_index (source)
"""


def compute_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SidecarRow:
    source: str
    item_id: str
    text_hash: str
    text_builder: str
    embedding_provider: str
    embedding_model: str
    dimension: int
    vector: list[float]
    bucket: str
    created_at: str
    updated_at: str


def encode_vector(vector: list[float]) -> bytes:
    if not vector:
        return b""
    return struct.pack(f"<{len(vector)}f", *vector)


def decode_vector(blob: bytes, dimension: int) -> list[float]:
    if dimension == 0:
        if blob:
            raise ValueError("non-empty blob for dimension=0")
        return []
    expected_bytes = 4 * dimension
    if len(blob) != expected_bytes:
        raise ValueError(
            f"vector blob size mismatch: got {len(blob)} bytes, want {expected_bytes}"
        )
    return list(struct.unpack(f"<{dimension}f", blob))


class SidecarStore:
    def __init__(self, data_dir: str | Path = "./data") -> None:
        self.data_dir = Path(data_dir)
        self._lock_registry: dict[str, threading.RLock] = {}
        self._registry_guard = threading.Lock()

    def _lock_for(self, user_id: str) -> threading.RLock:
        with self._registry_guard:
            lock = self._lock_registry.get(user_id)
            if lock is None:
                lock = threading.RLock()
                self._lock_registry[user_id] = lock
            return lock

    def _db_path(self, user_id: str) -> Path:
        return (
            self.data_dir
            / "users"
            / user_id
            / "memory"
            / "embeddings"
            / "index.db"
        )

    def _connect(self, user_id: str) -> sqlite3.Connection:
        path = self._db_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(_CREATE_TABLE_SQL)
        conn.execute(_CREATE_INDEX_SQL)
        return conn

    def ensure_user_index(self, user_id: str) -> None:
        with self._lock_for(user_id):
            conn = self._connect(user_id)
            conn.close()
