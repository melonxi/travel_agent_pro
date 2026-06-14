"""Language-aware character→token estimation.

A single `len(text) // N` ratio is wrong in opposite directions for CJK vs
ASCII text: under a real tokenizer one CJK character costs roughly one token,
while several ASCII characters share a token. Splitting the count by character
class and applying per-class coefficients removes that systematic bias.

The coefficients are not guessed -- they are fit from persisted flight-recorder
evidence (prompt artifacts paired with real provider `input_tokens`) via
`scripts/calibrate-token-estimator.py`. Re-run that script to recalibrate when
the model or tokenizer changes.
"""
from __future__ import annotations

# Fit from real (prompt, input_tokens) pairs on deepseek-v4-flash via
# scripts/calibrate-token-estimator.py. With these coefficients the estimate/real
# ratio is ~1.02 (median 1.02); the old flat len//3 ran ~0.72 (28% under-estimate).
# Recalibrate per model/tokenizer.
CJK_TOKENS_PER_CHAR = 0.80
OTHER_TOKENS_PER_CHAR = 0.339


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3000 <= code <= 0x303F  # CJK symbols & punctuation
        or 0x3040 <= code <= 0x30FF  # Hiragana + Katakana
        or 0x3400 <= code <= 0x4DBF  # CJK Ext A
        or 0x4E00 <= code <= 0x9FFF  # CJK Unified
        or 0xF900 <= code <= 0xFAFF  # CJK compatibility
        or 0xFF00 <= code <= 0xFFEF  # full-width forms
    )


def count_char_classes(text: str) -> tuple[int, int]:
    """Return (cjk_char_count, other_char_count) for *text*."""
    cjk = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
    return cjk, len(text) - cjk


def estimate_text_tokens(text: str) -> int:
    """Language-aware character→token estimate."""
    if not text:
        return 0
    cjk, other = count_char_classes(text)
    estimate = cjk * CJK_TOKENS_PER_CHAR + other * OTHER_TOKENS_PER_CHAR
    return max(1, round(estimate))
