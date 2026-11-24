"""The ingest steps, independent of scheduling.

Every dependency (source, extractor, embedder, store, metadata reader) is passed in, so the same code runs under
Prefect, from a script, or in tests with fakes. Errors propagate; `sync_changes` is where a failing file gets
recorded and skipped.

One file goes through three steps, split by what they touch so a scheduler can retry each on its own:
`triage` (store only, cheap), `prepare_file` (source, Docling, Crossref and the embedder; reads the store to
skip embedding a duplicate but writes nothing) and `DocumentStore.upsert_document`. `process_file` runs all three
in a row.
"""

import logging
import re
from collections.abc import Callable, Iterable, Iterator, Set
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from alexandria_core.models import (
    Document,
    DocumentMetadata,
    DocumentSummary,
    EmbeddedChunk,
    FileChange,
    Page,
    content_digest,
)
from alexandria_core.protocols import DocumentStore, Embedder, Extractor, FileSource
from alexandria_ingest.boilerplate import BoilerplateRules, strip_boilerplate
from alexandria_ingest.chunk import chunk_pages, embedding_text
from alexandria_ingest.katex import katex_error
from alexandria_ingest.latex import Formula, formulas
from alexandria_ingest.metadata import MetadataReader
from alexandria_ingest.settings import extension

log = logging.getLogger(__name__)

Outcome = Literal["deleted", "skipped", "unchanged", "ingested", "duplicate"]
"""What happened to a file: removed it, ignored it (not an ingested format), found it already indexed, indexed it,
or stored it as a duplicate of a document with the same content."""


@dataclass(frozen=True)
class IndexedDocument:
    """A document and its embedded chunks, ready for DocumentStore.upsert_document.

    A duplicate has no chunks, and `duplicate_of` is the document it duplicates.
    """

    document: Document
    chunks: tuple[EmbeddedChunk, ...]
    duplicate_of: int | None = None

    @property
    def outcome(self) -> Outcome:
        """What `process_file` reports: `duplicate` or `ingested`."""
        return "ingested" if self.duplicate_of is None else "duplicate"


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


def warn_about_truncation(path: str, pages: Iterable[Page]) -> None:
    """Log paragraphs with an unclosed `{`: usually math cut off mid-formula, which no extractor reports as an error."""
    for page in pages:
        for paragraph in re.split(r"\n\s*\n", page.markdown):
            if paragraph.count("{") > paragraph.count("}"):
                log.warning("%s p. %d: possibly truncated text: …%s", path, page.number, paragraph[-80:])


def formula_errors(page: Page) -> Iterator[tuple[Formula, str]]:
    """The page's formulas that KaTeX rejects, with its error message."""
    for formula in formulas(page.markdown):
        if (error := katex_error(formula.latex, display=formula.display)) is not None:
            yield formula, error


def warn_about_invalid_formulas(path: str, pages: Iterable[Page]) -> None:
    """Log formulas that will not render. These are mostly model reading errors, so the document is still stored."""
    for page in pages:
        for _, error in formula_errors(page):
            log.warning("%s p. %d: invalid formula: %s", path, page.number, error)


def prepare_file(
    change: FileChange,
    *,
    source: FileSource,
    extractor: Extractor,
    embedder: Embedder,
    store: DocumentStore,
    max_tokens: int,
    metadata_reader: MetadataReader,
    boilerplate: BoilerplateRules | None = None,
) -> IndexedDocument:
    """Download → extract → read metadata → chunk → embed. Writes nothing, so it is safe to retry.

    A document with the same text as one already searchable (`DocumentStore.original_of`) is not chunked or
    embedded; the store records it as a duplicate.
    """
    pages = extractor.extract(source.download(change.source_id), PurePosixPath(change.path).name)
    warn_about_truncation(change.path, pages)
    warn_about_invalid_formulas(change.path, pages)
    document = Document(
        source_id=change.source_id,
        path=change.path,
        metadata=metadata_reader.read(change.path, pages[0].markdown if pages else ""),
        content_hash=change.content_hash or "",
        pages=tuple(pages),
    )
    if (original := store.original_of(document)) is not None:
        log.info("%s has the same content as document %d; not embedding it", change.path, original)
        return IndexedDocument(document, (), duplicate_of=original)
    return embed_document(document, embedder=embedder, max_tokens=max_tokens, boilerplate=boilerplate)


