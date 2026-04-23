#!/usr/bin/env python
from __future__ import annotations

import argparse
import math
import time
from pathlib import Path

from fastembed import TextEmbedding


DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_CACHE_DIR = "backend/data/embedding_cache"
DEFAULT_TEXTS = (
    "这次京都住宿想安静一点，最好别离地铁太远。",
    "我喜欢清静的旅馆，交通要方便。",
    "这次机票不要红眼航班。",
    "上次东京住在新宿，吃饭和交通都方便。",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the Stage 3 local embedding runtime."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "--local-files-only",
        action="store_true",
        help="Use only files already present in the FastEmbed cache.",
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    load_start = time.perf_counter()
    model = TextEmbedding(
        model_name=args.model,
        cache_dir=str(cache_dir),
        local_files_only=args.local_files_only,
    )
    load_ms = (time.perf_counter() - load_start) * 1000

    embed_start = time.perf_counter()
    vectors = [list(vector) for vector in model.embed(list(DEFAULT_TEXTS))]
    batch_embed_ms = (time.perf_counter() - embed_start) * 1000

    warm_start = time.perf_counter()
    warm_vectors = [list(vector) for vector in model.embed(list(DEFAULT_TEXTS))]
    warm_batch_embed_ms = (time.perf_counter() - warm_start) * 1000

    dimension = len(vectors[0]) if vectors else 0
    print(f"model={args.model}")
    print(f"cache_dir={cache_dir.resolve()}")
    print(f"local_files_only={args.local_files_only}")
    print(f"load_ms={load_ms:.1f}")
    print(f"batch_size={len(DEFAULT_TEXTS)}")
    print(f"batch_embed_ms={batch_embed_ms:.1f}")
    print(f"warm_batch_embed_ms={warm_batch_embed_ms:.1f}")
    print(f"vector_count={len(vectors)}")
    print(f"dim={dimension}")
    print(f"same_dim_warm={bool(warm_vectors and len(warm_vectors[0]) == dimension)}")
    print(f"sim_lodging_synonym={_cosine(vectors[0], vectors[1]):.4f}")
    print(f"sim_lodging_vs_flight={_cosine(vectors[0], vectors[2]):.4f}")
    print(f"sim_lodging_vs_tokyo_stay={_cosine(vectors[0], vectors[3]):.4f}")

    if len(vectors) != len(DEFAULT_TEXTS):
        raise SystemExit("unexpected vector count")
    if dimension != 512:
        raise SystemExit(f"unexpected embedding dimension: {dimension}")
    if not warm_vectors or len(warm_vectors[0]) != dimension:
        raise SystemExit("warm embedding dimension mismatch")
    return 0


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


if __name__ == "__main__":
    raise SystemExit(main())
