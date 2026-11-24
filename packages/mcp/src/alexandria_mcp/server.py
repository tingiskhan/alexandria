"""The MCP server and its tools. Each tool is a thin layer: validate, call the store, format as text."""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from alexandria_core.models import DocType, DocumentFilter, DocumentMetadata, DocumentSummary, SearchHit
from alexandria_core.protocols import DocumentStore, Embedder, Reranker
from alexandria_mcp.retrieval import RERANK_CANDIDATES, Retrieval

MAX_RESULTS = 50
MAX_PAGES = 20
MAX_LISTED = 500
MAX_OUTPUT_CHARS = 40_000
MAX_AUTHORS = 3  # more are cited as "First Author et al."

INSTRUCTIONS = """\
Alexandria searches a private library of documents (reports, papers, invoices, manuals, slides, notes).
Use `search` to find passages (optionally within a folder, document type or range of years), `get_pages`
to read the full pages around a hit, and `list_documents` to see what is in the library. Always cite the
document (title, and authors and year where shown) and page numbers in your answers. A transcript's pages
are blocks of time, each starting with its time range (`[00:15:00–00:20:00]`); cite that time range."""


def build_server(
    store: DocumentStore,
    embedder: Embedder,
    reranker: Reranker | None = None,
    rerank_candidates: int = RERANK_CANDIDATES,
) -> MCPServer:
    """Build the server with its three read-only tools. Tests pass fakes; `main` passes the real store.

    Args:
        store: Where documents are searched and read.
        embedder: Embeds search queries; must be the model that embedded the chunks.
        reranker: Reorders search candidates when given. None keeps the hybrid-search (RRF) order.
        rerank_candidates: How many hybrid-search hits the reranker scores, or the requested limit if larger.

    Returns:
        The server, ready to run.
    """
    retrieval = Retrieval(store, embedder, reranker, rerank_candidates)
    server: MCPServer = MCPServer(name="alexandria", instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only)
    def search(
        query: str,
        *,
        document_ids: list[int] | None = None,
        path_prefix: str | None = None,
        doc_type: DocType | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_RESULTS)] = 10,
    ) -> str:
        """Search the library for passages relevant to `query`. Returns ranked excerpts.

        Combines meaning-based search with exact keyword matching, so both questions ("how did revenue
        develop?") and exact terms (names, codes, numbers) work. Keyword syntax: "quoted phrase", -exclude.

        Pass `document_ids` (from `list_documents` or earlier results) to search only those documents,
        e.g. to compare two reports or to dig deeper into one. Leave it empty to search everything.

        To narrow the search without looking up ids first, pass any of: `path_prefix` (one folder,
        case-insensitive, e.g. "/reports/2025"), `doc_type` (paper, book, policy, transcript or other) and
        `year_from` / `year_to` (inclusive; documents with an unknown year are left out). All filters given
        must match, and `limit` is still filled when enough matching passages exist.

        Each result starts with a header like `[doc 12] Annual report 2025 — Jane Doe (2025) — pp. 41–42`, with
        authors and year where they are known. Excerpts are short;
        when you need surrounding context or exact wording, call `get_pages` with that document id and pages.
        Cite the title and page numbers in your answer.
        """
        filters = _document_filter(path_prefix, doc_type, year_from, year_to)
        hits = retrieval.search(query, limit=limit, document_ids=document_ids, filters=filters)
        if not hits:
            return "No matching passages."
        return _cap("\n\n".join(_format_hit(h) for h in hits))

    @server.tool(annotations=read_only)
    def list_documents(
        *,
        path_prefix: str | None = None,
        doc_type: DocType | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_LISTED)] = 200,
    ) -> str:
        """List the documents in the library: id, title, authors, year, type, pages, DOI, path and ingest date.

        The type is paper, book, policy, transcript or other. Authors, year and DOI are left out when unknown.
        A document with the same content as another ends in `DUPLICATE of doc N`: `search` skips it and returns
        doc N's passages instead, so search or cite doc N. `get_pages` still reads it.

        Use it to find document ids for `search` or `get_pages`, or to answer "which documents do we have
        about X?". `path_prefix` (case-insensitive, e.g. "/reports/2025") limits the list to one folder,
        `doc_type` to one type, and `year_from` / `year_to` (inclusive) to a range of years, leaving out
        documents whose year is unknown. Documents are sorted by path. When there are more than `limit`,
        the last line says which `offset` to pass for the next page.
        """
        documents = store.list_documents(_document_filter(path_prefix, doc_type, year_from, year_to))
        if not documents:
            return "No documents found."
        page = documents[offset : offset + limit]
        lines = [_format_summary(d) for d in page]
        if offset + len(page) < len(documents):
            lines.append(
                f"[Showing {offset + 1}–{offset + len(page)} of {len(documents)}. "
                f"Pass offset={offset + len(page)} for more.]"
            )
        return _cap("\n".join(lines))

    @server.tool(annotations=read_only)
    def get_pages(
        document_id: int,
        first_page: Annotated[int, Field(ge=1)],
        last_page: Annotated[int, Field(ge=1)],
    ) -> str:
        """Read the full text (Markdown, including tables) of pages `first_page` to `last_page` of a document.

        Use it after `search` to read the context around a hit, to quote exactly, or to check a table.
        At most 20 pages per call; page numbers are 1-based and match the ones in search results.
        Slides and spreadsheet sheets are pages; Word, HTML, Markdown and text files are a single page 1.
        A transcript's pages are consecutive blocks of time (5 minutes unless configured otherwise), each
        starting with its time range, e.g. `## [00:15:00–00:20:00]`; cite the time range.
        Cite the document title and page numbers when you use what you read.
        """
        if last_page < first_page:
            raise ToolError("last_page must be greater than or equal to first_page")
        if last_page - first_page + 1 > MAX_PAGES:
            raise ToolError(f"At most {MAX_PAGES} pages per call; request a smaller range")
        pages = store.get_pages(document_id, first_page, last_page)
        if not pages:
            return f"No pages {first_page}–{last_page} found for document {document_id}."
        return _cap("\n\n".join(f"[doc {document_id}] — p. {p.number}\n\n{p.markdown}" for p in pages))

    return server


