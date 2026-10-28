"""Domain types passed between packages. Plain frozen dataclasses; the ORM rows stay inside `db` and `store`."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FileChange:
    """A file that was added, changed or deleted in a source since the last sync."""

    source_id: str  # stable id within the source (S3 key, Dropbox path_lower); the document's key in the store
    path: str  # display path, starting with "/"; its stem becomes the document title
    content_hash: str | None  # the source's hash or ETag; None for deletions
    deleted: bool = False


@dataclass(frozen=True)
class Page:
    """One page of extracted Markdown. Pages are numbered from 1."""

    number: int
    markdown: str


@dataclass(frozen=True)
class Document:
    """A source file with its extracted pages, as stored by `DocumentStore.upsert_document`."""

    source_id: str
    path: str
    title: str
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
class SearchQuery:
    """A hybrid search: `text` drives full-text search, `embedding` vector search."""

    text: str
    embedding: list[float]
    model_name: str  # only chunks embedded by this model are searched
    document_ids: tuple[int, ...] | None = None  # None searches every document
    limit: int = 10


@dataclass(frozen=True)
class SearchHit:
    """One ranked chunk, with what an agent needs to cite it."""

    document_id: int
    title: str
    path: str
    page_start: int
    page_end: int
    section: str
    text: str
    score: float  # Reciprocal Rank Fusion score; only meaningful for ordering


@dataclass(frozen=True)
class DocumentSummary:
    """A stored document without its pages or chunks."""

    id: int
    source_id: str
    content_hash: str
    title: str
    path: str
    page_count: int
    ingested_at: datetime
