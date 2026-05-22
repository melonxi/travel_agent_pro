from __future__ import annotations

import pytest

from memory.embedding_sidecar import decode_vector, encode_vector


def test_encode_decode_round_trip_preserves_values():
    vector = [0.0, 1.5, -2.25, 3.14159]
    encoded = encode_vector(vector)
    assert isinstance(encoded, bytes)
    assert len(encoded) == 4 * len(vector)
    decoded = decode_vector(encoded, dimension=len(vector))
    for original, restored in zip(vector, decoded):
        assert restored == pytest.approx(original, rel=1e-6, abs=1e-6)


def test_encode_empty_vector_returns_empty_bytes():
    assert encode_vector([]) == b""
    assert decode_vector(b"", dimension=0) == []


def test_decode_rejects_dimension_mismatch():
    encoded = encode_vector([1.0, 2.0])
    with pytest.raises(ValueError):
        decode_vector(encoded, dimension=3)
