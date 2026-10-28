"""The `alexandria-mcp` command: the MCP server's composition root."""

from enum import StrEnum
from typing import Annotated

import typer

from alexandria_core.db import EMBEDDING_DIMENSION, make_engine
from alexandria_core.settings import DatabaseSettings
from alexandria_core.store import PgStore
from alexandria_embed.embedder import SentenceTransformerEmbedder
from alexandria_embed.settings import EmbeddingSettings
from alexandria_mcp.server import build_server


class Transport(StrEnum):
    """How MCP clients connect."""

    STDIO = "stdio"
    HTTP = "streamable-http"


def main(transport: Annotated[Transport, typer.Option(help="How clients connect.")] = Transport.STDIO) -> None:
    """Run the read-only Alexandria MCP server."""
    database, embedding = DatabaseSettings(), EmbeddingSettings()
    # Queries are short, so the CPU is fast enough and the GPU stays free for ingest.
    embedder = SentenceTransformerEmbedder(embedding.model, embedding.revision, device="cpu")
    if embedder.dimension != EMBEDDING_DIMENSION:
        raise SystemExit(
            f"{embedder.model_name} has {embedder.dimension} dimensions; the schema has {EMBEDDING_DIMENSION}"
        )
    server = build_server(PgStore(make_engine(database.url.get_secret_value())), embedder)
    server.run(transport=transport.value)


def run() -> None:
    """Entry point for the `alexandria-mcp` script."""
    typer.run(main)
