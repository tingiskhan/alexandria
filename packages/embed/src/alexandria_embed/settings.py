"""Embedding settings, shared by ingest and the MCP server."""

from pydantic_settings import BaseSettings

from alexandria_core.settings import settings_config


class EmbeddingSettings(BaseSettings):
    """Ingest and the MCP server must use the same model and revision."""

    model_config = settings_config("ALEXANDRIA_EMBEDDING_")

    model: str = "BAAI/bge-m3"
    revision: str = "5617a9f61b028005a4858fdac845db406aefb181"
    batch_size: int = 32
