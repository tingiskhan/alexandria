"""Settings for ingest as a whole. Each source keeps its own settings class next to its code in `sources/`."""

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from docling.datamodel.base_models import FormatToExtensions, InputFormat
from pydantic import Field, PositiveInt, field_validator
from pydantic_settings import BaseSettings, NoDecode

from alexandria_core.models import DocType
from alexandria_core.settings import settings_config
from alexandria_ingest.boilerplate import DEFAULT_FRONT_MATTER_PATTERNS, BoilerplateRules, compile_patterns

TRANSCRIPT_FORMATS = frozenset({"vtt", "srt"})
"""Timestamped transcripts (WebVTT, SubRip), read by TranscriptExtractor instead of Docling."""

DEFAULT_FORMATS = frozenset({"pdf", "docx", "pptx", "xlsx", "html", "htm", "md", "txt"}) | TRANSCRIPT_FORMATS

# Extensions Docling lists under more than one format, resolved to the one a library of documents means.
_PREFERRED = {"txt": InputFormat.MD}  # not USPTO patent XML


def extension(path: str) -> str:
    """The file's extension, lower-case and without the dot, as ExtractSettings.formats lists it."""
    return PurePosixPath(path).suffix.lower().lstrip(".")


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


class BoilerplateSettings(BaseSettings):
    """What is stripped from pages before chunking (ALEXANDRIA_BOILERPLATE_*). Stored pages are kept whole.

    The pattern lists are JSON arrays of regexes in the environment, e.g. `["^Downloaded from ", "^Preprint"]`.
    All patterns match case-insensitively.
    """

    model_config = settings_config("ALEXANDRIA_BOILERPLATE_")

    # Lines matching any of these are dropped from every page, e.g. a publisher's header.
    patterns: list[str] = []
    # A line that appears on at least this fraction of a document's pages (digits ignored) is a running header.
    repeat_ratio: float = Field(default=0.6, gt=0, le=1)
    # Only look for repeated lines in documents with at least this many pages. A large value turns it off.
    repeat_min_pages: int = Field(default=4, ge=2)
    # The first page of a multi-page document is skipped when paragraphs matching these make up most of it.
    # An empty list turns it off.
    front_matter_patterns: list[str] = list(DEFAULT_FRONT_MATTER_PATTERNS)

    @field_validator("patterns", "front_matter_patterns")
    @classmethod
    def _compiles(cls, patterns: list[str]) -> list[str]:
        """Compile each pattern, so a typo fails at startup rather than during a sync."""
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"invalid regex {pattern!r}: {error}") from error
        return patterns

    def rules(self) -> BoilerplateRules:
        """The settings as the rules `strip_boilerplate` takes."""
        return BoilerplateRules(
            patterns=compile_patterns(self.patterns),
            repeat_ratio=self.repeat_ratio,
            repeat_min_pages=self.repeat_min_pages,
            front_matter_patterns=compile_patterns(self.front_matter_patterns),
        )


class ExtractSettings(BaseSettings):
    """Which files are extracted (ALEXANDRIA_EXTRACT_*)."""

    model_config = settings_config("ALEXANDRIA_EXTRACT_")

    # File extensions to ingest, comma-separated in the environment, e.g. "pdf,docx,png". Others are skipped.
    formats: Annotated[frozenset[str], NoDecode] = DEFAULT_FORMATS
    # How long each page of a transcript (vtt, srt) is: page N holds minutes (N-1)*block to N*block.
    transcript_block_minutes: PositiveInt = 5

    @field_validator("formats", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        """Split the comma-separated environment value."""
        return value.split(",") if isinstance(value, str) else value

    @field_validator("formats")
    @classmethod
    def _known(cls, formats: frozenset[str]) -> frozenset[str]:
        """Normalise " .DOCX" to "docx". Each must be a transcript or one Docling format, so a typo fails early."""
        formats = frozenset(f.strip().lower().lstrip(".") for f in formats) - {""}
        if not formats:
            raise ValueError("at least one format is required")
        for docling_format in sorted(formats - TRANSCRIPT_FORMATS):
            input_format(docling_format)
        return formats


DEFAULT_DOC_TYPES: dict[str, DocType] = {
    "/papers": "paper",
    "/books": "book",
    "/policies": "policy",
    "/transcripts": "transcript",
}


class MetadataSettings(BaseSettings):
    """How document metadata (title, authors, year, DOI, type) is found (ALEXANDRIA_METADATA_*)."""

    model_config = settings_config("ALEXANDRIA_METADATA_")

    # Look up DOIs found on a document's first page on Crossref (api.crossref.org), and prefer what it has.
    crossref: bool = True
    # An email address sent to Crossref with each lookup, which gets faster, more reliable service ("polite pool").
    crossref_mailto: str | None = None
    # The type of the documents in each folder, comma-separated in the environment, e.g.
    # "papers=paper,books=book,reports/policy=policy". A path below more than one folder takes the longest match.
    doc_types: Annotated[dict[str, DocType], NoDecode] = DEFAULT_DOC_TYPES

    @field_validator("doc_types", mode="before")
    @classmethod
    def _split_pairs(cls, value: object) -> object:
        """Split "folder=type,folder=type" from the environment."""
        if not isinstance(value, str):
            return value
        pairs = [pair.split("=", 1) for pair in value.split(",") if pair.strip()]
        if any(len(pair) != 2 for pair in pairs):
            raise ValueError("expected folder=type pairs, e.g. papers=paper,books=book")
        return {folder: doc_type.strip().lower() for folder, doc_type in pairs}

    @field_validator("doc_types")
    @classmethod
    def _normalise_folders(cls, doc_types: dict[str, DocType]) -> dict[str, DocType]:
        """Write every folder as "/a/b", lower-case, so "Papers/" and "/papers" are the same folder."""
        folders = {"/" + folder.strip().strip("/").lower(): doc_type for folder, doc_type in doc_types.items()}
        if "/" in folders:
            raise ValueError("a folder is required on the left of each '='")
        return folders


class SourceSettings(BaseSettings):
    """Which file source to sync from (ALEXANDRIA_SOURCE_*)."""

    model_config = settings_config("ALEXANDRIA_SOURCE_")

    kind: Literal["dropbox", "s3", "sharepoint"]  # which backend in alexandria_ingest.sources to sync from
