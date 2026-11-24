"""`PgStore`, the Postgres implementation of `DocumentStore`."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath

from sqlalchemy import ColumnElement, Engine, delete, insert, or_, select, text, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from alexandria_core.db import (
    ChunkRow,
    DocumentRow,
    PageRow,
    SyncStateRow,
    document_metadata,
    metadata_columns,
)
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
    content_digest,
)
from alexandria_core.search import document_conditions, hybrid_search

RELEASED = "Its original was deleted or changed, so it is ingested again on the next sync"
"""The error a released duplicate is recorded as failed with; see `_release_duplicates`."""


class PgStore:
    """DocumentStore on Postgres with pgvector. Each method runs in its own transaction."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert_document(self, doc: Document, chunks: Sequence[EmbeddedChunk]) -> None:
        """Insert or replace a document with its pages and chunks in one transaction, and mark it `ok`.

        A document with the same content as a searchable one is stored as its duplicate, without the chunks.
        """
        digest = content_digest(doc.pages)
        with Session(self._engine) as session, session.begin():
            original = _original_of(session, doc.source_id, digest)
            document_id = _upsert_document_row(
                session,
                source_id=doc.source_id,
                path=doc.path,
                metadata=doc.metadata,
                content_hash=doc.content_hash,
                page_count=len(doc.pages),
                status="ok",
                error=None,
                digest=digest,
                duplicate_of=original,
            )
            if doc.pages:
                session.execute(
                    insert(PageRow),
                    [{"document_id": document_id, "page_number": p.number, "markdown": p.markdown} for p in doc.pages],
                )
            if chunks and original is None:
                session.execute(
                    insert(ChunkRow),
                    [
                        {
                            "document_id": document_id,
                            "page_start": c.chunk.page_start,
                            "page_end": c.chunk.page_end,
                            "section": c.chunk.section,
                            "text": c.chunk.text,
                            "embedding": c.embedding,
                            "model_name": c.model_name,
                        }
                        for c in chunks
                    ],
                )

    def original_of(self, doc: Document) -> int | None:
        """Return the id of the searchable document with the same content as `doc`, or None."""
        with Session(self._engine) as session:
            return _original_of(session, doc.source_id, content_digest(doc.pages))

    def update_content_digest(self, document_id: int, digest: str | None) -> None:
        """Store a document's content digest, changing nothing else."""
        with self._engine.begin() as conn:
            conn.execute(update(DocumentRow).where(DocumentRow.id == document_id).values(content_digest=digest))

    def update_metadata(self, document_id: int, metadata: DocumentMetadata) -> None:
        """Replace a stored document's metadata, leaving its pages and chunks alone."""
        with self._engine.begin() as conn:
            conn.execute(update(DocumentRow).where(DocumentRow.id == document_id).values(**metadata_columns(metadata)))

    def delete_document(self, source_id: str) -> None:
        """Delete the document, or every document below it when source_id is a deleted folder's path.

        Duplicates of the deleted documents that are not deleted with them are released, to be ingested again.
        """
        deleted = or_(
            DocumentRow.source_id == source_id,
            DocumentRow.source_id.startswith(source_id.rstrip("/") + "/", autoescape=True),
        )
        with Session(self._engine) as session, session.begin():
            _release_duplicates(session, DocumentRow.duplicate_of.in_(select(DocumentRow.id).where(deleted)), ~deleted)
            session.execute(delete(DocumentRow).where(deleted))

    def content_hash(self, source_id: str) -> str | None:
        """Return the stored hash of a successfully ingested document, or None."""
        with Session(self._engine) as session:
            return session.scalar(
                select(DocumentRow.content_hash).where(DocumentRow.source_id == source_id, DocumentRow.status == "ok")
            )

    def search(self, query: SearchQuery) -> list[SearchHit]:
        """Run a hybrid search; see `alexandria_core.search`."""
        with self._engine.begin() as conn:
            return hybrid_search(conn, query)

    def get_pages(self, document_id: int, first: int, last: int) -> list[Page]:
        """Return the stored pages `first` to `last` (inclusive) of a document."""
        with Session(self._engine) as session:
            rows = session.execute(
                select(PageRow.page_number, PageRow.markdown)
                .where(PageRow.document_id == document_id, PageRow.page_number.between(first, last))
                .order_by(PageRow.page_number)
            )
            return [Page(number=number, markdown=markdown) for number, markdown in rows]

    def list_documents(self, filters: DocumentFilter | None = None) -> list[DocumentSummary]:
        """Return the successfully ingested documents that match `filters`, duplicates included, sorted by path.

        Filtered in SQL.
        """
        statement = select(DocumentRow).where(DocumentRow.status == "ok").order_by(DocumentRow.path)
        if filters is not None:
            # The same conditions search uses, so both tools agree on what a filter matches.
            conditions, params = document_conditions(filters)
            if conditions:
                statement = statement.where(text(" AND ".join(conditions)).bindparams(**params))
        with Session(self._engine) as session:
            rows = session.scalars(statement)
            return [
                DocumentSummary(
                    id=r.id,
                    source_id=r.source_id,
                    content_hash=r.content_hash,
                    metadata=document_metadata(r.title, r.authors, r.year, r.doi, r.doc_type),
                    path=r.path,
                    page_count=r.page_count,
                    ingested_at=r.ingested_at,
                    content_digest=r.content_digest,
                    duplicate_of=r.duplicate_of,
                )
                for r in rows
            ]

    def get_cursor(self, source: str) -> str | None:
        """Return the cursor saved for `source`, or None."""
        with Session(self._engine) as session:
            return session.scalar(select(SyncStateRow.cursor).where(SyncStateRow.source == source))

    def save_cursor(self, source: str, cursor: str) -> None:
        """Save or replace the cursor for `source`."""
        now = datetime.now(UTC)
        statement = pg_insert(SyncStateRow).values(source=source, cursor=cursor, updated_at=now)
        statement = statement.on_conflict_do_update(
            index_elements=[SyncStateRow.source], set_={"cursor": cursor, "updated_at": now}
        )
        with self._engine.begin() as conn:
            conn.execute(statement)

    def mark_failed(self, change: FileChange, error: str) -> None:
        """Record a failed ingest. Its old pages and chunks are removed so search never serves stale content.

        Its duplicates are released, to be ingested again.
        """
        with Session(self._engine) as session, session.begin():
            _upsert_document_row(
                session,
                source_id=change.source_id,
                path=change.path,
                metadata=DocumentMetadata(title=PurePosixPath(change.path).stem),
                content_hash=change.content_hash or "",
                page_count=0,
                status="failed",
                error=error,
                digest=None,
                duplicate_of=None,
            )

    def failed_documents(self) -> list[FileChange]:
        """Return the files recorded as failed, so a sync can retry them."""
        with Session(self._engine) as session:
            rows = session.execute(
                select(DocumentRow.source_id, DocumentRow.path, DocumentRow.content_hash).where(
                    DocumentRow.status == "failed"
                )
            )
            return [FileChange(source_id=s, path=p, content_hash=h or None) for s, p, h in rows]


