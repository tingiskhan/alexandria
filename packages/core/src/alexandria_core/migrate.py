"""Schema migrations with Alembic, configured in code so they ship with the package.

The revisions live in `alexandria_core/migrations/versions/`, one file per revision, named after its id: the
next four-digit number and a snake_case description, e.g. `0004_add_tags` (see `next_revision_id`).

Every entry point calls `migrate` on start. It takes a Postgres advisory lock for the duration, so a Prefect
worker and the MCP server starting together don't both migrate. A database created before the migrations
existed has the tables but no `alembic_version`; `migrate` works out which revision its tables match, stamps
that, and upgrades from there.
"""

import logging
import re
import sys
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from typing import TextIO

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, inspect, text

log = logging.getLogger(__name__)

SCRIPT_LOCATION = "alexandria_core:migrations"
FIRST_REVISION = "0001_initial_schema"

# The `documents` columns each revision after the first adds, in order. Releases from before migrations added
# them with `ALTER TABLE … ADD COLUMN`, so they tell which revision such a database is at. Only revisions from
# before migrations belong here: databases at later ones have `alembic_version`.
REVISION_COLUMNS: tuple[tuple[str, frozenset[str]], ...] = (
    ("0002_document_metadata", frozenset({"authors", "year", "doi", "doc_type"})),
    ("0003_duplicate_detection", frozenset({"content_digest", "duplicate_of"})),
)

# The key of the advisory lock `migrate` holds; any fixed bigint works, as long as nothing else uses it.
LOCK_KEY = 0x616C6578616E6472  # "alexandr"

# alembic_version.version_num is VARCHAR(32).
MAX_REVISION_ID_LENGTH = 32


def alembic_config(connection: Connection | None = None, *, stdout: TextIO | None = None) -> Config:
    """Alembic's configuration, without an alembic.ini.

    Args:
        connection: The connection the migrations run on, handed to `migrations/env.py`. Commands that only
            read the scripts (`history`) don't need one.
        stdout: Where commands such as `current` and `history` print; by default `sys.stdout`.
    """
    config = Config(stdout=stdout or sys.stdout)
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.set_main_option("file_template", "%%(rev)s")  # the revision id already says what it does
    # Format new revisions the way the rest of the code is; only `alexandria-db revision` writes any.
    config.set_section_option("post_write_hooks", "hooks", "ruff_format, ruff_fix")
    for hook, arguments in (("ruff_format", "format"), ("ruff_fix", "check --fix")):
        config.set_section_option("post_write_hooks", f"{hook}.type", "module")
        config.set_section_option("post_write_hooks", f"{hook}.module", "ruff")
        config.set_section_option("post_write_hooks", f"{hook}.options", f"{arguments} REVISION_SCRIPT_FILENAME")
    config.attributes["connection"] = connection
    return config


@contextmanager
def locked(engine: Engine) -> Generator[Connection]:
    """A transaction holding the migration lock, so only one process changes the schema at a time.

    The lock is released when the transaction ends. Postgres DDL is transactional, so a failed migration leaves
    the schema as it was.
    """
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
        yield connection


def migrate(engine: Engine, revision: str = "head") -> None:
    """Bring the schema to `revision` (by default the newest), stamping a database from before the migrations first.

    Idempotent, and safe to call from several processes at once, so every entry point calls it on start.
    """
    with locked(engine) as connection:
        stamp_unversioned(connection)
        # Alembic logs what it runs; stdout is the MCP server's stdio transport, so nothing may print there.
        command.upgrade(alembic_config(connection, stdout=sys.stderr), revision)


def downgrade(engine: Engine, revision: str) -> None:
    """Revert the schema to `revision`, e.g. `0002_document_metadata`, `-1` or `base` (which drops every table)."""
    with locked(engine) as connection:
        stamp_unversioned(connection)
        command.downgrade(alembic_config(connection, stdout=sys.stderr), revision)


def stamp_unversioned(connection: Connection) -> str | None:
    """Stamp a database created before the migrations with the revision its tables match, and return it.

    Does nothing to a database that already has `alembic_version`, or that has no `documents` table (a new one).
    """
    inspector = inspect(connection)
    if inspector.has_table("alembic_version") or not inspector.has_table("documents"):
        return None
    revision = detect_revision({column["name"] for column in inspector.get_columns("documents")})
    log.info("The database predates migrations; its tables match %s, so it is stamped with it", revision)
    command.stamp(alembic_config(connection, stdout=sys.stderr), revision)
    return revision


def detect_revision(document_columns: Iterable[str]) -> str:
    """The revision a `documents` table with these columns was created at, by an Alexandria from before migrations.

    Older releases added the columns in revision order, each revision's together in one transaction, so a table
    has every column of the revisions up to its own, and none of the later ones.

    Raises:
        RuntimeError: If no release made a table with these columns, e.g. one with some of a revision's columns.
    """
    columns = set(document_columns)
    detected, complete = FIRST_REVISION, True
    for revision, added in REVISION_COLUMNS:
        present = added & columns
        if complete and present == added:
            detected = revision
            continue
        if present:
            raise RuntimeError(
                f"documents has {', '.join(sorted(present))} from {revision} without every column before them;"
                " no release made such a table, so it can't be stamped"
            )
        complete = False
    return detected


def next_revision_id(message: str, existing: Iterable[str]) -> str:
    """The id for a new revision: the next four-digit number, then `message` in snake_case.

    "Add tags" after `0003_duplicate_detection` is `0004_add_tags`. Ids are cut to fit `alembic_version`.

    Raises:
        ValueError: If `message` has no letters or digits.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")
    if not slug:
        raise ValueError(f"A revision needs a description with letters or digits, not {message!r}")
    numbers = [int(match.group()) for revision in existing if (match := re.match(r"\d+", revision))]
    prefix = f"{max(numbers, default=0) + 1:04d}_"
    return prefix + slug[: MAX_REVISION_ID_LENGTH - len(prefix)].rstrip("_")


def revision_ids(config: Config) -> list[str]:
    """The ids of every revision in the scripts, newest first."""
    return [script.revision for script in ScriptDirectory.from_config(config).walk_revisions()]
