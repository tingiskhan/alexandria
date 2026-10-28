import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alexandria_core.db import EMBEDDING_DIMENSION, ChunkRow, PageRow
from alexandria_core.models import Chunk, Document, EmbeddedChunk, FileChange, Page
from alexandria_core.store import PgStore

pytestmark = pytest.mark.integration

MODEL = "test-model"


def vector(index: int) -> list[float]:
    values = [0.0] * EMBEDDING_DIMENSION
    values[index] = 1.0
    return values


def make_doc(source_id: str = "id:1", content_hash: str = "h1", page_count: int = 3) -> Document:
    pages = tuple(Page(number=n, markdown=f"page {n} of {source_id}") for n in range(1, page_count + 1))
    return Document(
        source_id=source_id, path=f"/papers/{source_id}.pdf", title=source_id, content_hash=content_hash, pages=pages
    )


def make_chunks(texts: list[str]) -> list[EmbeddedChunk]:
    return [
        EmbeddedChunk(
            chunk=Chunk(page_start=1, page_end=1, section="Intro", text=t), embedding=vector(i), model_name=MODEL
        )
        for i, t in enumerate(texts)
    ]


def count(engine: Engine, row: type[ChunkRow] | type[PageRow]) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(row)) or 0


def test_upsert_replaces_pages_and_chunks(store: PgStore, engine: Engine) -> None:
    store.upsert_document(make_doc(page_count=3), make_chunks(["a", "b", "c"]))
    store.upsert_document(make_doc(content_hash="h2", page_count=2), make_chunks(["d"]))

    assert count(engine, PageRow) == 2
    assert count(engine, ChunkRow) == 1
    assert store.content_hash("id:1") == "h2"
    [summary] = store.list_documents()
    assert summary.page_count == 2


def test_delete_cascades(store: PgStore, engine: Engine) -> None:
    store.upsert_document(make_doc(), make_chunks(["a", "b"]))
    store.delete_document("id:1")

    assert store.list_documents() == []
    assert count(engine, PageRow) == 0
    assert count(engine, ChunkRow) == 0


def test_delete_folder_removes_documents_below_it(store: PgStore) -> None:
    for source_id in ["/papers/a.pdf", "/papers/sub/b.pdf", "/papers2/c.pdf", "/papers_x/d.pdf"]:
        store.upsert_document(make_doc(source_id=source_id), [])

    store.delete_document("/papers")

    assert sorted(d.title for d in store.list_documents()) == ["/papers2/c.pdf", "/papers_x/d.pdf"]


def test_get_pages_returns_inclusive_range_in_order(store: PgStore) -> None:
    store.upsert_document(make_doc(page_count=5), [])
    [summary] = store.list_documents()

    pages = store.get_pages(summary.id, 2, 4)

    assert [p.number for p in pages] == [2, 3, 4]
    assert pages[0].markdown == "page 2 of id:1"


def test_failed_document_is_hidden_and_retryable(store: PgStore, engine: Engine) -> None:
    store.upsert_document(make_doc(), make_chunks(["a"]))
    change = FileChange(source_id="id:1", path="/papers/id:1.pdf", content_hash="h2")

    store.mark_failed(change, "boom")

    assert store.content_hash("id:1") is None
    assert store.list_documents() == []
    assert count(engine, ChunkRow) == 0
    assert store.failed_documents() == [change]

    store.upsert_document(make_doc(content_hash="h2"), make_chunks(["a"]))
    assert store.failed_documents() == []


def test_cursor_roundtrip(store: PgStore) -> None:
    assert store.get_cursor("dropbox:/") is None
    store.save_cursor("dropbox:/", "c1")
    store.save_cursor("dropbox:/", "c2")
    assert store.get_cursor("dropbox:/") == "c2"
