from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass


PROFILE_TEXT_BUILDER_VERSION = "profile_item_text:v1"
SLICE_TEXT_BUILDER_VERSION = "episode_slice_text:v1"


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
