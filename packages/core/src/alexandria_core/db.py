"""The Postgres schema (SQLAlchemy ORM) and the engine helpers entry points use.

The tables are created and changed by the migrations in `migrations/versions/` (see `alexandria_core.migrate`),
never from these classes directly. A change here needs a new migration, written with `alexandria-db revision`.
"""

import logging
import time
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, DateTime, Engine, ForeignKey, Index, Text, UniqueConstraint, create_engine
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from alexandria_core.models import DOC_TYPES, DocumentMetadata

# Baked into the schema. Changing either means a new column or table and a full re-index.
EMBEDDING_DIMENSION = 1024
TS_CONFIG = "simple"

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for Alexandria's tables."""


class DocumentRow(Base):
    """One source file. `status` is "ok", or "failed" with the error, in which case it has no pages or chunks.

    The metadata columns after `title` are nullable: they came after the first release (migration
    `0002_document_metadata`), and rows ingested before that have NULL in them until
    `alexandria-ingest backfill-metadata` runs.

    A document whose `content_digest` matches a searchable one is stored as its duplicate: with its pages, no
    chunks, and `duplicate_of` pointing at the original. `content_digest` is NULL for rows ingested before
    duplicates were detected until `alexandria-ingest duplicates` runs.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, unique=True)  # the source's file id: Dropbox path_lower, S3 key
    path: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    year: Mapped[int | None]
    doi: Mapped[str | None] = mapped_column(Text)
    doc_type: Mapped[str | None] = mapped_column(Text)  # one of models.DOC_TYPES
    content_hash: Mapped[str] = mapped_column(Text)
    page_count: Mapped[int]
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_digest: Mapped[str | None] = mapped_column(Text, index=True)  # models.content_digest of the pages
    # The store re-ingests a duplicate whose original is deleted or changes; SET NULL only guards manual deletes.
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="SET NULL"), index=True)


class PageRow(Base):
    """The extracted Markdown of one page."""

    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("document_id", "page_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    page_number: Mapped[int]
    markdown: Mapped[str] = mapped_column(Text)


class ChunkRow(Base):
    """One chunk with its embedding and a generated tsvector for full-text search."""

    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("chunks_tsv_gin", "tsv", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_start: Mapped[int]
    page_end: Mapped[int]
    section: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION))
    model_name: Mapped[str] = mapped_column(Text)
    tsv: Mapped[str] = mapped_column(TSVECTOR, Computed(f"to_tsvector('{TS_CONFIG}', text)", persisted=True))


class SyncStateRow(Base):
    """The cursor a source's next sync starts from."""

    __tablename__ = "sync_state"

    source: Mapped[str] = mapped_column(Text, primary_key=True)
    cursor: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def metadata_columns(metadata: DocumentMetadata) -> dict[str, object]:
    """The `documents` columns that hold `metadata`, as values for an insert or update."""
    return {
        "title": metadata.title,
        "authors": list(metadata.authors),
        "year": metadata.year,
        "doi": metadata.doi,
        "doc_type": metadata.doc_type,
    }


def document_metadata(
    title: str, authors: list[str] | None, year: int | None, doi: str | None, doc_type: str | None
) -> DocumentMetadata:
    """Read the metadata columns back. Rows from before they existed have NULLs, which read as unknown."""
    return DocumentMetadata(
        title=title,
        authors=tuple(authors or ()),
        year=year,
        doi=doi,
        doc_type=doc_type if doc_type in DOC_TYPES else "other",
    )


def make_engine(url: str) -> Engine:
    """Create an engine for a `postgresql+psycopg://` URL."""
    # Pre-ping replaces pooled connections that died, e.g. when Postgres restarted between two queries.
    return create_engine(url, pool_pre_ping=True)


def wait_for_database(engine: Engine, *, timeout: float = 120, interval: float = 5) -> None:
    """Block until the database accepts connections, e.g. while Postgres starts or recovers after a restart.

    Pre-ping cannot help with the first connection, so entry points call this before doing any work.
    Raises the last OperationalError once `timeout` seconds have passed.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            with engine.connect():
                return
        except OperationalError as error:
            if time.monotonic() + interval > deadline:
                raise
            log.warning("Database not ready (%s); retrying in %gs", str(error.orig).strip(), interval)
            time.sleep(interval)
