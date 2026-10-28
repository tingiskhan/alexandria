"""The Postgres schema (SQLAlchemy ORM) and the engine helpers entry points use."""

import logging
import time
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, DateTime, Engine, ForeignKey, Index, Text, UniqueConstraint, create_engine, text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Baked into the schema. Changing either means a new column or table and a full re-index.
EMBEDDING_DIMENSION = 1024
TS_CONFIG = "simple"

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for Alexandria's tables."""


class DocumentRow(Base):
    """One source file. `status` is "ok", or "failed" with the error, in which case it has no pages or chunks."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, unique=True)  # the source's file id: Dropbox path_lower, S3 key
    path: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    page_count: Mapped[int]
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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


def init_schema(engine: Engine) -> None:
    """Create the `vector` extension and any missing tables. Existing tables are not migrated."""
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
