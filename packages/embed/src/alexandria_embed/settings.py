"""Embedding settings, shared by ingest and the MCP server, and the MCP server's reranking settings."""

from pydantic import Field
from pydantic_settings import BaseSettings

from alexandria_core.settings import settings_config


class EmbeddingSettings(BaseSettings):
    """Ingest and the MCP server must use the same model and revision."""

    model_config = settings_config("ALEXANDRIA_EMBEDDING_")

    model: str = "BAAI/bge-m3"
    revision: str = "5617a9f61b028005a4858fdac845db406aefb181"
    batch_size: int = 32


class RerankSettings(BaseSettings):
    """Optional cross-encoder reranking of search results (ALEXANDRIA_RERANK_*). Off unless `model` is set."""

    model_config = settings_config("ALEXANDRIA_RERANK_")

    model: str = ""  # e.g. BAAI/bge-reranker-v2-m3; empty turns reranking off
    revision: str | None = None  # a commit to pin; None means the model's default branch
    candidates: int = Field(default=50, ge=1)  # hybrid-search hits to rerank; at least the requested limit is used

    @property
    def enabled(self) -> bool:
        """Whether a reranking model is configured."""
        return bool(self.model.strip())
