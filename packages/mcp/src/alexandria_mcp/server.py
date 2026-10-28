"""The MCP server and its tools. Each tool is a thin layer: validate, call the store, format as text."""

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from alexandria_core.models import SearchHit, SearchQuery
from alexandria_core.protocols import DocumentStore, Embedder

MAX_RESULTS = 50
MAX_PAGES = 20
MAX_LISTED = 500
MAX_OUTPUT_CHARS = 40_000

INSTRUCTIONS = """\
Alexandria searches a private library of PDF documents (reports, papers, invoices, manuals).
Use `search` to find passages, `get_pages` to read the full pages around a hit, and `list_documents`
to see what is in the library. Always cite the document title and page numbers in your answers."""


def build_server(store: DocumentStore, embedder: Embedder) -> MCPServer:
    """Build the server with its three read-only tools. Tests pass fakes; `main` passes the real store."""
    server: MCPServer = MCPServer(name="alexandria", instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only)
    def search(
        query: str,
        document_ids: list[int] | None = None,
        limit: Annotated[int, Field(ge=1, le=MAX_RESULTS)] = 10,
    ) -> str:
        """Search the PDF library for passages relevant to `query`. Returns ranked excerpts.

        Combines meaning-based search with exact keyword matching, so both questions ("how did revenue
        develop?") and exact terms (names, codes, numbers) work. Keyword syntax: "quoted phrase", -exclude.

        Pass `document_ids` (from `list_documents` or earlier results) to search only those documents,
        e.g. to compare two reports or to dig deeper into one. Leave it empty to search everything.

        Each result starts with a header like `[doc 12] Annual report 2025 — pp. 41–42`. Excerpts are short;
        when you need surrounding context or exact wording, call `get_pages` with that document id and pages.
        Cite the title and page numbers in your answer.
        """
        hits = store.search(
            SearchQuery(
                text=query,
                embedding=embedder.embed_query(query),
                model_name=embedder.model_name,
                document_ids=tuple(document_ids) if document_ids else None,
                limit=limit,
            )
        )
        if not hits:
            return "No matching passages."
        return _cap("\n\n".join(_format_hit(h) for h in hits))

    @server.tool(annotations=read_only)
    def list_documents(
        path_prefix: str | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_LISTED)] = 200,
    ) -> str:
        """List the documents in the library: id, title, page count, path and when it was ingested.

        Use it to find document ids for `search` or `get_pages`, or to answer "which documents do we have
        about X?". `path_prefix` (case-insensitive, e.g. "/reports/2025") limits the list to one folder.
        Documents are sorted by path. When there are more than `limit`, the last line says which `offset`
        to pass for the next page.
        """
        documents = store.list_documents()
        if path_prefix:
            documents = [d for d in documents if d.path.lower().startswith(path_prefix.lower())]
        if not documents:
            return "No documents found."
        page = documents[offset : offset + limit]
        lines = [
            f"[doc {d.id}] {d.title} — {d.page_count} pages — {d.path} — ingested {d.ingested_at:%Y-%m-%d}"
            for d in page
        ]
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


def _format_hit(hit: SearchHit) -> str:
    pages = f"p. {hit.page_start}" if hit.page_start == hit.page_end else f"pp. {hit.page_start}–{hit.page_end}"
    section = f"\nSection: {hit.section}" if hit.section else ""
    return f"[doc {hit.document_id}] {hit.title} — {pages}\nPath: {hit.path}{section}\n\n{hit.text}"


def _cap(text: str) -> str:
    """Keep one response from flooding the agent's context."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n\n[Output truncated at {MAX_OUTPUT_CHARS} characters. Ask for less.]"
