from collections.abc import Sequence

import pytest

from alexandria_core.models import DocumentMetadata, SearchHit
from alexandria_core.rerank import rerank


class FakeReranker:
    """Scores each text from a canned table; records what it was asked."""

    model_name = "fake-reranker"

    def __init__(self, scores: dict[str, float], extra: int = 0) -> None:
        self.scores = scores
        self.extra = extra  # scores to return beyond one per text, to test the length check
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls.append((query, list(texts)))
        return [self.scores.get(t, 0.0) for t in texts] + [0.0] * self.extra


METADATA = DocumentMetadata(title="t", authors=("Jane Doe",), year=2020, doc_type="paper")


def hit(text: str, score: float) -> SearchHit:
    return SearchHit(
        document_id=1, metadata=METADATA, path="/t.pdf", page_start=1, page_end=1, section="", text=text, score=score
    )


def test_reorders_by_reranker_score_and_keeps_the_best() -> None:
    hits = [hit("a", 0.03), hit("b", 0.02), hit("c", 0.01)]
    reranker = FakeReranker({"a": 0.1, "b": 0.9, "c": 0.5})

    reranked = rerank("query", hits, reranker, limit=2)

    assert [(h.text, h.score) for h in reranked] == [("b", 0.9), ("c", 0.5)]
    assert all(h.metadata == METADATA for h in reranked)  # only the score changes
    assert reranker.calls == [("query", ["a", "b", "c"])]


def test_ties_keep_the_fused_order() -> None:
    hits = [hit("a", 0.03), hit("b", 0.02), hit("c", 0.01)]

    reranked = rerank("query", hits, FakeReranker({"c": 1.0}), limit=3)

    assert [h.text for h in reranked] == ["c", "a", "b"]


def test_no_hits_skip_the_reranker() -> None:
    reranker = FakeReranker({})

    assert rerank("query", [], reranker, limit=5) == []
    assert reranker.calls == []


def test_a_score_per_text_is_required() -> None:
    with pytest.raises(ValueError, match="fake-reranker returned 2 scores for 1 texts"):
        rerank("query", [hit("a", 0.03)], FakeReranker({}, extra=1), limit=1)
