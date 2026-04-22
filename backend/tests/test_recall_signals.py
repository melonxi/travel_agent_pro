from memory.recall_signals import (
    extract_signals,
    HISTORY_SIGNALS,
    STYLE_SIGNALS,
    RECOMMEND_SIGNALS,
    FACT_SCOPE_SIGNALS,
    FACT_FIELD_SIGNALS,
    ACK_SYS_SIGNALS,
)


def test_extract_signals_returns_all_six_keys_for_empty_text():
    out = extract_signals("")
    assert set(out.keys()) == {
        "history", "style", "recommend", "fact_scope", "fact_field", "ack_sys"
    }
    assert all(v == () for v in out.values())


def test_extract_signals_history_hits():
    out = extract_signals("我是不是说过不坐红眼航班？")
    assert "我是不是说过" in out["history"]


def test_extract_signals_style_hits_zhaojiu_and_laoyangzi():
    assert "照旧" in extract_signals("照旧安排就行")["style"]
    assert "老样子" in extract_signals("老样子，别太折腾")["style"]


def test_extract_signals_recommend_hits_on_helper_verbs():
    out = extract_signals("帮我选一个酒店")
    assert "帮我选" in out["recommend"]


def test_extract_signals_fact_scope_and_field_are_separate():
    out = extract_signals("这次预算多少？")
    assert "这次" in out["fact_scope"]
    assert "预算" in out["fact_field"]


def test_extract_signals_ack_sys_ok_case_sensitive():
    assert "OK" in extract_signals("OK 就这个")["ack_sys"]
    assert "ok" in extract_signals("ok 好的")["ack_sys"]


def test_extract_signals_does_not_confuse_style_and_fact():
    out = extract_signals("别太折腾")
    assert "别太折腾" in out["style"]
    assert out["fact_scope"] == ()
    assert out["fact_field"] == ()


def test_extract_signals_multi_hits_same_category():
    out = extract_signals("这次本次的预算和日期")
    assert set(out["fact_scope"]) == {"这次", "本次"}
    assert set(out["fact_field"]) == {"预算", "日期"}


def test_extract_signals_whitespace_only_returns_empty():
    out = extract_signals("   \t\n")
    assert all(v == () for v in out.values())


def test_extract_signals_recommend_hits_zenmeding():
    assert "怎么订" in extract_signals("这次航班怎么订？")["recommend"]
