"""Settings for ingest as a whole. Each source keeps its own settings class next to its code in `sources/`."""

from typing import Annotated, Literal

from docling.datamodel.base_models import FormatToExtensions, InputFormat
from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode

from alexandria_core.settings import settings_config

DEFAULT_FORMATS = frozenset({"pdf", "docx", "pptx", "xlsx", "html", "htm", "md", "txt"})

# Extensions Docling lists under more than one format, resolved to the one a library of documents means.
_PREFERRED = {"txt": InputFormat.MD}  # not USPTO patent XML


def input_format(extension: str) -> InputFormat:
    """The Docling format that reads files with `extension` (lower-case, no dot). Raises ValueError if unclear."""
    if extension in _PREFERRED:
        return _PREFERRED[extension]
    matches = [f for f, extensions in FormatToExtensions.items() if extension in extensions]
    if not matches:
        raise ValueError(f"Docling cannot read {extension!r} files")
    if len(matches) > 1:
        raise ValueError(f"{extension!r} is ambiguous in Docling ({', '.join(sorted(f.value for f in matches))})")
    return matches[0]


class ChunkSettings(BaseSettings):
    """How documents are cut into chunks (ALEXANDRIA_CHUNK_*)."""

    model_config = settings_config("ALEXANDRIA_CHUNK_")

    max_tokens: int = 500  # upper bound per chunk; BGE-M3 handles far more, but smaller chunks retrieve sharper


class ExtractSettings(BaseSettings):
    """Which files are extracted (ALEXANDRIA_EXTRACT_*)."""

    model_config = settings_config("ALEXANDRIA_EXTRACT_")

    # File extensions to ingest, comma-separated in the environment, e.g. "pdf,docx,png". Others are skipped.
    formats: Annotated[frozenset[str], NoDecode] = DEFAULT_FORMATS

    @field_validator("formats", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        """Split the comma-separated environment value."""
        return value.split(",") if isinstance(value, str) else value

    @field_validator("formats")
    @classmethod
    def _known(cls, formats: frozenset[str]) -> frozenset[str]:
        """Normalise " .DOCX" to "docx". Each must belong to one Docling input format, so a typo fails at startup."""
        formats = frozenset(f.strip().lower().lstrip(".") for f in formats) - {""}
        if not formats:
            raise ValueError("at least one format is required")
        for extension in sorted(formats):
            input_format(extension)
        return formats


class SourceSettings(BaseSettings):
    """Which file source to sync from (ALEXANDRIA_SOURCE_*)."""

    model_config = settings_config("ALEXANDRIA_SOURCE_")

    kind: Literal["dropbox", "s3"]  # which backend in alexandria_ingest.sources to sync from
