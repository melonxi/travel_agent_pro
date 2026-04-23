from __future__ import annotations

import math
from collections import OrderedDict
from pathlib import Path
from typing import Protocol


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector for each input text."""


class NullEmbeddingProvider:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


class CachedEmbeddingProvider:
    def __init__(self, provider: EmbeddingProvider, max_items: int = 10000) -> None:
        self._provider = provider
        self._max_items = max_items
        self._cache: OrderedDict[str, list[float]] = OrderedDict()

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing: list[str] = []
        seen_missing: set[str] = set()
        for text in texts:
            if text in self._cache:
                self._cache.move_to_end(text)
            elif text not in seen_missing:
                missing.append(text)
                seen_missing.add(text)

        if missing:
            vectors = self._provider.embed(missing)
            for text, vector in zip(missing, vectors):
                self._cache[text] = vector
                self._cache.move_to_end(text)
                while len(self._cache) > self._max_items:
                    self._cache.popitem(last=False)

        return [self._cache[text] for text in texts]


class FastEmbedProvider:
    def __init__(
        self,
        model_name: str,
        cache_dir: str,
        local_files_only: bool,
    ) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=str(Path(cache_dir)),
            local_files_only=local_files_only,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [list(vector) for vector in self._model.embed(texts)]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0

    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0

    dot = sum(left_value * right_value for left_value, right_value in zip(left, right))
    return dot / (left_norm * right_norm)
