"""CrossEncoderReranker's own logic, with a fake model. test_reranker.py covers a real model."""

from typing import Any

import numpy as np
import pytest

from alexandria_embed import reranker as module
from alexandria_embed.reranker import CrossEncoderReranker
from alexandria_embed.settings import RerankSettings


class FakeCrossEncoder:
    """Records how the reranker calls sentence-transformers; scores a pair by the passage's length."""

    def __init__(self, name: str, *, revision: str | None, device: str | None) -> None:
        self.loaded = (name, revision, device)
        self.calls: list[tuple[list[tuple[str, str]], dict[str, Any]]] = []

    def predict(self, pairs: list[tuple[str, str]], **kwargs: Any) -> np.ndarray:
        self.calls.append((pairs, kwargs))
        return np.array([len(passage) for _, passage in pairs], dtype=np.float32)


@pytest.fixture(autouse=True)
def fake_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "CrossEncoder", FakeCrossEncoder)


def model_of(reranker: CrossEncoderReranker) -> FakeCrossEncoder:
    model = reranker._model
    assert isinstance(model, FakeCrossEncoder)
    return model


def test_loads_the_model_where_asked() -> None:
    reranker = CrossEncoderReranker("some/reranker", "abc123", device="cpu")

    assert model_of(reranker).loaded == ("some/reranker", "abc123", "cpu")
    assert reranker.model_name == "some/reranker"


def test_scores_query_passage_pairs_as_floats() -> None:
    reranker = CrossEncoderReranker("some/reranker", batch_size=4)

    scores = reranker.score("revenue", ("ab", "abcd"))

    assert scores == [2.0, 4.0]
    assert all(type(s) is float for s in scores)
    [(pairs, kwargs)] = model_of(reranker).calls
    assert pairs == [("revenue", "ab"), ("revenue", "abcd")]
    assert kwargs["batch_size"] == 4


def test_no_texts_skip_the_model() -> None:
    reranker = CrossEncoderReranker("some/reranker")

    assert reranker.score("revenue", []) == []
    assert model_of(reranker).calls == []


def test_reranking_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALEXANDRIA_RERANK_MODEL", raising=False)

    settings = RerankSettings(_env_file=None)

    assert not settings.enabled
    assert (settings.revision, settings.candidates) == (None, 50)


def test_setting_a_model_turns_reranking_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    monkeypatch.setenv("ALEXANDRIA_RERANK_CANDIDATES", "30")

    settings = RerankSettings(_env_file=None)

    assert settings.enabled
    assert (settings.model, settings.candidates) == ("BAAI/bge-reranker-v2-m3", 30)
