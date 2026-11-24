"""The interfaces between packages. Code depends on these, never on a concrete adapter, so tests can pass fakes."""

from collections.abc import Sequence
from typing import Protocol

from alexandria_core.models import (
    Document,
    DocumentFilter,
    DocumentMetadata,
    DocumentSummary,
    EmbeddedChunk,
    FileChange,
    Page,
    SearchHit,
    SearchQuery,
)


class FileSource(Protocol):
    """Where documents come from: reports what changed and serves file contents."""

    def changes(self, cursor: str | None) -> tuple[list[FileChange], str]:
        """Return the files changed since `cursor` (everything when it is None) and the cursor for next time.

        Must report every deletion and give each file a hash that changes when its content does.
        """
        ...

    def download(self, file_id: str) -> bytes:
        """Return the content of the file whose `FileChange.source_id` is `file_id`."""
        ...


class Extractor(Protocol):
    """Turns a document (PDF, Word, HTML, …) into per-page Markdown."""

    def extract(self, content: bytes, name: str) -> list[Page]:
        """Return the document's pages in order, numbered from 1. `name` is the file name, which picks the format.

        A format without pages (Word, HTML, Markdown) comes back as a single page 1.
        """
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


class BibliographyLookup(Protocol):
    """Looks up a document's bibliographic data by DOI, e.g. on Crossref."""

    def lookup(self, doi: str) -> DocumentMetadata | None:
        """Return what is registered for `doi`, or None when it is unknown or the lookup failed.

        Best effort: a failure must not stop a document from being ingested, so it returns None rather than raise.
        """
        ...


class Reranker(Protocol):
    """Scores how well each passage answers a query, e.g. a cross-encoder. Higher is better."""

    model_name: str

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """Return one relevance score per text, in the order given. Scores only need to be comparable per call."""
        ...


class DocumentStore(Protocol):
    """Stores documents, pages, chunks and sync state, and searches them."""

    def upsert_document(self, doc: Document, chunks: Sequence[EmbeddedChunk]) -> None:
        """Insert or replace a document with its pages and chunks, atomically, and mark it `ok`.

        When `original_of(doc)` finds another document with the same content, `doc` is stored as its duplicate:
        with its pages but without chunks, so search skips it. Duplicates of `doc` itself whose content no longer
        matches it are released (see `delete_document`).
        """
        ...

    def original_of(self, doc: Document) -> int | None:
        """Return the id of the searchable document with the same content (`content_digest`) as `doc`, or None.

        Documents with another `source_id` count, as do only those that are `ok` and not duplicates themselves.
        """
        ...

    def update_content_digest(self, document_id: int, digest: str | None) -> None:
        """Store a document's content digest, e.g. for one ingested before digests existed. Changes nothing else."""
        ...

    def update_metadata(self, document_id: int, metadata: DocumentMetadata) -> None:
        """Replace a stored document's metadata, leaving its pages and chunks alone."""
        ...

    def delete_document(self, source_id: str) -> None:
        """Delete a document, or every document below `source_id` when it is a deleted folder.

        Duplicates of a deleted document are released: recorded as failed, so the next sync ingests them again and
        one of them becomes searchable.
        """
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

    def list_documents(self, filters: DocumentFilter | None = None) -> list[DocumentSummary]:
        """Return the successfully ingested documents that match `filters` (all of them when None), sorted by path.

        Duplicates are included.
        """
        ...

    def get_cursor(self, source: str) -> str | None:
        """Return the cursor saved for `source`, or None before its first sync."""
        ...

    def save_cursor(self, source: str, cursor: str) -> None:
        """Save the cursor that `source`'s next sync starts from."""
        ...

    def mark_failed(self, change: FileChange, error: str) -> None:
        """Record that a file failed to ingest, remove any older pages and chunks of it, and release its duplicates."""
        ...

    def failed_documents(self) -> list[FileChange]:
        """Return the files recorded as failed, so a sync can retry them."""
        ...
