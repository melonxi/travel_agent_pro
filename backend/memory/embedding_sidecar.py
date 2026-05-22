from __future__ import annotations

import struct


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
