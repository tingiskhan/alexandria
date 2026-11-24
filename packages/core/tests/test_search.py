import pytest
from sqlalchemy import Engine, update

from alexandria_core.db import EMBEDDING_DIMENSION, DocumentRow
from alexandria_core.models import Chunk, Document, DocumentFilter, DocumentMetadata, EmbeddedChunk, Page, SearchQuery
from alexandria_core.store import PgStore

pytestmark = pytest.mark.integration

MODEL = "test-model"


def vector(index: int) -> list[float]:
    values = [0.0] * EMBEDDING_DIMENSION
    values[index] = 1.0
    return values


def add_doc(
    store: PgStore,
    source_id: str,
    chunks: list[tuple[str, int]],
    *,
    model: str = MODEL,
    path: str | None = None,
    metadata: DocumentMetadata | None = None,
) -> int:
    """Store a document whose chunks are (text, one-hot vector index) pairs; return its id."""
    doc = Document(
        source_id=source_id,
        path=path or f"/papers/{source_id}.pdf",
        metadata=metadata or DocumentMetadata(title=f"Title {source_id}"),
        content_hash="h",
        pages=(Page(number=1, markdown=f"p1 of {source_id}"),),
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


def query(
    text: str,
    index: int,
    document_ids: tuple[int, ...] | None = None,
    limit: int = 10,
    filters: DocumentFilter | None = None,
) -> SearchQuery:
    return SearchQuery(
        text=text,
        embedding=vector(index),
        model_name=MODEL,
        document_ids=document_ids,
        filters=filters or DocumentFilter(),
        limit=limit,
    )


def found_paths(store: PgStore, filters: DocumentFilter) -> set[str]:
    return {h.path for h in store.search(query("chunk", 0, filters=filters, limit=50))}


def test_hit_in_both_lists_ranks_first(store: PgStore) -> None:
    add_doc(store, "a", [("apples and pears", 0), ("bananas", 1), ("apples, more apples", 2)])

    hits = store.search(query("apples", 2))

    assert hits[0].text == "apples, more apples"
    assert hits[0].metadata.title == "Title a"
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


def test_hits_carry_the_document_metadata(store: PgStore) -> None:
    metadata = DocumentMetadata(title="Widgets", authors=("Jane Doe",), year=2021, doi="10.1000/w", doc_type="paper")
    document_id = add_doc(store, "a", [("widgets work", 0)])
    store.update_metadata(document_id, metadata)

    [hit] = store.search(query("widgets", 0))

    assert hit.metadata == metadata


def test_path_prefix_fills_limit_with_matching_hits(store: PgStore) -> None:
    # 80 chunks outside the folder beat every chunk inside it on both vector distance and full text, so filtering
    # after ranking would leave nothing; filtering inside the candidate lists still fills the limit.
    add_doc(store, "big", [(f"apples chunk {i}", 0) for i in range(80)], path="/Other/big.pdf")
    inside = add_doc(store, "inside", [(f"pears {i}", 3) for i in range(15)], path="/Reports/2025/inside.pdf")

    hits = store.search(query("apples", 0, limit=10, filters=DocumentFilter(path_prefix="/reports/2025")))

    assert len(hits) == 10
    assert {h.document_id for h in hits} == {inside}


PATHS = ["/a_b/x.pdf", "/aXb/x.pdf", "/100%/y.pdf", "/1000/y.pdf", r"/back\slash/z.pdf", "/backXslash/z.pdf"]


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("/A_B/", {"/a_b/x.pdf"}),  # "_" is literal, and case doesn't matter
        ("/100%", {"/100%/y.pdf"}),  # "%" is literal
        (r"/back\slash", {r"/back\slash/z.pdf"}),  # so is the escape character
        ("/", set(PATHS)),
    ],
)
def test_path_prefix_is_case_insensitive_and_literal(store: PgStore, prefix: str, expected: set[str]) -> None:
    for n, path in enumerate(PATHS):
        add_doc(store, str(n), [("chunk", 0)], path=path)

    assert found_paths(store, DocumentFilter(path_prefix=prefix)) == expected


def test_doc_type_filter_reads_unknown_type_as_other(store: PgStore, engine: Engine) -> None:
    add_doc(store, "paper", [("chunk", 0)], metadata=DocumentMetadata(title="p", doc_type="paper"))
    add_doc(store, "book", [("chunk", 0)], metadata=DocumentMetadata(title="b", doc_type="book"))
    legacy = add_doc(store, "legacy", [("chunk", 0)])
    with engine.begin() as conn:  # a row from before the doc_type column existed
        conn.execute(update(DocumentRow).where(DocumentRow.id == legacy).values(doc_type=None))

    assert found_paths(store, DocumentFilter(doc_type="paper")) == {"/papers/paper.pdf"}
    assert found_paths(store, DocumentFilter(doc_type="other")) == {"/papers/legacy.pdf"}
    assert found_paths(store, DocumentFilter(doc_type="transcript")) == set()


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (DocumentFilter(year_from=2020), {"/2020", "/2021"}),
        (DocumentFilter(year_to=2020), {"/2019", "/2020"}),
        (DocumentFilter(year_from=2020, year_to=2020), {"/2020"}),
        (DocumentFilter(), {"/2019", "/2020", "/2021", "/unknown"}),
    ],
)
def test_year_range_is_inclusive_and_leaves_out_unknown_years(
    store: PgStore, filters: DocumentFilter, expected: set[str]
) -> None:
    for year in (2019, 2020, 2021, None):
        name = str(year) if year else "unknown"
        add_doc(store, name, [("chunk", 0)], path=f"/{name}", metadata=DocumentMetadata(title=name, year=year))

    assert found_paths(store, filters) == expected


def test_filters_combine_with_each_other_and_with_document_ids(store: PgStore) -> None:
    paper = DocumentMetadata(title="p", doc_type="paper", year=2020)
    first = add_doc(store, "a", [("chunk", 0)], path="/r/a.pdf", metadata=paper)
    add_doc(store, "b", [("chunk", 0)], path="/r/b.pdf", metadata=paper)
    add_doc(store, "c", [("chunk", 0)], path="/r/c.pdf", metadata=DocumentMetadata(title="c", year=2020))
    add_doc(store, "d", [("chunk", 0)], path="/x/d.pdf", metadata=paper)
    filters = DocumentFilter(path_prefix="/r/", doc_type="paper", year_from=2019, year_to=2021)

    assert found_paths(store, filters) == {"/r/a.pdf", "/r/b.pdf"}
    assert {h.path for h in store.search(query("chunk", 0, document_ids=(first,), filters=filters))} == {"/r/a.pdf"}


def test_duplicates_are_not_searched(store: PgStore) -> None:
    pages = (Page(number=1, markdown="The same paper"),)
    original = Document("a", "/a.pdf", DocumentMetadata(title="A"), "h1", pages)
    copy = Document("b", "/copies/a.pdf", DocumentMetadata(title="A"), "h2", pages)
    chunks = [EmbeddedChunk(Chunk(1, 1, "", "the same paper"), vector(0), MODEL)]
    store.upsert_document(original, chunks)
    store.upsert_document(copy, chunks)
    duplicate = next(d.id for d in store.list_documents() if d.path == "/copies/a.pdf")

    assert [h.path for h in store.search(query("same paper", 0))] == ["/a.pdf"]
    assert store.search(query("same paper", 0, document_ids=(duplicate,))) == []
