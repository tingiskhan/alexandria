from dataclasses import replace

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from alexandria_core.db import EMBEDDING_DIMENSION, ChunkRow, DocumentRow, PageRow
from alexandria_core.models import (
    Chunk,
    Document,
    DocumentFilter,
    DocumentMetadata,
    DocumentSummary,
    EmbeddedChunk,
    FileChange,
    Page,
)
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
        source_id=source_id,
        path=f"/papers/{source_id}.pdf",
        metadata=DocumentMetadata(title=source_id),
        content_hash=content_hash,
        pages=pages,
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

    assert sorted(d.metadata.title for d in store.list_documents()) == ["/papers2/c.pdf", "/papers_x/d.pdf"]


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


PAPER = DocumentMetadata(
    title="Widgets Revisited", authors=("Jane Doe", "John Roe"), year=2021, doi="10.1000/w", doc_type="paper"
)


def test_metadata_roundtrip_and_update(store: PgStore) -> None:
    store.upsert_document(replace(make_doc(), metadata=PAPER), make_chunks(["a"]))
    [summary] = store.list_documents()
    assert summary.metadata == PAPER

    store.update_metadata(summary.id, DocumentMetadata(title="Widgets"))

    [updated] = store.list_documents()
    assert updated.metadata == DocumentMetadata(title="Widgets")
    assert updated.page_count == summary.page_count
    assert len(store.get_pages(summary.id, 1, 3)) == 3


def test_failed_document_is_titled_after_its_file(store: PgStore, engine: Engine) -> None:
    store.mark_failed(FileChange(source_id="x", path="/papers/Report 7.pdf", content_hash="h"), "boom")

    with Session(engine) as session:
        row = session.scalars(select(DocumentRow)).one()
    assert (row.title, row.authors, row.doc_type) == ("Report 7", [], "other")


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (None, ["/Reports/100%/a.pdf", "/Reports/2025/b.pdf", "/reports/2025/c.pdf"]),
        (DocumentFilter(), ["/Reports/100%/a.pdf", "/Reports/2025/b.pdf", "/reports/2025/c.pdf"]),
        (DocumentFilter(path_prefix="/REPORTS/2025"), ["/Reports/2025/b.pdf", "/reports/2025/c.pdf"]),
        (DocumentFilter(path_prefix="/reports/100%"), ["/Reports/100%/a.pdf"]),
        (DocumentFilter(path_prefix="/reports/1_0"), []),
        (DocumentFilter(doc_type="paper"), ["/Reports/2025/b.pdf"]),
        (DocumentFilter(year_from=2021), ["/Reports/2025/b.pdf"]),
        (DocumentFilter(path_prefix="/reports/2025", year_to=2020), []),
    ],
)
def test_list_documents_filters_in_sql(store: PgStore, filters: DocumentFilter | None, expected: list[str]) -> None:
    for source_id, path, metadata in [
        ("a", "/Reports/100%/a.pdf", DocumentMetadata(title="a", year=2019)),
        ("b", "/Reports/2025/b.pdf", PAPER),
        ("c", "/reports/2025/c.pdf", DocumentMetadata(title="c")),
    ]:
        store.upsert_document(replace(make_doc(source_id), path=path, metadata=metadata), [])
    store.mark_failed(FileChange(source_id="f", path="/Reports/2025/failed.pdf", content_hash="h"), "boom")

    assert sorted(d.path for d in store.list_documents(filters)) == sorted(expected)  # path order is collation-specific


def copy_of(doc: Document, source_id: str, content_hash: str = "other") -> Document:
    return replace(doc, source_id=source_id, path=f"/copies/{source_id}.pdf", content_hash=content_hash)


def by_path(store: PgStore) -> dict[str, DocumentSummary]:
    return {d.path: d for d in store.list_documents()}


def test_same_content_under_another_path_is_stored_as_a_duplicate(store: PgStore, engine: Engine) -> None:
    original = make_doc("a")
    store.upsert_document(original, make_chunks(["a"]))
    # Whitespace doesn't count, e.g. when a re-exported copy wraps lines differently.
    copy = replace(copy_of(original, "b"), pages=tuple(replace(p, markdown=f" {p.markdown}\n") for p in original.pages))
    original_id = by_path(store)["/papers/a.pdf"].id

    assert store.original_of(copy) == original_id
    assert store.original_of(original) is None  # a document is not its own duplicate
    store.upsert_document(copy, make_chunks(["b"]))

    documents = by_path(store)
    assert documents["/copies/b.pdf"].duplicate_of == original_id
    assert documents["/copies/b.pdf"].content_digest == documents["/papers/a.pdf"].content_digest
    assert documents["/papers/a.pdf"].duplicate_of is None
    assert count(engine, ChunkRow) == 1  # the duplicate's chunks are dropped
    assert len(store.get_pages(documents["/copies/b.pdf"].id, 1, 3)) == 3  # its pages can still be read
    assert store.content_hash("b") == "other"  # so an unchanged duplicate isn't processed again