def _upsert_document_row(
    session: Session,
    *,
    source_id: str,
    path: str,
    metadata: DocumentMetadata,
    content_hash: str,
    page_count: int,
    status: str,
    error: str | None,
    digest: str | None,
    duplicate_of: int | None,
) -> int:
    """Insert or update the document row, clear its pages and chunks, and return its id.

    Releases the duplicates of it that no longer match: all of them, unless it is a searchable document with the
    same content as before.
    """
    values = {
        "path": path,
        **metadata_columns(metadata),
        "content_hash": content_hash,
        "page_count": page_count,
        "status": status,
        "error": error,
        "ingested_at": datetime.now(UTC),
        "content_digest": digest,
        "duplicate_of": duplicate_of,
    }
    statement = (
        pg_insert(DocumentRow)
        .values(source_id=source_id, **values)
        .on_conflict_do_update(index_elements=[DocumentRow.source_id], set_=values)
        .returning(DocumentRow.id)
    )
    document_id: int = session.execute(statement).scalar_one()
    session.execute(delete(PageRow).where(PageRow.document_id == document_id))
    session.execute(delete(ChunkRow).where(ChunkRow.document_id == document_id))
    stale = DocumentRow.content_digest.is_distinct_from(digest) if status == "ok" and duplicate_of is None else true()
    _release_duplicates(session, DocumentRow.duplicate_of == document_id, stale)
    return document_id


def _original_of(session: Session, source_id: str, digest: str | None) -> int | None:
    """The searchable document, other than `source_id`, whose content digest is `digest`; the oldest if several."""
    if digest is None:
        return None
    return session.scalar(
        select(DocumentRow.id)
        .where(
            DocumentRow.content_digest == digest,
            DocumentRow.status == "ok",
            DocumentRow.duplicate_of.is_(None),
            DocumentRow.source_id != source_id,
        )
        .order_by(DocumentRow.id)
        .limit(1)
    )


def _release_duplicates(session: Session, *where: ColumnElement[bool]) -> None:
    """Record the duplicates matching `where` as failed, so the next sync ingests them again.

    Used when their original is deleted, fails or changes. A retried file is downloaded and extracted again, which
    costs a Docling run, but reuses the retry of failed files instead of adding a step to every sync. The first one
    stored then becomes searchable, and any others its duplicates.
    """
    released = session.scalars(
        update(DocumentRow)
        .where(DocumentRow.duplicate_of.is_not(None), *where)
        .values(status="failed", error=RELEASED, page_count=0, content_digest=None, duplicate_of=None)
        .returning(DocumentRow.id)
        .execution_options(synchronize_session=False)
    ).all()
    if released:
        session.execute(delete(PageRow).where(PageRow.document_id.in_(released)))
