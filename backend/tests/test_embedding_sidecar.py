from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

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


def test_compute_text_hash_is_stable_and_hex():
    from memory.embedding_sidecar import compute_text_hash

    h1 = compute_text_hash("hello")
    h2 = compute_text_hash("hello")
    h3 = compute_text_hash("hello ")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_sidecar_row_default_construction():
    from memory.embedding_sidecar import (
        PROFILE_TEXT_BUILDER_VERSION,
        SLICE_TEXT_BUILDER_VERSION,
        SidecarRow,
    )

    assert PROFILE_TEXT_BUILDER_VERSION == "profile_item_text:v1"
    assert SLICE_TEXT_BUILDER_VERSION == "episode_slice_text:v1"

    row = SidecarRow(
        source="profile",
        item_id="stable_preferences:hotel:quiet",
        text_hash="a" * 64,
        text_builder="profile_item_text:v1",
        embedding_provider="fastembed",
        embedding_model="BAAI/bge-small-zh-v1.5",
        dimension=2,
        vector=[1.0, 0.0],
        bucket="stable_preferences",
        created_at="2026-05-22T10:00:00Z",
        updated_at="2026-05-22T10:00:00Z",
    )
    assert row.source == "profile"
    assert row.vector == [1.0, 0.0]


def test_store_creates_index_db_on_first_use(tmp_path: Path):
    from memory.embedding_sidecar import SidecarStore

    store = SidecarStore(data_dir=tmp_path)
    store.ensure_user_index("u1")

    db_path = tmp_path / "users" / "u1" / "memory" / "embeddings" / "index.db"
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert "embedding_index" in {row[0] for row in rows}
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_store_returns_per_user_rlock():
    from memory.embedding_sidecar import SidecarStore

    store = SidecarStore(data_dir=Path("."))
    lock_a = store._lock_for("u1")
    lock_a_again = store._lock_for("u1")
    lock_b = store._lock_for("u2")
    assert lock_a is lock_a_again
    assert lock_a is not lock_b
    assert isinstance(lock_a, type(threading.RLock()))


def test_fetch_many_returns_empty_dict_for_empty_index(tmp_path: Path):
    from memory.embedding_sidecar import SidecarStore

    store = SidecarStore(data_dir=tmp_path)
    result = store.fetch_many(
        "u1", [("profile", "stable_preferences:hotel:quiet")]
    )
    assert result == {}


def test_fetch_many_returns_only_matching_keys(tmp_path: Path):
    from memory.embedding_sidecar import SidecarRow, SidecarStore, encode_vector

    store = SidecarStore(data_dir=tmp_path)
    store.ensure_user_index("u1")

    db_path = store._db_path("u1")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO embedding_index VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "profile",
            "stable_preferences:hotel:quiet",
            "a" * 64,
            "profile_item_text:v1",
            "fastembed",
            "BAAI/bge-small-zh-v1.5",
            2,
            encode_vector([1.0, 0.0]),
            "stable_preferences",
            "2026-05-22T10:00:00Z",
            "2026-05-22T10:00:00Z",
        ),
    )
    conn.commit()
    conn.close()

    result = store.fetch_many(
        "u1",
        [
            ("profile", "stable_preferences:hotel:quiet"),
            ("profile", "stable_preferences:hotel:missing"),
        ],
    )
    assert set(result.keys()) == {("profile", "stable_preferences:hotel:quiet")}
    row = result[("profile", "stable_preferences:hotel:quiet")]
    assert isinstance(row, SidecarRow)
    assert row.vector == [1.0, 0.0]
    assert row.dimension == 2
    assert row.embedding_model == "BAAI/bge-small-zh-v1.5"


def test_upsert_many_inserts_new_rows(tmp_path: Path):
    from memory.embedding_sidecar import SidecarRow, SidecarStore

    store = SidecarStore(data_dir=tmp_path)
    row = SidecarRow(
        source="profile",
        item_id="stable_preferences:hotel:quiet",
        text_hash="a" * 64,
        text_builder="profile_item_text:v1",
        embedding_provider="fastembed",
        embedding_model="BAAI/bge-small-zh-v1.5",
        dimension=2,
        vector=[1.0, 0.0],
        bucket="stable_preferences",
        created_at="2026-05-22T10:00:00Z",
        updated_at="2026-05-22T10:00:00Z",
    )
    store.upsert_many("u1", [row])

    fetched = store.fetch_many(
        "u1", [("profile", "stable_preferences:hotel:quiet")]
    )
    assert fetched[("profile", "stable_preferences:hotel:quiet")].vector == [
        1.0,
        0.0,
    ]


