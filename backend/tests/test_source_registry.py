"""SourceRegistry 单元测试：source_id 铸造、幂等登记、伪造引用不可解析。"""

from __future__ import annotations

import pytest

from tools.source_registry import SOURCE_ID_PATTERN, SourceRegistry, source_id_for


def test_source_id_deterministic_and_well_formed():
    a = source_id_for("sess_1", "https://example.com/page")
    b = source_id_for("sess_1", "https://example.com/page")
    other_session = source_id_for("sess_2", "https://example.com/page")
    other_url = source_id_for("sess_1", "https://example.com/other")

    assert a == b
    assert a != other_session
    assert a != other_url
    assert SOURCE_ID_PATTERN.match(a)


def test_register_and_lookup_roundtrip(tmp_path):
    registry = SourceRegistry(tmp_path)
    source_id = registry.register(
        "sess_1",
        url="https://example.com/official",
        title="官网开放信息",
        tool_name="web_search",
    )

    record = registry.lookup("sess_1", source_id)
    assert record is not None
    assert record["url"] == "https://example.com/official"
    assert record["title"] == "官网开放信息"
    assert record["tool_name"] == "web_search"


def test_register_is_idempotent_for_same_url(tmp_path):
    registry = SourceRegistry(tmp_path)
    first = registry.register("sess_1", url="https://example.com/a")
    second = registry.register("sess_1", url="https://example.com/a")

    assert first == second
    lines = (tmp_path / "sess_1.jsonl").read_text(encoding="utf-8").splitlines()
    assert len([line for line in lines if line.strip()]) == 1


def test_lookup_unknown_id_returns_none(tmp_path):
    registry = SourceRegistry(tmp_path)
    registry.register("sess_1", url="https://example.com/a")

    assert registry.lookup("sess_1", "src_0000000000") is None
    # 其他 session 的 id 不可跨会话解析
    other = registry.register("sess_2", url="https://example.com/b")
    assert registry.lookup("sess_1", other) is None


def test_register_rejects_non_http_url(tmp_path):
    registry = SourceRegistry(tmp_path)
    with pytest.raises(ValueError, match="http"):
        registry.register("sess_1", url="javascript:alert(1)")
    with pytest.raises(ValueError, match="http"):
        registry.register("sess_1", url="")


def test_unsafe_session_segment_rejected(tmp_path):
    registry = SourceRegistry(tmp_path)
    with pytest.raises(ValueError, match="unsafe"):
        registry.register("../escape", url="https://example.com/a")
    with pytest.raises(ValueError, match="unsafe"):
        registry.lookup("a/b", "src_0000000000")