def embed_document(
    document: Document, *, embedder: Embedder, max_tokens: int, boilerplate: BoilerplateRules | None = None
) -> IndexedDocument:
    """Strip boilerplate → chunk → embed.

    Only the chunks lose the boilerplate; the document keeps its pages as extracted, so page reads match the file,
    a reindex can apply changed rules, and the content digest that finds duplicates is taken over the whole
    pages. None strips nothing, and neither does a transcript: its time blocks have no running headers or front
    matter, and a line said in most blocks ("Alice: Yes.") is speech, not boilerplate.
    """
    strip = boilerplate is not None and document.metadata.doc_type != "transcript"
    pages = strip_boilerplate(document.pages, boilerplate) if strip else document.pages
    chunks = chunk_pages(pages, max_tokens)
    vectors = embedder.embed_documents([embedding_text(document.metadata.title, c) for c in chunks])
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
    metadata_reader: MetadataReader,
    boilerplate: BoilerplateRules | None = None,
) -> Outcome:
    """Bring the store in line with one changed file: triage, then prepare and store it if needed."""
    outcome = triage(change, store=store, formats=formats)
    if outcome is not None:
        return outcome
    indexed = prepare_file(
        change,
        source=source,
        extractor=extractor,
        embedder=embedder,
        store=store,
        max_tokens=max_tokens,
        metadata_reader=metadata_reader,
        boilerplate=boilerplate,
    )
    store_document(indexed, store=store)
    return indexed.outcome


def reindex_document(
    summary: DocumentSummary,
    *,
    embedder: Embedder,
    store: DocumentStore,
    max_tokens: int,
    boilerplate: BoilerplateRules | None = None,
) -> None:
    """Re-chunk and re-embed a stored document from its stored pages, without downloading it again.

    Duplicates are left alone: they have no chunks.
    """
    if summary.duplicate_of is not None:
        return
    document = Document(
        source_id=summary.source_id,
        path=summary.path,
        metadata=summary.metadata,
        content_hash=summary.content_hash,
        pages=tuple(store.get_pages(summary.id, 1, summary.page_count)),
    )
    indexed = embed_document(document, embedder=embedder, max_tokens=max_tokens, boilerplate=boilerplate)
    store_document(indexed, store=store)


def read_stored_metadata(
    summary: DocumentSummary, *, store: DocumentStore, metadata_reader: MetadataReader
) -> DocumentMetadata:
    """Work out a stored document's metadata again from its stored first page, without downloading or extracting it.

    Chunks keep the title they were embedded with until the document is re-indexed (`reindex_document`).
    """
    first_page = store.get_pages(summary.id, 1, 1)
    return metadata_reader.read(summary.path, first_page[0].markdown if first_page else "")


def stored_content_digest(summary: DocumentSummary, *, store: DocumentStore) -> str | None:
    """A stored document's content digest. One ingested before digests existed gets it from its stored pages.

    Neither downloads nor extracts anything. A digest computed here is stored, so later files with the same
    content are recognised as duplicates of this document.
    """
    if summary.content_digest is not None:
        return summary.content_digest
    digest = content_digest(store.get_pages(summary.id, 1, summary.page_count))
    if digest is not None:
        store.update_content_digest(summary.id, digest)
    return digest


def duplicate_groups(
    summaries: Iterable[DocumentSummary], digests: Iterable[str | None]
) -> list[list[DocumentSummary]]:
    """Group documents with the same digest. Each group lists the searched one first, then the others by id.

    The searched one is the one that isn't a duplicate, or the oldest when several aren't (documents from before
    duplicates were detected). Documents without a digest, and groups of one, are left out.
    """
    groups: dict[str, list[DocumentSummary]] = {}
    for summary, digest in zip(summaries, digests, strict=True):
        if digest is not None:
            groups.setdefault(digest, []).append(summary)
    result = []
    for group in groups.values():
        if len(group) > 1:
            searched = min(group, key=lambda s: (s.duplicate_of is not None, s.id))
            result.append([searched, *sorted((s for s in group if s is not searched), key=lambda s: s.id)])
    return result


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
