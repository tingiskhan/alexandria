from collections.abc import Callable, Iterator
from itertools import count

import pytest
from sqlalchemy import Engine, text
from testcontainers.community.postgres import PostgresContainer

from alexandria_core.db import make_engine
from alexandria_core.migrate import migrate
from alexandria_core.store import PgStore


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    with PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as postgres:
        engine = make_engine(postgres.get_connection_url())
        migrate(engine)
        yield engine
        engine.dispose()


@pytest.fixture
def store(engine: Engine) -> PgStore:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE documents, pages, chunks, sync_state RESTART IDENTITY CASCADE"))
    return PgStore(engine)


databases = count()


@pytest.fixture
def new_database(engine: Engine) -> Iterator[Callable[[], Engine]]:
    """Creates empty databases in the test server, and drops them afterwards."""
    admin = engine.execution_options(isolation_level="AUTOCOMMIT")
    created: list[tuple[str, Engine]] = []

    def create() -> Engine:
        name = f"migrations_{next(databases)}"
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {name}"))
        created.append((name, make_engine(engine.url.set(database=name).render_as_string(hide_password=False))))
        return created[-1][1]

    yield create
    for name, database in created:
        database.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))


@pytest.fixture
def database(new_database: Callable[[], Engine]) -> Engine:
    return new_database()
