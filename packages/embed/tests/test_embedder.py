import math

import pytest

from alexandria_core.db import EMBEDDING_DIMENSION
from alexandria_embed.embedder import SentenceTransformerEmbedder
from alexandria_embed.settings import EmbeddingSettings

pytestmark = pytest.mark.gpu


@pytest.fixture(scope="module")
def embedder() -> SentenceTransformerEmbedder:
    settings = EmbeddingSettings()
    return SentenceTransformerEmbedder(settings.model, settings.revision, device="cuda")


def test_dimension_matches_schema(embedder: SentenceTransformerEmbedder) -> None:
    assert embedder.dimension == EMBEDDING_DIMENSION
    assert len(embedder.embed_query("hello")) == EMBEDDING_DIMENSION


def test_vectors_are_normalised(embedder: SentenceTransformerEmbedder) -> None:
    vectors = [*embedder.embed_documents(["Revenue rose 12%", "Kostnaderna minskade"]), embedder.embed_query("revenue")]

    assert all(math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-4) for v in vectors)


def test_related_text_is_closer(embedder: SentenceTransformerEmbedder) -> None:
    query = embedder.embed_query("How much did revenue grow?")
    near, far = embedder.embed_documents(["Revenue rose 12% in 2025.", "The cat sat on the mat."])

    assert sum(q * n for q, n in zip(query, near, strict=True)) > sum(q * f for q, f in zip(query, far, strict=True))
