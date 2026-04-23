import pytest

from memory.embedding_provider import (
    CachedEmbeddingProvider,
    NullEmbeddingProvider,
    cosine_similarity,
)


class StaticEmbeddingProvider:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        return self.vectors


def test_cosine_similarity_scores_identical_and_orthogonal_vectors() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0


def test_null_embedding_provider_returns_empty_vectors() -> None:
    assert NullEmbeddingProvider().embed(["京都住宿"]) == [[]]


def test_cached_embedding_provider_rejects_vector_count_mismatch() -> None:
    provider = CachedEmbeddingProvider(StaticEmbeddingProvider([[1.0, 0.0]]))

    with pytest.raises(ValueError, match="embedding_count_mismatch"):
        provider.embed(["first", "second"])


def test_cached_embedding_provider_rejects_invalid_max_items() -> None:
    with pytest.raises(ValueError, match="max_items must be >= 1"):
        CachedEmbeddingProvider(StaticEmbeddingProvider([]), max_items=0)


def test_cached_embedding_provider_returns_current_batch_when_cache_evicted() -> None:
    provider = CachedEmbeddingProvider(
        StaticEmbeddingProvider([[1.0, 0.0], [0.0, 1.0]]),
        max_items=1,
    )

    assert provider.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
