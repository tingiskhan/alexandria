from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from alexandria_core import migrate
from alexandria_core.db import Base
from alexandria_core.models import DocumentMetadata
from alexandria_core.store import PgStore

HEAD = "0003_duplicate_detection"

# The schema as the first release's `init_schema` (create_all) made it, before the metadata columns.
FIRST_RELEASE = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    ingested_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE TABLE sync_state (source TEXT PRIMARY KEY, cursor TEXT NOT NULL, updated_at TIMESTAMP WITH TIME ZONE NOT NULL);
CREATE TABLE chunks (
    id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    section TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    model_name TEXT NOT NULL,
    tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED NOT NULL
);
CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv);
CREATE INDEX ix_chunks_document_id ON chunks (document_id);
CREATE TABLE pages (
    id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    markdown TEXT NOT NULL,
    UNIQUE (document_id, page_number)
);
"""

# What `add_missing_columns` added, as it ran it, in the release that added metadata and in the one after.
METADATA_COLUMNS = """
ALTER TABLE documents ADD COLUMN IF NOT EXISTS "authors" TEXT[];
ALTER TABLE documents ADD COLUMN IF NOT EXISTS "year" INTEGER;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS "doi" TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS "doc_type" TEXT;
"""
DUPLICATE_COLUMNS = """
ALTER TABLE documents ADD COLUMN IF NOT EXISTS "content_digest" TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS "duplicate_of" INTEGER REFERENCES documents (id) ON DELETE SET NULL;
CREATE INDEX ix_documents_content_digest ON documents (content_digest);
CREATE INDEX ix_documents_duplicate_of ON documents (duplicate_of);
"""

# One document with a page and a chunk, as an older release stored it.
OLD_DATA = """
INSERT INTO documents (source_id, path, title, content_hash, page_count, status, ingested_at)
    VALUES ('/old.pdf', '/old.pdf', 'old', 'h', 1, 'ok', now());
INSERT INTO pages (document_id, page_number, markdown) VALUES (1, 1, 'Widgets work.');
INSERT INTO chunks (document_id, page_start, page_end, section, text, embedding, model_name)
    VALUES (1, 1, 1, '', 'Widgets work.', array_fill(0.5, ARRAY[1024])::vector, 'm');
"""


def run_sql(engine: Engine, *scripts: str) -> None:
    with engine.begin() as conn:
        for script in scripts:
            conn.exec_driver_sql(script)


def differences(engine: Engine) -> list[object]:
    """What autogenerate would write to bring the database to the models; empty when they match."""
    with engine.connect() as conn:
        return compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)


def version(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


pytestmark = pytest.mark.integration


def test_a_new_database_is_migrated_to_exactly_the_models(database: Engine) -> None:
    migrate.migrate(database)

    assert version(database) == HEAD
    assert differences(database) == []  # the migrations and db.py describe the same schema


def test_migrate_is_idempotent(database: Engine) -> None:
    migrate.migrate(database)
    PgStore(database).save_cursor("s", "cursor")

    migrate.migrate(database)

    assert version(database) == HEAD
    assert PgStore(database).get_cursor("s") == "cursor"


def test_concurrent_migrations_wait_for_each_other(database: Engine) -> None:
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: migrate.migrate(database), range(4)))

    assert version(database) == HEAD
    assert differences(database) == []


def test_downgrades_to_base_and_upgrades_again(database: Engine) -> None:
    migrate.migrate(database)

    migrate.downgrade(database, "-1")
    assert version(database) == "0002_document_metadata"
    assert "content_digest" not in {c["name"] for c in inspect(database).get_columns("documents")}

    migrate.downgrade(database, "base")
    assert version(database) is None
    assert tables(database) == {"alembic_version"}

    migrate.migrate(database)
    assert version(database) == HEAD
    assert differences(database) == []


def test_upgrades_step_by_step(database: Engine) -> None:
    for revision in ("0001_initial_schema", "0002_document_metadata", HEAD):
        migrate.migrate(database, revision)
        assert version(database) == revision

    assert differences(database) == []


@pytest.mark.parametrize(
    ("scripts", "expected"),
    [
        pytest.param((FIRST_RELEASE,), "0001_initial_schema", id="first release"),
        pytest.param((FIRST_RELEASE, METADATA_COLUMNS), "0002_document_metadata", id="metadata columns"),
        pytest.param((FIRST_RELEASE, METADATA_COLUMNS, DUPLICATE_COLUMNS), HEAD, id="every column"),
    ],
)
def test_a_database_from_before_migrations_is_stamped_and_upgraded(
    database: Engine, scripts: tuple[str, ...], expected: str
) -> None:
    run_sql(database, *scripts, OLD_DATA)

    with migrate.locked(database) as conn:
        assert migrate.stamp_unversioned(conn) == expected
    assert version(database) == expected

    migrate.migrate(database)

    assert version(database) == HEAD
    assert differences(database) == []
    store = PgStore(database)
    [old] = store.list_documents()
    assert old.metadata == DocumentMetadata(title="old")  # NULLs read as unknown
    assert (old.content_digest, old.duplicate_of) == (None, None)
    assert [page.markdown for page in store.get_pages(old.id, 1, 1)] == ["Widgets work."]
    with database.connect() as conn:
        assert conn.execute(text("SELECT tsv::text FROM chunks")).scalar_one() == "'widgets':1 'work':2"


def test_a_versioned_or_empty_database_is_not_stamped(database: Engine) -> None:
    with migrate.locked(database) as conn:
        assert migrate.stamp_unversioned(conn) is None  # empty
    migrate.migrate(database)
    with migrate.locked(database) as conn:
        assert migrate.stamp_unversioned(conn) is None  # versioned


def test_downgrade_stamps_a_database_from_before_migrations_first(database: Engine) -> None:
    run_sql(database, FIRST_RELEASE, METADATA_COLUMNS)

    migrate.downgrade(database, "0001_initial_schema")

    assert version(database) == "0001_initial_schema"
    assert "authors" not in {c["name"] for c in inspect(database).get_columns("documents")}


def test_migrations_need_a_connection() -> None:
    with pytest.raises(RuntimeError, match="pass a connection"):
        command.upgrade(migrate.alembic_config(), "head")


def test_revision_ids_are_listed_newest_first() -> None:
    assert migrate.revision_ids(migrate.alembic_config()) == [
        HEAD,
        "0002_document_metadata",
        "0001_initial_schema",
    ]
