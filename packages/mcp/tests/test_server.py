from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from alexandria_core.models import DocumentFilter, DocumentMetadata, DocumentSummary, Page, SearchHit
from alexandria_mcp.server import MAX_OUTPUT_CHARS, build_server

from .fakes import FakeEmbedder, FakeReranker, FakeStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def hit(document_id: int = 12, page_start: int = 41, page_end: int = 42, text: str = "Revenue rose 12%.") -> SearchHit:
    return SearchHit(
        document_id=document_id,
        metadata=DocumentMetadata(title="Annual report 2025"),
        path="/Reports/Annual report 2025.pdf",
        page_start=page_start,
        page_end=page_end,
        section="Results > Revenue",
        text=text,
        score=0.03,
    )


def summary(document_id: int, path: str) -> DocumentSummary:
    return DocumentSummary(
        id=document_id,
        source_id=path.lower(),
        content_hash="h",
        metadata=DocumentMetadata(title=path.rsplit("/", 1)[-1]),
        path=path,
        page_count=3,
        ingested_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


async def call(store: FakeStore, tool: str, **arguments: Any) -> CallToolResult:
    result = await build_server(store, FakeEmbedder()).call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    return result


def text_of(result: CallToolResult) -> str:
    [content] = result.content
    assert isinstance(content, TextContent)
    return content.text


async def test_search_formats_hits_with_citation_header() -> None:
    store = FakeStore(hits=[hit(), hit(document_id=3, page_start=7, page_end=7, text="Costs fell.")])

    text = text_of(await call(store, "search", query="revenue"))

    assert text.startswith("[doc 12] Annual report 2025 — pp. 41–42\n")
    assert "Section: Results > Revenue" in text
    assert "Revenue rose 12%." in text
    assert "[doc 3] Annual report 2025 — p. 7\n" in text


async def test_search_passes_query_filter_and_limit_to_store() -> None:
    store = FakeStore()

    text = text_of(await call(store, "search", query="revenue", document_ids=[3, 4], limit=5))

    assert text == "No matching passages."
    [query] = store.queries
    assert (query.text, query.document_ids, query.limit, query.model_name) == ("revenue", (3, 4), 5, "fake-embedder")
    assert query.embedding == FakeEmbedder().embed_query("revenue")


async def test_search_without_filter_searches_everything() -> None:
    store = FakeStore()

    await call(store, "search", query="x", document_ids=[])

    assert store.queries[0].document_ids is None


@pytest.mark.parametrize("limit", [0, 51])
async def test_search_limit_is_bounded(limit: int) -> None:
    store = FakeStore()

    with pytest.raises(ToolError, match="limit"):
        await call(store, "search", query="x", limit=limit)

    assert store.queries == []


async def test_without_a_reranker_the_store_order_and_limit_are_kept() -> None:
    store = FakeStore(hits=[hit(text=f"chunk {n}") for n in range(5)])

    text = text_of(await call(store, "search", query="chunk", limit=3))

    assert store.queries[0].limit == 3
    assert [line for line in text.splitlines() if line.startswith("chunk")] == ["chunk 0", "chunk 1", "chunk 2"]


async def test_reranker_reorders_more_candidates_and_returns_the_limit() -> None:
    store = FakeStore(hits=[hit(text="revenue"), hit(text="costs"), hit(text="revenue revenue"), hit(text="x")])
    reranker = FakeReranker()
    server = build_server(store, FakeEmbedder(), reranker, rerank_candidates=4)

    result = await server.call_tool("search", {"query": "revenue", "limit": 2})

    assert isinstance(result, CallToolResult)
    assert store.queries[0].limit == 4
    assert reranker.calls == [["revenue", "costs", "revenue revenue", "x"]]
    lines = text_of(result).splitlines()
    assert [line for line in lines if line.startswith("revenue")] == ["revenue revenue", "revenue"]


async def test_reranker_fetches_at_least_the_limit() -> None:
    store = FakeStore()
    server = build_server(store, FakeEmbedder(), FakeReranker(), rerank_candidates=5)

    await server.call_tool("search", {"query": "x", "limit": 20})

    assert store.queries[0].limit == 20


async def test_reranker_candidates_are_fetched_with_the_filters() -> None:
    store = FakeStore()
    server = build_server(store, FakeEmbedder(), FakeReranker(), rerank_candidates=30)

    await server.call_tool("search", {"query": "x", "limit": 5, "path_prefix": "/Reports", "doc_type": "paper"})

    [query] = store.queries
    assert query.limit == 30
    assert query.filters == DocumentFilter(path_prefix="/Reports", doc_type="paper")


async def test_output_is_capped() -> None:
    store = FakeStore(hits=[hit(text="x" * 30_000), hit(text="y" * 30_000)])

    text = text_of(await call(store, "search", query="x"))

    assert len(text) < MAX_OUTPUT_CHARS + 200
    assert text.endswith("Ask for less.]")


async def test_search_passes_document_filters_to_store() -> None:
    store = FakeStore()

    await call(store, "search", query="x", path_prefix="/Reports", doc_type="paper", year_from=2019, year_to=2021)

    [query] = store.queries
    assert query.filters == DocumentFilter(path_prefix="/Reports", doc_type="paper", year_from=2019, year_to=2021)


async def test_search_without_filters_passes_an_empty_filter() -> None:
    store = FakeStore()

    await call(store, "search", query="x", path_prefix="")

    assert store.queries[0].filters == DocumentFilter()


@pytest.mark.parametrize("tool", ["search", "list_documents"])
@pytest.mark.parametrize(
    ("arguments", "message"),
    [({"doc_type": "invoice"}, "doc_type"), ({"year_from": 2021, "year_to": 2020}, "year_from must be less")],
)
async def test_bad_document_filters_are_rejected(tool: str, arguments: dict[str, Any], message: str) -> None:
    store = FakeStore()
    if tool == "search":
        arguments = {"query": "x", **arguments}

    with pytest.raises(ToolError, match=message):
        await call(store, tool, **arguments)

    assert store.queries == []
    assert store.listed == []


async def test_list_documents_passes_filters_to_store() -> None:
    store = FakeStore(documents=[summary(1, "/Reports/a.pdf")])

    text = text_of(
        await call(store, "list_documents", path_prefix="/reports", doc_type="book", year_from=2000, year_to=2010)
    )

    assert text == "[doc 1] a.pdf — other — 3 pages — /Reports/a.pdf — ingested 2026-10-01"
    assert store.listed == [DocumentFilter(path_prefix="/reports", doc_type="book", year_from=2000, year_to=2010)]


async def test_list_documents_pages_through_long_lists() -> None:
    store = FakeStore(documents=[summary(n, f"/{n}.pdf") for n in range(1, 6)])

    first = text_of(await call(store, "list_documents", limit=2))
    last = text_of(await call(store, "list_documents", offset=4, limit=2))

    assert first.splitlines()[-1] == "[Showing 1–2 of 5. Pass offset=2 for more.]"
    assert last == "[doc 5] 5.pdf — other — 3 pages — /5.pdf — ingested 2026-10-01"


async def test_list_documents_says_when_nothing_matches() -> None:
    store = FakeStore()

    assert text_of(await call(store, "list_documents", path_prefix="/invoices")) == "No documents found."
    assert store.listed == [DocumentFilter(path_prefix="/invoices")]


async def test_get_pages_says_when_pages_are_missing() -> None:
    text = text_of(await call(FakeStore(), "get_pages", document_id=7, first_page=3, last_page=4))

    assert text == "No pages 3–4 found for document 7."


async def test_get_pages_returns_full_pages_with_headers() -> None:
    store = FakeStore(pages={12: [Page(n, f"Page {n} text") for n in range(1, 6)]})

    text = text_of(await call(store, "get_pages", document_id=12, first_page=2, last_page=3))

    assert text == "[doc 12] — p. 2\n\nPage 2 text\n\n[doc 12] — p. 3\n\nPage 3 text"


@pytest.mark.parametrize(
    ("first", "last", "message"),
    [(1, 21, "At most 20 pages"), (5, 4, "greater than or equal to first_page"), (0, 3, "first_page")],
)
async def test_get_pages_rejects_bad_ranges(first: int, last: int, message: str) -> None:
    with pytest.raises(ToolError, match=message):
        await call(FakeStore(), "get_pages", document_id=12, first_page=first, last_page=last)


async def test_tools_are_registered_read_only() -> None:
    tools = await build_server(FakeStore(), FakeEmbedder()).list_tools()

    assert {t.name for t in tools} == {"search", "list_documents", "get_pages"}
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


def paper(authors: tuple[str, ...] = (), year: int | None = None) -> DocumentMetadata:
    return DocumentMetadata(
        title="Attention is all you need", authors=authors, year=year, doi="10.5555/attention", doc_type="paper"
    )


async def test_list_documents_shows_metadata() -> None:
    metadata = paper(authors=("Ashish Vaswani", "Noam Shazeer"), year=2017)
    store = FakeStore(documents=[replace(summary(4, "/Papers/vaswani.pdf"), metadata=metadata)])

    text = text_of(await call(store, "list_documents"))

    assert text == (
        "[doc 4] Attention is all you need — Ashish Vaswani, Noam Shazeer (2017) — paper — 3 pages"
        " — doi:10.5555/attention — /Papers/vaswani.pdf — ingested 2026-10-01"
    )


@pytest.mark.parametrize(
    ("metadata", "header"),
    [
        (paper(authors=("A B", "C D", "E F", "G H"), year=2017), "Attention is all you need — A B et al. (2017)"),
        (paper(authors=("A B", "C D", "E F")), "Attention is all you need — A B, C D, E F"),
        (paper(year=2017), "Attention is all you need — (2017)"),
        (paper(), "Attention is all you need"),
    ],
)
async def test_search_header_cites_authors_and_year(metadata: DocumentMetadata, header: str) -> None:
    store = FakeStore(hits=[replace(hit(), metadata=metadata)])

    text = text_of(await call(store, "search", query="attention"))

    assert text.startswith(f"[doc 12] {header} — pp. 41–42\n")


async def test_list_documents_marks_duplicates() -> None:
    original = summary(4, "/Papers/vaswani.pdf")
    store = FakeStore(documents=[original, replace(summary(9, "/Copies/vaswani.pdf"), duplicate_of=4)])

    lines = text_of(await call(store, "list_documents")).splitlines()

    assert not lines[0].endswith("DUPLICATE of doc 4")
    assert lines[1] == (
        "[doc 9] vaswani.pdf — other — 3 pages — /Copies/vaswani.pdf — ingested 2026-10-01 — DUPLICATE of doc 4"
    )