def test_upsert_many_overwrites_old_hash_under_same_key(tmp_path: Path):
    from memory.embedding_sidecar import SidecarRow, SidecarStore

    store = SidecarStore(data_dir=tmp_path)
    base = dict(
        source="profile",
        item_id="stable_preferences:hotel:quiet",
        text_builder="profile_item_text:v1",
        embedding_provider="fastembed",
        embedding_model="BAAI/bge-small-zh-v1.5",
        dimension=2,
        bucket="stable_preferences",
        created_at="2026-05-22T10:00:00Z",
        updated_at="2026-05-22T10:00:00Z",
    )
    store.upsert_many("u1", [SidecarRow(text_hash="a" * 64, vector=[1.0, 0.0], **base)])
    store.upsert_many("u1", [SidecarRow(text_hash="b" * 64, vector=[0.0, 1.0], **base)])

    db_path = store._db_path("u1")
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM embedding_index").fetchone()[0]
        assert count == 1
        text_hash = conn.execute(
            "SELECT text_hash FROM embedding_index"
        ).fetchone()[0]
        assert text_hash == "b" * 64
    finally:
        conn.close()


def test_is_row_valid_when_all_fields_match():
    from memory.embedding_sidecar import SidecarRow, is_row_valid_for

    row = SidecarRow(
        source="profile",
        item_id="x",
        text_hash="h",
        text_builder="profile_item_text:v1",
        embedding_provider="fastembed",
        embedding_model="m",
        dimension=2,
        vector=[1.0, 0.0],
        bucket="",
        created_at="",
        updated_at="",
    )
    assert (
        is_row_valid_for(
            row,
            expected_hash="h",
            expected_text_builder="profile_item_text:v1",
            expected_provider="fastembed",
            expected_model="m",
            expected_dimension=2,
        )
        is True
    )


def test_is_row_invalid_when_any_field_mismatches():
    from memory.embedding_sidecar import SidecarRow, is_row_valid_for

    row = SidecarRow(
        source="profile",
        item_id="x",
        text_hash="h",
        text_builder="profile_item_text:v1",
        embedding_provider="fastembed",
        embedding_model="m",
        dimension=2,
        vector=[1.0, 0.0],
        bucket="",
        created_at="",
        updated_at="",
    )
    base = dict(
        expected_hash="h",
        expected_text_builder="profile_item_text:v1",
        expected_provider="fastembed",
        expected_model="m",
        expected_dimension=2,
    )
    for field_name, bad_value in [
        ("expected_hash", "other"),
        ("expected_text_builder", "profile_item_text:v2"),
        ("expected_provider", "openai"),
        ("expected_model", "other"),
        ("expected_dimension", 3),
    ]:
        kwargs = dict(base, **{field_name: bad_value})
        assert is_row_valid_for(row, **kwargs) is False


def test_delete_for_item_removes_matching_row(tmp_path: Path):
    from memory.embedding_sidecar import SidecarRow, SidecarStore

    store = SidecarStore(data_dir=tmp_path)
    row = SidecarRow(
        source="profile",
        item_id="stable_preferences:hotel:quiet",
        text_hash="a" * 64,
        text_builder="profile_item_text:v1",
        embedding_provider="fastembed",
        embedding_model="m",
        dimension=2,
        vector=[1.0, 0.0],
        bucket="stable_preferences",
        created_at="t",
        updated_at="t",
    )
    store.upsert_many("u1", [row])

    store.delete_for_item("u1", "profile", "stable_preferences:hotel:quiet")
    result = store.fetch_many(
        "u1", [("profile", "stable_preferences:hotel:quiet")]
    )
    assert result == {}


def test_delete_for_item_is_noop_when_db_missing(tmp_path: Path):
    from memory.embedding_sidecar import SidecarStore

    store = SidecarStore(data_dir=tmp_path)
    store.delete_for_item("u1", "profile", "anything")  # no raise
