"""Settings for ingest as a whole. Each source keeps its own settings class next to its code in `sources/`."""

from typing import Literal

from pydantic_settings import BaseSettings

from alexandria_core.settings import settings_config


class ChunkSettings(BaseSettings):
    """How documents are cut into chunks (ALEXANDRIA_CHUNK_*)."""

    model_config = settings_config("ALEXANDRIA_CHUNK_")

    max_tokens: int = 500  # upper bound per chunk; BGE-M3 handles far more, but smaller chunks retrieve sharper


class SourceSettings(BaseSettings):
    """Which file source to sync from (ALEXANDRIA_SOURCE_*)."""

    model_config = settings_config("ALEXANDRIA_SOURCE_")

    kind: Literal["dropbox", "s3"]  # which backend in alexandria_ingest.sources to sync from
