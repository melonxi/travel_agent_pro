"""Pure signal extractor for recall short-circuit.

Layer 1 of the three-layer recall gate:
  extract_signals -> rule engine (recall_gate.apply_recall_short_circuit) -> LLM gate.

This module performs NO decision logic. It only reports which tokens from
each closed vocabulary matched the message. The rule engine is responsible
for interpreting the resulting signal dict.
"""
from __future__ import annotations

HISTORY_SIGNALS: tuple[str, ...] = (
    "我是不是说过",
    "按我的习惯",
    "上次",
    "之前",
    "以前",
    "跟之前一样",
    "跟以前一样",
    "像那次",
    "像上次",
)

STYLE_SIGNALS: tuple[str, ...] = (
    "照旧",
    "老样子",
    "老规矩",
    "常规偏好",
    "平时喜欢",
    "像我平时",
    "别太折腾",
    "轻松点",
    "舒服点",
)

RECOMMEND_SIGNALS: tuple[str, ...] = (
    "推荐",
    "帮我选",
    "帮我订",
    "帮我安排",
    "哪家",
    "哪趟",
    "哪个更",
    "更合适",
    "适合我",
    "怎么订",
    "换一家",
    "换一个",
    "哪里",
)

FACT_SCOPE_SIGNALS: tuple[str, ...] = (
    "这次",
    "本次",
    "当前",
    "现在",
)

FACT_FIELD_SIGNALS: tuple[str, ...] = (
    "预算",
    "几号",
    "出发",
    "骨架",
    "日期",
    "酒店",
    "航班",
    "车次",
    "目的地",
    "天数",
)

ACK_SYS_SIGNALS: tuple[str, ...] = (
    "继续",
    "好的",
    "嗯",
    "OK",
    "ok",
    "就这个",
    "重新开始",
)

Signals = dict[str, tuple[str, ...]]

_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("history", HISTORY_SIGNALS),
    ("style", STYLE_SIGNALS),
    ("recommend", RECOMMEND_SIGNALS),
    ("fact_scope", FACT_SCOPE_SIGNALS),
    ("fact_field", FACT_FIELD_SIGNALS),
    ("ack_sys", ACK_SYS_SIGNALS),
)


def extract_signals(text: str) -> Signals:
    """Return which tokens matched each category.

    Case-sensitive substring match. Returns tuples (hashable, stable order)
    keyed by category name. Every category key is always present.
    """
    source = text or ""
    result: Signals = {}
    for name, vocab in _CATEGORIES:
        hits = tuple(token for token in vocab if token in source)
        result[name] = hits
    return result
