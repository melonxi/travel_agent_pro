from memory.embedding_provider import NullEmbeddingProvider, cosine_similarity


def test_cosine_similarity_scores_identical_and_orthogonal_vectors() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0


def test_null_embedding_provider_returns_empty_vectors() -> None:
    assert NullEmbeddingProvider().embed(["京都住宿"]) == [[]]
