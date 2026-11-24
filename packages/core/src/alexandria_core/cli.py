"""The `alexandria-db` command: schema migrations.

Entry points migrate on start, so `upgrade` is only needed to migrate ahead of them. `revision` is for
developers adding a migration.
"""

from typing import Annotated

import typer
from alembic import command
from pydantic import ValidationError
from sqlalchemy import Engine

from alexandria_core import migrate
from alexandria_core.db import make_engine, wait_for_database
from alexandria_core.settings import DatabaseSettings

app = typer.Typer(name="alexandria-db", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Alexandria's database schema migrations."""


def database() -> Engine:
    """An engine for ALEXANDRIA_DATABASE_URL, once the database accepts connections.

    Raises:
        typer.Exit: With code 2 if the URL isn't set.
    """
    try:
        url = DatabaseSettings().url.get_secret_value()
    except ValidationError as error:
        typer.echo(f"Configuration is incomplete; see .env.example.\n{error}", err=True)
        raise typer.Exit(2) from error
    engine = make_engine(url)
    wait_for_database(engine)
    return engine


@app.command()
def upgrade(
    revision: Annotated[str, typer.Argument(help="The revision to upgrade to, or `head` for the newest.")] = "head",
) -> None:
    """Upgrade the schema; a database from before migrations is stamped with the revision it matches first."""
    migrate.migrate(database(), revision)


@app.command()
def downgrade(
    revision: Annotated[str, typer.Argument(help="The revision to go back to, `-1` for one step, or `base`.")],
) -> None:
    """Downgrade the schema. Data in the columns and tables that go away is lost; `base` drops every table."""
    migrate.downgrade(database(), revision)


@app.command()
def current() -> None:
    """Print the database's revision; nothing for a database from before migrations that no command has stamped."""
    with database().connect() as connection:
        command.current(migrate.alembic_config(connection), verbose=False)


@app.command()
def history() -> None:
    """List every revision, newest first."""
    command.history(migrate.alembic_config(), verbose=False)


@app.command()
def revision(
    message: Annotated[str, typer.Option("--message", "-m", help="What it changes, e.g. 'add tags'.")],
    autogenerate: Annotated[
        bool, typer.Option(help="Compare the models with a database at head and write the difference.")
    ] = False,
) -> None:
    """Write a new revision, numbered after the newest: `-m "add tags"` writes `0004_add_tags.py`.

    With --autogenerate, ALEXANDRIA_DATABASE_URL must point at a scratch database upgraded to head (`make
    migration` starts one). Review what it writes: autogenerate misses or gets some changes wrong.
    """
    config = migrate.alembic_config()
    rev_id = migrate.next_revision_id(message, migrate.revision_ids(config))
    if not autogenerate:
        command.revision(config, message, rev_id=rev_id)
        return
    with database().connect() as connection:
        config.attributes["connection"] = connection
        command.revision(config, message, autogenerate=True, rev_id=rev_id)
