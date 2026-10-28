import pytest

from alexandria_core.db import EMBEDDING_DIMENSION
from alexandria_core.models import Chunk, Document, EmbeddedChunk, Page, SearchQuery
from alexandria_core.store import PgStore

pytestmark = pytest.mark.integration

MODEL = "test-model"


def vector(index: int) -> list[float]:
    values = [0.0] * EMBEDDING_DIMENSION
    values[index] = 1.0
    return values


def add_doc(store: PgStore, source_id: str, chunks: list[tuple[str, int]], model: str = MODEL) -> int:
    """Store a document whose chunks are (text, one-hot vector index) pairs; return its id."""
    doc = Document(
        source_id=source_id,
        path=f"/papers/{source_id}.pdf",
        title=f"Title {source_id}",
        content_hash="h",
        pages=(Page(number=1, markdown="p1"),),
    )
    embedded = [
        EmbeddedChunk(
            chunk=Chunk(page_start=1, page_end=1, section="S", text=text),
            embedding=vector(index),
            model_name=model,
        )
        for text, index in chunks
    ]
    store.upsert_document(doc, embedded)
    return next(d.id for d in store.list_documents() if d.path == doc.path)


def query(text: str, index: int, document_ids: tuple[int, ...] | None = None, limit: int = 10) -> SearchQuery:
    return SearchQuery(text=text, embedding=vector(index), model_name=MODEL, document_ids=document_ids, limit=limit)


def test_hit_in_both_lists_ranks_first(store: PgStore) -> None:
    add_doc(store, "a", [("apples and pears", 0), ("bananas", 1), ("apples, more apples", 2)])

    hits = store.search(query("apples", 2))

    assert hits[0].text == "apples, more apples"
    assert hits[0].title == "Title a"
    assert hits[0].path == "/papers/a.pdf"
    assert hits[0].score > hits[-1].score


def test_exact_term_found_through_full_text(store: PgStore) -> None:
    add_doc(store, "a", [(f"filler chunk {i}", 1) for i in range(60)] + [("part number XK-4471 is discontinued", 500)])

    hits = store.search(query("XK-4471", 1))

    assert any("XK-4471" in h.text for h in hits)


def test_document_filter_returns_full_result_set(store: PgStore) -> None:
    add_doc(store, "big", [(f"near chunk {i}", 0) for i in range(80)])
    small = add_doc(store, "small", [(f"far chunk {i}", 3) for i in range(5)])

    hits = store.search(query("nothing matches", 0, document_ids=(small,)))

    assert len(hits) == 5
    assert {h.document_id for h in hits} == {small}


def test_websearch_syntax_excludes_terms(store: PgStore) -> None:
    add_doc(store, "a", [("alpha beta", 0), ("alpha gamma", 0)])

    hits = store.search(query("alpha -beta", 9))

    assert hits[0].text == "alpha gamma"


def test_other_models_are_never_returned(store: PgStore) -> None:
    add_doc(store, "a", [("same text", 0)])
    add_doc(store, "b", [("same text", 0)], model="other-model")

    hits = store.search(query("same text", 0))

    assert [h.path for h in hits] == ["/papers/a.pdf"]


def test_limit(store: PgStore) -> None:
    add_doc(store, "a", [(f"chunk {i}", 0) for i in range(20)])

    assert len(store.search(query("chunk", 0, limit=7))) == 7
