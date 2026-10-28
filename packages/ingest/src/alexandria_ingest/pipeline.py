"""The ingest steps, independent of scheduling.

Every dependency (source, extractor, embedder, store) is passed in, so the same code runs under Prefect,
from a script, or in tests with fakes. Errors propagate; `sync_changes` is where a failing file gets
recorded and skipped.

One file goes through three steps, split by what they touch so a scheduler can retry each on its own:
`triage` (store only, cheap), `prepare_file` (source, Docling and the embedder; no store) and
`DocumentStore.upsert_document`. `process_file` runs all three in a row.
"""

import logging
from collections.abc import Callable, Set
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from alexandria_core.models import Document, DocumentSummary, EmbeddedChunk, FileChange
from alexandria_core.protocols import DocumentStore, Embedder, Extractor, FileSource
from alexandria_ingest.chunk import chunk_pages, embedding_text

log = logging.getLogger(__name__)

Outcome = Literal["deleted", "skipped", "unchanged", "ingested"]
"""What happened to a file: removed it, ignored it (not an ingested format), found it already indexed, or indexed it."""


@dataclass(frozen=True)
class IndexedDocument:
    """A document and its embedded chunks, ready for DocumentStore.upsert_document."""

    document: Document
    chunks: tuple[EmbeddedChunk, ...]


def extension(path: str) -> str:
    """The file's extension, lower-case and without the dot, as ExtractSettings.formats lists it."""
    return PurePosixPath(path).suffix.lower().lstrip(".")


def cursor_key(source_name: str, formats: Set[str]) -> str:
    """The name a source's sync cursor is stored under.

    Includes the formats, because a source only reports changed files: files of a newly added format were
    skipped before and would never come up again. A new key starts with a full listing, and content hashes
    keep files that are already indexed from being processed again.
    """
    return f"{source_name}|{','.join(sorted(formats))}"


def triage(change: FileChange, *, store: DocumentStore, formats: Set[str]) -> Outcome | None:
    """Handle what needs no download: deletions, other formats and unchanged files. None means: prepare and store it."""
    if change.deleted:
        store.delete_document(change.source_id)
        return "deleted"
    if extension(change.path) not in formats:
        return "skipped"
    if change.content_hash is not None and store.content_hash(change.source_id) == change.content_hash:
        return "unchanged"
    return None


def prepare_file(
    change: FileChange, *, source: FileSource, extractor: Extractor, embedder: Embedder, max_tokens: int
) -> IndexedDocument:
    """Download → extract → chunk → embed. Writes nothing, so it is safe to retry."""
    pages = extractor.extract(source.download(change.source_id), PurePosixPath(change.path).name)
    document = Document(
        source_id=change.source_id,
        path=change.path,
        title=PurePosixPath(change.path).stem,
        content_hash=change.content_hash or "",
        pages=tuple(pages),
    )
    return embed_document(document, embedder=embedder, max_tokens=max_tokens)


def embed_document(document: Document, *, embedder: Embedder, max_tokens: int) -> IndexedDocument:
    """Chunk → embed."""
    chunks = chunk_pages(document.pages, max_tokens)
    vectors = embedder.embed_documents([embedding_text(document.title, c) for c in chunks])
    return IndexedDocument(
        document,
        tuple(
            EmbeddedChunk(chunk=c, embedding=v, model_name=embedder.model_name)
            for c, v in zip(chunks, vectors, strict=True)
        ),
    )


def store_document(indexed: IndexedDocument, *, store: DocumentStore) -> None:
    """Upsert. Replaces the document's pages and chunks in one transaction, so searches never see half of it."""
    store.upsert_document(indexed.document, list(indexed.chunks))


def process_file(
    change: FileChange,
    *,
    source: FileSource,
    extractor: Extractor,
    embedder: Embedder,
    store: DocumentStore,
    max_tokens: int,
    formats: Set[str],
) -> Outcome:
    """Bring the store in line with one changed file: triage, then prepare and store it if needed."""
    outcome = triage(change, store=store, formats=formats)
    if outcome is not None:
        return outcome
    indexed = prepare_file(change, source=source, extractor=extractor, embedder=embedder, max_tokens=max_tokens)
    store_document(indexed, store=store)
    return "ingested"


def reindex_document(summary: DocumentSummary, *, embedder: Embedder, store: DocumentStore, max_tokens: int) -> None:
    """Re-chunk and re-embed a stored document from its stored pages, without downloading it again."""
    document = Document(
        source_id=summary.source_id,
        path=summary.path,
        title=summary.title,
        content_hash=summary.content_hash,
        pages=tuple(store.get_pages(summary.id, 1, summary.page_count)),
    )
    store_document(embed_document(document, embedder=embedder, max_tokens=max_tokens), store=store)


def sync_changes(
    source_name: str,
    list_changes: Callable[[str | None], tuple[list[FileChange], str]],
    store: DocumentStore,
    process: Callable[[FileChange], object],
) -> list[FileChange]:
    """Process everything that changed since the stored cursor, plus earlier failures, then save the new cursor.

    `list_changes` is usually `source.changes`; a scheduler can pass a wrapped version to retry it.
    A file that raises is recorded as failed and the run continues. A deletion that raises ends the run
    without saving the cursor instead: a failed row would be retried as a download of a file that no longer
    exists, while an unsaved cursor makes the next run report the deletion again.

    Returns:
        The files that failed.
    """
    changes, cursor = list_changes(store.get_cursor(source_name))
    seen = {c.source_id for c in changes}
    retries = [c for c in store.failed_documents() if c.source_id not in seen]
    log.info("%d changes and %d retries for %s", len(changes), len(retries), source_name)

    failed: list[FileChange] = []
    for change in [*changes, *retries]:
        try:
            outcome = process(change)
        except Exception as error:
            if change.deleted:
                raise
            log.exception("Failed to process %s", change.path)
            store.mark_failed(change, f"{type(error).__name__}: {error}")
            failed.append(change)
        else:
            log.info("%s: %s", change.path, outcome)

    store.save_cursor(source_name, cursor)
    return failed
