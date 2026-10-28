"""SentenceTransformerEmbedder's own logic, with a fake model. test_embedder.py covers the real model."""

from typing import Any

import numpy as np
import pytest

from alexandria_embed import embedder as module
from alexandria_embed.embedder import SentenceTransformerEmbedder


class FakeModel:
    """Records how the embedder calls sentence-transformers."""

    dimension: int | None = 3

    def __init__(self, name: str, *, revision: str, device: str | None) -> None:
        self.loaded = (name, revision, device)
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    def get_embedding_dimension(self) -> int | None:
        return self.dimension

    def encode_document(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        self.calls.append(("document", texts, kwargs))
        return np.ones((len(texts), 3), dtype=np.float32)

    def encode_query(self, text: str, **kwargs: Any) -> np.ndarray:
        self.calls.append(("query", text, kwargs))
        return np.ones(3, dtype=np.float32)


@pytest.fixture(autouse=True)
def fake_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "SentenceTransformer", FakeModel)


def model_of(embedder: SentenceTransformerEmbedder) -> FakeModel:
    model = embedder._model
    assert isinstance(model, FakeModel)
    return model


def test_loads_the_pinned_revision_and_reports_its_dimension() -> None:
    embedder = SentenceTransformerEmbedder("some/model", "abc123", device="cpu")

    assert model_of(embedder).loaded == ("some/model", "abc123", "cpu")
    assert embedder.model_name == "some/model"
    assert embedder.dimension == 3


def test_model_without_a_dimension_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FakeModel, "dimension", None)

    with pytest.raises(ValueError, match="does not report an embedding dimension"):
        SentenceTransformerEmbedder("some/model", "abc123")


def test_documents_are_batched_normalised_and_returned_as_floats() -> None:
    embedder = SentenceTransformerEmbedder("some/model", "abc123", batch_size=8)

    vectors = embedder.embed_documents(("a", "b"))

    assert vectors == [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0]]
    assert all(type(x) is float for x in vectors[0])
    [(kind, texts, kwargs)] = model_of(embedder).calls
    assert (kind, texts) == ("document", ["a", "b"])
    assert kwargs["batch_size"] == 8
    assert kwargs["normalize_embeddings"] is True


def test_no_documents_skips_the_model() -> None:
    embedder = SentenceTransformerEmbedder("some/model", "abc123")

    assert embedder.embed_documents([]) == []
    assert model_of(embedder).calls == []


def test_query_uses_the_query_encoder() -> None:
    embedder = SentenceTransformerEmbedder("some/model", "abc123")

    assert embedder.embed_query("revenue") == [1.0, 1.0, 1.0]
    [(kind, text, kwargs)] = model_of(embedder).calls
    assert (kind, text, kwargs["normalize_embeddings"]) == ("query", "revenue", True)
