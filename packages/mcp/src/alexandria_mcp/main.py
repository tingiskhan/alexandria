"""The `alexandria-mcp` command: the MCP server's composition root."""

from enum import StrEnum
from typing import Annotated

import typer

from alexandria_core.db import EMBEDDING_DIMENSION, make_engine, wait_for_database
from alexandria_core.migrate import migrate
from alexandria_core.settings import DatabaseSettings
from alexandria_core.store import PgStore
from alexandria_embed.embedder import SentenceTransformerEmbedder
from alexandria_embed.reranker import CrossEncoderReranker
from alexandria_embed.settings import EmbeddingSettings, RerankSettings
from alexandria_mcp.retrieval import Retrieval
from alexandria_mcp.server import build_server
from alexandria_mcp.settings import McpSettings


class Transport(StrEnum):
    """How MCP clients connect."""

    STDIO = "stdio"
    HTTP = "streamable-http"


def build_retrieval() -> Retrieval:
    """Build the real store, query embedder and (when configured) reranker; shared with `alexandria-eval`.

    Waits for the database and migrates it to the newest schema, so both commands can query a database that no
    sync has touched since an upgrade.

    Raises:
        SystemExit: If the embedding model doesn't fit the schema.
    """
    database, embedding, rerank = DatabaseSettings(), EmbeddingSettings(), RerankSettings()
    # Queries are short, so the CPU is fast enough and the GPU stays free for ingest.
    embedder = SentenceTransformerEmbedder(embedding.model, embedding.revision, device="cpu")
    if embedder.dimension != EMBEDDING_DIMENSION:
        raise SystemExit(
            f"{embedder.model_name} has {embedder.dimension} dimensions; the schema has {EMBEDDING_DIMENSION}"
        )
    # The reranker runs on the same device as the embedder.
    reranker = CrossEncoderReranker(rerank.model, rerank.revision, device="cpu") if rerank.enabled else None
    engine = make_engine(database.url.get_secret_value())
    wait_for_database(engine)
    # Both commands otherwise only read, but after an upgrade they must not wait for a sync to migrate.
    migrate(engine)
    return Retrieval(PgStore(engine), embedder, reranker, rerank.candidates)


def main(transport: Annotated[Transport, typer.Option(help="How clients connect.")] = Transport.STDIO) -> None:
    """Run the read-only Alexandria MCP server."""
    mcp = McpSettings()
    retrieval = build_retrieval()
    server = build_server(retrieval.store, retrieval.embedder, retrieval.reranker, retrieval.rerank_candidates)
    if transport is Transport.HTTP:
        server.run(transport=transport.value, host=mcp.host, port=mcp.port, transport_security=mcp.transport_security())
    else:
        server.run(transport=transport.value)


def run() -> None:
    """Entry point for the `alexandria-mcp` script."""
    typer.run(main)
