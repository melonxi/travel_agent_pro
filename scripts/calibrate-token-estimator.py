#!/usr/bin/env python3
"""Calibrate the heuristic token estimator against real provider usage.

The compaction estimator (`agent.compaction._estimate_text_tokens`) approximates
token counts from character counts. A single `len//N` ratio is wrong in opposite
directions for CJK vs ASCII text, so this script fits per-class coefficients
(`cjk_tokens_per_char`, `other_tokens_per_char`) from persisted flight-recorder
evidence -- no LLM calls required.

For every persisted `llm_prompt` artifact we recover:
  - the prompt body (messages + tools) written at call time
  - the REAL input_tokens from the paired `llm_output` event

We then solve a 2-parameter least-squares fit  real ≈ a*cjk + b*other  and
report the fit quality plus how the current `len//3` heuristic compares.

Usage:
  python scripts/calibrate-token-estimator.py [--db data/sessions.db]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agent.token_chars import count_char_classes  # noqa: E402


def _load_pairs(db_path: Path) -> list[tuple[int, int, int]]:
    """Return (cjk_chars, other_chars, real_input_tokens) per LLM call."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # real input_tokens per llm_call event_id (via its child llm_output)
        real_by_call: dict[str, int] = {}
        for row in conn.execute(
            "SELECT parent_event_id, payload_json FROM trace_events "
            "WHERE event_type = 'llm_output' AND parent_event_id IS NOT NULL"
        ):
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                continue
            tokens = payload.get("input_tokens")
            if isinstance(tokens, int) and tokens > 0:
                real_by_call[row["parent_event_id"]] = tokens

        artifacts = conn.execute(
            "SELECT event_id, storage_path FROM trace_artifacts WHERE kind = 'llm_prompt'"
        ).fetchall()
    finally:
        conn.close()

    pairs: list[tuple[int, int, int]] = []
    missing_files = 0
    for row in artifacts:
        real = real_by_call.get(row["event_id"])
        if real is None or not row["storage_path"]:
            continue
        # storage_path is relative to the configured data dir (e.g.
        # "trace_artifacts/<sess>/<run>/<id>.json"); try the common roots.
        sp = row["storage_path"]
        path = next(
            (p for p in (ROOT / "data" / sp, ROOT / sp, Path(sp)) if p.exists()),
            None,
        )
        if path is None:
            missing_files += 1
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        cjk, other = count_char_classes(text)
        if cjk + other == 0:
            continue
        pairs.append((cjk, other, real))
    if missing_files:
        print(f"(skipped {missing_files} artifacts whose files are gone)")
    return pairs


def _fit_two_param(pairs: list[tuple[int, int, int]]) -> tuple[float, float]:
    """Closed-form least squares for real ≈ a*cjk + b*other (no intercept)."""
    sxx = sum(c * c for c, _, _ in pairs)
    syy = sum(o * o for _, o, _ in pairs)
    sxy = sum(c * o for c, o, _ in pairs)
    sxr = sum(c * r for c, _, r in pairs)
    syr = sum(o * r for _, o, r in pairs)
    det = sxx * syy - sxy * sxy
    if det == 0:
        raise ValueError("degenerate system; not enough variation in data")
    a = (syy * sxr - sxy * syr) / det
    b = (sxx * syr - sxy * sxr) / det
    return a, b


def _report(pairs: list[tuple[int, int, int]], a: float, b: float) -> None:
    def err_stats(predict) -> tuple[float, float]:
        ratios = [predict(c, o) / r for c, o, r in pairs if r]
        ratios.sort()
        mean = sum(ratios) / len(ratios)
        median = ratios[len(ratios) // 2]
        return mean, median

    fitted_mean, fitted_median = err_stats(lambda c, o: a * c + b * o)
    legacy_mean, legacy_median = err_stats(lambda c, o: (c + o) / 3)

    print(f"\nsamples: {len(pairs)}")
    print(f"fitted coefficients: cjk_tokens_per_char={a:.4f} other_tokens_per_char={b:.4f}")
    print("ratio = estimate / real  (1.0 = perfect, <1 = under-estimate)")
    print(f"  fitted : mean={fitted_mean:.3f}  median={fitted_median:.3f}")
    print(f"  len//3 : mean={legacy_mean:.3f}  median={legacy_median:.3f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "sessions.db"))
    args = ap.parse_args()
    pairs = _load_pairs(Path(args.db))
    if len(pairs) < 10:
        print(f"not enough calibration pairs ({len(pairs)}); need persisted llm_prompt "
              "artifacts with paired llm_output usage")
        return 1
    a, b = _fit_two_param(pairs)
    _report(pairs, a, b)
    print(
        "\nApply by setting in backend/agent/token_chars.py:\n"
        f"  CJK_TOKENS_PER_CHAR = {a:.3f}\n"
        f"  OTHER_TOKENS_PER_CHAR = {b:.3f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
