from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from testcontainers.community.postgres import PostgresContainer

from alexandria_core.db import init_schema, make_engine
from alexandria_core.store import PgStore


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    with PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as postgres:
        engine = make_engine(postgres.get_connection_url())
        init_schema(engine)
        yield engine
        engine.dispose()


@pytest.fixture
def store(engine: Engine) -> PgStore:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE documents, pages, chunks, sync_state RESTART IDENTITY CASCADE"))
    return PgStore(engine)
