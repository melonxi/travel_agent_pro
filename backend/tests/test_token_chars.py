from agent.token_chars import (
    CJK_TOKENS_PER_CHAR,
    OTHER_TOKENS_PER_CHAR,
    count_char_classes,
    estimate_text_tokens,
)


def test_empty_text_is_zero():
    assert estimate_text_tokens("") == 0


def test_count_char_classes_splits_cjk_and_ascii():
    cjk, other = count_char_classes("东京tokyo")
    assert cjk == 2
    assert other == 5


def test_count_includes_japanese_kana_and_fullwidth():
    # hiragana + katakana + fullwidth digit all count as CJK-class
    cjk, other = count_char_classes("とうきょうトウキョウ１")
    assert other == 0
    assert cjk == 11


def test_cjk_costs_more_tokens_per_char_than_ascii():
    cjk_only = "东" * 100
    ascii_only = "a" * 100
    assert estimate_text_tokens(cjk_only) > estimate_text_tokens(ascii_only)


def test_cjk_estimate_tracks_calibrated_coefficient():
    s = "东" * 100
    assert estimate_text_tokens(s) == round(100 * CJK_TOKENS_PER_CHAR)


def test_ascii_estimate_tracks_calibrated_coefficient():
    s = "a" * 100
    assert estimate_text_tokens(s) == round(100 * OTHER_TOKENS_PER_CHAR)


def test_fixes_legacy_cjk_under_estimate():
    # The old heuristic was len//3; for CJK the calibrated estimate must be higher
    # because a CJK char costs ~0.8 token, not ~0.33.
    s = "新宿是东京最繁华的商业区之一" * 5
    assert estimate_text_tokens(s) > len(s) // 3


def test_estimate_is_monotonic_in_length():
    short = "东京旅行"
    long = "东京旅行" * 10
    assert estimate_text_tokens(long) > estimate_text_tokens(short)


def test_mixed_text_between_pure_cjk_and_pure_ascii_rates():
    n = 200
    mixed = ("东a" * (n // 2))
    cjk_rate = estimate_text_tokens("东" * n) / n
    ascii_rate = estimate_text_tokens("a" * n) / n
    mixed_rate = estimate_text_tokens(mixed) / n
    assert ascii_rate < mixed_rate < cjk_rate