def test_documents_without_text_are_never_duplicates(store: PgStore) -> None:
    empty = make_doc("a", page_count=0)
    store.upsert_document(empty, [])

    assert store.original_of(copy_of(empty, "b")) is None


def test_deleting_the_original_releases_its_duplicates_for_the_next_sync(store: PgStore, engine: Engine) -> None:
    original = make_doc("a")
    store.upsert_document(original, make_chunks(["a"]))
    store.upsert_document(copy_of(original, "b", "hb"), [])

    store.delete_document("a")

    assert store.list_documents() == []
    assert store.failed_documents() == [FileChange(source_id="b", path="/copies/b.pdf", content_hash="hb")]
    assert store.content_hash("b") is None  # so the retry isn't skipped as unchanged
    assert count(engine, PageRow) == 0

    store.upsert_document(copy_of(original, "b", "hb"), make_chunks(["b"]))  # the retry
    [promoted] = store.list_documents()
    assert promoted.duplicate_of is None
    assert count(engine, ChunkRow) == 1


def test_deleting_a_folder_with_original_and_duplicate_releases_nothing(store: PgStore) -> None:
    original = make_doc("/f/a")
    store.upsert_document(original, make_chunks(["a"]))
    store.upsert_document(replace(original, source_id="/f/b"), [])

    store.delete_document("/f")

    assert store.list_documents() == []
    assert store.failed_documents() == []


def test_deleting_a_duplicate_leaves_the_original_alone(store: PgStore) -> None:
    original = make_doc("a")
    store.upsert_document(original, make_chunks(["a"]))
    store.upsert_document(copy_of(original, "b"), [])

    store.delete_document("b")

    [kept] = store.list_documents()
    assert kept.path == "/papers/a.pdf"
    assert store.failed_documents() == []


def test_changing_the_original_releases_its_duplicates(store: PgStore) -> None:
    original = make_doc("a")
    store.upsert_document(original, make_chunks(["a"]))
    store.upsert_document(copy_of(original, "b"), [])

    store.upsert_document(make_doc("a", content_hash="h2", page_count=2), make_chunks(["a"]))

    assert [d.path for d in store.list_documents()] == ["/papers/a.pdf"]
    assert [c.source_id for c in store.failed_documents()] == ["b"]


def test_reingesting_the_original_unchanged_keeps_its_duplicates(store: PgStore) -> None:
    original = make_doc("a")
    store.upsert_document(original, make_chunks(["a"]))
    store.upsert_document(copy_of(original, "b"), [])

    store.upsert_document(replace(original, content_hash="resaved"), make_chunks(["a"]))  # same text

    assert by_path(store)["/copies/b.pdf"].duplicate_of == by_path(store)["/papers/a.pdf"].id
    assert store.failed_documents() == []


def test_a_failing_original_releases_its_duplicates(store: PgStore) -> None:
    original = make_doc("a")
    store.upsert_document(original, make_chunks(["a"]))
    store.upsert_document(copy_of(original, "b"), [])

    store.mark_failed(FileChange(source_id="a", path="/papers/a.pdf", content_hash="h2"), "boom")

    assert sorted(c.source_id for c in store.failed_documents()) == ["a", "b"]


def test_a_failed_document_is_not_an_original(store: PgStore) -> None:
    original = make_doc("a")
    store.upsert_document(original, make_chunks(["a"]))
    store.mark_failed(FileChange(source_id="a", path="/papers/a.pdf", content_hash="h2"), "boom")

    assert store.original_of(copy_of(original, "b")) is None


def test_a_duplicate_is_not_an_original(store: PgStore) -> None:
    original = make_doc("a")
    store.upsert_document(original, make_chunks(["a"]))
    store.upsert_document(copy_of(original, "b"), [])
    original_id = by_path(store)["/papers/a.pdf"].id

    store.upsert_document(copy_of(original, "c"), [])

    assert by_path(store)["/copies/c.pdf"].duplicate_of == original_id


def test_update_content_digest_changes_only_the_digest(store: PgStore) -> None:
    store.upsert_document(make_doc("a"), make_chunks(["a"]))
    [before] = store.list_documents()

    store.update_content_digest(before.id, None)

    [after] = store.list_documents()
    assert after == replace(before, content_digest=None)


def test_reupserting_copies_from_before_digests_makes_one_a_duplicate(store: PgStore, engine: Engine) -> None:
    original = make_doc("a")
    store.upsert_document(original, make_chunks(["a"]))
    store.upsert_document(copy_of(original, "b"), make_chunks(["b"]))
    for summary in store.list_documents():  # as if both were ingested by an older release
        store.update_content_digest(summary.id, None)
    with Session(engine) as session, session.begin():
        session.execute(update(DocumentRow).values(duplicate_of=None))
    store.upsert_document(copy_of(original, "b"), make_chunks(["b"]))  # e.g. by the reindex flow

    store.upsert_document(original, make_chunks(["a"]))

    documents = by_path(store)
    assert documents["/copies/b.pdf"].duplicate_of is None
    assert documents["/papers/a.pdf"].duplicate_of == documents["/copies/b.pdf"].id
    assert count(engine, ChunkRow) == 1
