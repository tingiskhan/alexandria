"""`PgStore`, the Postgres implementation of `DocumentStore`."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath

from sqlalchemy import Engine, delete, insert, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from alexandria_core.db import ChunkRow, DocumentRow, PageRow, SyncStateRow
from alexandria_core.models import (
    Document,
    DocumentSummary,
    EmbeddedChunk,
    FileChange,
    Page,
    SearchHit,
    SearchQuery,
)
from alexandria_core.search import hybrid_search


class PgStore:
    """DocumentStore on Postgres with pgvector. Each method runs in its own transaction."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert_document(self, doc: Document, chunks: Sequence[EmbeddedChunk]) -> None:
        """Insert or replace a document with its pages and chunks in one transaction, and mark it `ok`."""
        with Session(self._engine) as session, session.begin():
            document_id = _upsert_document_row(
                session,
                source_id=doc.source_id,
                path=doc.path,
                title=doc.title,
                content_hash=doc.content_hash,
                page_count=len(doc.pages),
                status="ok",
                error=None,
            )
            if doc.pages:
                session.execute(
                    insert(PageRow),
                    [{"document_id": document_id, "page_number": p.number, "markdown": p.markdown} for p in doc.pages],
                )
            if chunks:
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

    def delete_document(self, source_id: str) -> None:
        """Delete the document, or every document below it when source_id is a deleted folder's path."""
        with self._engine.begin() as conn:
            conn.execute(
                delete(DocumentRow).where(
                    or_(
                        DocumentRow.source_id == source_id,
                        DocumentRow.source_id.startswith(source_id.rstrip("/") + "/", autoescape=True),
                    )
                )
            )

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

    def list_documents(self) -> list[DocumentSummary]:
        """Return every successfully ingested document, sorted by path."""
        with Session(self._engine) as session:
            rows = session.scalars(select(DocumentRow).where(DocumentRow.status == "ok").order_by(DocumentRow.path))
            return [
                DocumentSummary(
                    id=r.id,
                    source_id=r.source_id,
                    content_hash=r.content_hash,
                    title=r.title,
                    path=r.path,
                    page_count=r.page_count,
                    ingested_at=r.ingested_at,
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
        """Record a failed ingest. Its old pages and chunks are removed so search never serves stale content."""
        with Session(self._engine) as session, session.begin():
            _upsert_document_row(
                session,
                source_id=change.source_id,
                path=change.path,
                title=PurePosixPath(change.path).stem,
                content_hash=change.content_hash or "",
                page_count=0,
                status="failed",
                error=error,
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
    title: str,
    content_hash: str,
    page_count: int,
    status: str,
    error: str | None,
) -> int:
    """Insert or update the document row, clear its pages and chunks, and return its id."""
    values = {
        "path": path,
        "title": title,
        "content_hash": content_hash,
        "page_count": page_count,
        "status": status,
        "error": error,
        "ingested_at": datetime.now(UTC),
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
    return document_id