def _document_filter(
    path_prefix: str | None, doc_type: DocType | None, year_from: int | None, year_to: int | None
) -> DocumentFilter:
    """Build the filter a tool's arguments describe; an empty `path_prefix` doesn't filter."""
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ToolError("year_from must be less than or equal to year_to")
    return DocumentFilter(path_prefix=path_prefix or None, doc_type=doc_type, year_from=year_from, year_to=year_to)


def _cite(metadata: DocumentMetadata) -> str:
    """Title, authors and year, as known: `Title — Ada Lovelace, Charles Babbage (1843)`."""
    authors = metadata.authors
    byline = f"{authors[0]} et al." if len(authors) > MAX_AUTHORS else ", ".join(authors)
    if metadata.year is not None:
        byline = f"{byline} ({metadata.year})" if byline else f"({metadata.year})"
    return f"{metadata.title} — {byline}" if byline else metadata.title


def _format_summary(document: DocumentSummary) -> str:
    doi = f" — doi:{document.metadata.doi}" if document.metadata.doi else ""
    duplicate = f" — DUPLICATE of doc {document.duplicate_of}" if document.duplicate_of is not None else ""
    return (
        f"[doc {document.id}] {_cite(document.metadata)} — {document.metadata.doc_type} — "
        f"{document.page_count} pages{doi} — {document.path} — ingested {document.ingested_at:%Y-%m-%d}{duplicate}"
    )


def _format_hit(hit: SearchHit) -> str:
    pages = f"p. {hit.page_start}" if hit.page_start == hit.page_end else f"pp. {hit.page_start}–{hit.page_end}"
    section = f"\nSection: {hit.section}" if hit.section else ""
    return f"[doc {hit.document_id}] {_cite(hit.metadata)} — {pages}\nPath: {hit.path}{section}\n\n{hit.text}"


def _cap(text: str) -> str:
    """Keep one response from flooding the agent's context."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n\n[Output truncated at {MAX_OUTPUT_CHARS} characters. Ask for less.]"
