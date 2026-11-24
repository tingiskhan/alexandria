"""Domain types passed between packages. Plain frozen dataclasses; the ORM rows stay inside `db` and `store`."""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

DocType = Literal["paper", "book", "policy", "transcript", "other"]
"""What kind of document it is. Set per folder (`ALEXANDRIA_METADATA_DOC_TYPES`); "other" when nothing says."""

DOC_TYPES: tuple[DocType, ...] = get_args(DocType)


@dataclass(frozen=True)
class FileChange:
    """A file that was added, changed or deleted in a source since the last sync."""

    source_id: str  # stable id within the source (S3 key, Dropbox path_lower); the document's key in the store
    path: str  # display path, starting with "/"; its stem is the title when the document doesn't name one
    content_hash: str | None  # the source's hash or ETag; None for deletions
    deleted: bool = False


@dataclass(frozen=True)
class Page:
    """One page of extracted Markdown. Pages are numbered from 1."""

    number: int
    markdown: str


def content_digest(pages: Iterable[Page]) -> str | None:
    """A SHA-256 of the pages' text with all whitespace collapsed, to find the same document under another path.

    Computed from the extracted text rather than the file's bytes: a re-saved or re-exported copy whose bytes differ
    still matches, and stored documents get a digest from their stored pages without downloading anything. The
    source's `content_hash` can't be used: it is the source's own hash (an S3 ETag, a Dropbox content hash), which
    differs between sources and, for S3 multipart uploads, between copies of the same bytes.

    Returns:
        The hex digest, or None for a document without text, which is never treated as a duplicate.
    """
    text = " ".join(word for page in pages for word in page.markdown.split())
    return hashlib.sha256(text.encode()).hexdigest() if text else None


@dataclass(frozen=True)
class DocumentMetadata:
    """What a document is: its bibliographic data, for listing and citing it.

    Only `title` is always known (the file stem when the document names none); the rest is None or empty when
    it could not be found.
    """

    title: str
    authors: tuple[str, ...] = ()  # as printed, e.g. ("Ada Lovelace", "Charles Babbage")
    year: int | None = None
    doi: str | None = None  # bare, e.g. "10.1000/xyz123", without a https://doi.org/ prefix
    doc_type: DocType = "other"


@dataclass(frozen=True)
class Document:
    """A source file with its extracted pages, as stored by `DocumentStore.upsert_document`."""

    source_id: str
    path: str
    metadata: DocumentMetadata
    content_hash: str
    pages: tuple[Page, ...]


@dataclass(frozen=True)
class Chunk:
    """A piece of a document small enough to embed, with the pages it spans and its heading path."""

    page_start: int
    page_end: int
    section: str  # heading path, e.g. "Results > Revenue"; "" before the first heading
    text: str


@dataclass(frozen=True)
class EmbeddedChunk:
    """A chunk with its vector and the model that made it. Search only compares vectors from the same model."""

    chunk: Chunk
    embedding: list[float]
    model_name: str


@dataclass(frozen=True)
class DocumentFilter:
    """Which documents to search or list, by folder, type and year. Fields left as None don't filter.

    Every set field must match. A year filter leaves out documents whose year is unknown; documents whose type is
    unknown count as "other".

    Raises:
        ValueError: if `doc_type` is not one of `DOC_TYPES`, or `year_from` is after `year_to`.
    """

    path_prefix: str | None = None  # case-insensitive prefix of the path, e.g. "/reports/2025"
    doc_type: DocType | None = None
    year_from: int | None = None  # inclusive
    year_to: int | None = None  # inclusive

    def __post_init__(self) -> None:
        """Reject a type that isn't a `DocType` and an empty year range."""
        if self.doc_type is not None and self.doc_type not in DOC_TYPES:
            raise ValueError(f"doc_type must be one of {', '.join(DOC_TYPES)}, not {self.doc_type!r}")
        if self.year_from is not None and self.year_to is not None and self.year_from > self.year_to:
            raise ValueError(f"year_from ({self.year_from}) is after year_to ({self.year_to})")


@dataclass(frozen=True)
class SearchQuery:
    """A hybrid search: `text` drives full-text search, `embedding` vector search."""

    text: str
    embedding: list[float]
    model_name: str  # only chunks embedded by this model are searched
    document_ids: tuple[int, ...] | None = None  # None searches every document
    filters: DocumentFilter = DocumentFilter()  # applied together with document_ids
    limit: int = 10


@dataclass(frozen=True)
class SearchHit:
    """One ranked chunk, with what an agent needs to cite it."""

    document_id: int
    metadata: DocumentMetadata
    path: str
    page_start: int
    page_end: int
    section: str
    text: str
    score: float  # Reciprocal Rank Fusion score, or the reranker's when reranking; only meaningful for ordering


@dataclass(frozen=True)
class DocumentSummary:
    """A stored document without its pages or chunks."""

    id: int
    source_id: str
    content_hash: str
    metadata: DocumentMetadata
    path: str
    page_count: int
    ingested_at: datetime
    content_digest: str | None = None  # see `content_digest`; None until computed for documents from older releases
    duplicate_of: int | None = None  # the id of the searchable document with the same content; this one isn't searched
