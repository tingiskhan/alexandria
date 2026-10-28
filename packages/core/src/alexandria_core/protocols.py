"""The interfaces between packages. Code depends on these, never on a concrete adapter, so tests can pass fakes."""

from collections.abc import Sequence
from typing import Protocol

from alexandria_core.models import (
    Document,
    DocumentSummary,
    EmbeddedChunk,
    FileChange,
    Page,
    SearchHit,
    SearchQuery,
)


class FileSource(Protocol):
    """Where PDFs come from: reports what changed and serves file contents."""

    def changes(self, cursor: str | None) -> tuple[list[FileChange], str]:
        """Return the files changed since `cursor` (everything when it is None) and the cursor for next time.

        Must report every deletion and give each file a hash that changes when its content does.
        """
        ...

    def download(self, file_id: str) -> bytes:
        """Return the content of the file whose `FileChange.source_id` is `file_id`."""
        ...


class Extractor(Protocol):
    """Turns a PDF into per-page Markdown."""

    def extract(self, pdf: bytes) -> list[Page]:
        """Return the PDF's pages in order, numbered from 1."""
        ...


class Embedder(Protocol):
    """Turns text into normalised vectors of a fixed dimension."""

    model_name: str
    dimension: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed passages for storage, one vector per text."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query. May differ from `embed_documents` for models with a query prompt."""
        ...


class DocumentStore(Protocol):
    """Stores documents, pages, chunks and sync state, and searches them."""

    def upsert_document(self, doc: Document, chunks: Sequence[EmbeddedChunk]) -> None:
        """Insert or replace a document with its pages and chunks, atomically, and mark it `ok`."""
        ...

    def delete_document(self, source_id: str) -> None:
        """Delete a document, or every document below `source_id` when it is a deleted folder."""
        ...

    def content_hash(self, source_id: str) -> str | None:
        """Return the stored hash of a successfully ingested document, or None if there is none."""
        ...

    def search(self, query: SearchQuery) -> list[SearchHit]:
        """Return the best-matching chunks, best first."""
        ...

    def get_pages(self, document_id: int, first: int, last: int) -> list[Page]:
        """Return the stored pages `first` to `last` (inclusive) of a document; missing pages are left out."""
        ...

    def list_documents(self) -> list[DocumentSummary]:
        """Return every successfully ingested document, sorted by path."""
        ...

    def get_cursor(self, source: str) -> str | None:
        """Return the cursor saved for `source`, or None before its first sync."""
        ...

    def save_cursor(self, source: str, cursor: str) -> None:
        """Save the cursor that `source`'s next sync starts from."""
        ...

    def mark_failed(self, change: FileChange, error: str) -> None:
        """Record that a file failed to ingest, and remove any older pages and chunks of it."""
        ...

    def failed_documents(self) -> list[FileChange]:
        """Return the files recorded as failed, so a sync can retry them."""
        ...
