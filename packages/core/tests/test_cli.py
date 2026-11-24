import shutil
from importlib.resources import files
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from sqlalchemy import Engine
from typer.testing import CliRunner

from alexandria_core import migrate
from alexandria_core.cli import app


def use(monkeypatch: pytest.MonkeyPatch, database: Engine) -> None:
    monkeypatch.setenv("ALEXANDRIA_DATABASE_URL", database.url.render_as_string(hide_password=False))


def version(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


@pytest.fixture
def scripts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A copy of the migrations, so `revision` writes there instead of into the package."""
    copy = tmp_path / "migrations"
    shutil.copytree(str(files("alexandria_core") / "migrations"), copy, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(migrate, "SCRIPT_LOCATION", str(copy))
    return copy / "versions"


def test_reports_missing_settings() -> None:
    result = CliRunner().invoke(app, ["upgrade"])

    assert result.exit_code == 2
    assert "Configuration is incomplete" in result.output


def test_history_lists_the_revisions() -> None:
    result = CliRunner().invoke(app, ["history"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].startswith("0002_document_metadata -> 0003_duplicate_detection (head), Duplicate detection")
    assert lines[-1].startswith("<base> -> 0001_initial_schema, Initial schema")


@pytest.mark.integration
def test_upgrade_current_and_downgrade(monkeypatch: pytest.MonkeyPatch, database: Engine) -> None:
    use(monkeypatch, database)
    runner = CliRunner()

    assert runner.invoke(app, ["upgrade", "0002_document_metadata"]).exit_code == 0
    assert version(database) == "0002_document_metadata"
    assert runner.invoke(app, ["upgrade"]).exit_code == 0
    assert version(database) == "0003_duplicate_detection"
    current = runner.invoke(app, ["current"])
    assert current.exit_code == 0, current.output
    assert "0003_duplicate_detection (head)" in current.stdout
    assert runner.invoke(app, ["downgrade", "base"]).exit_code == 0
    assert version(database) is None


def test_revision_is_numbered_after_the_newest(scripts: Path) -> None:
    result = CliRunner().invoke(app, ["revision", "-m", "Add tags"])

    assert result.exit_code == 0, result.output
    written = (scripts / "0004_add_tags.py").read_text()
    assert '"""Add tags.' in written
    assert 'revision: str = "0004_add_tags"' in written
    assert 'down_revision: str | None = "0003_duplicate_detection"' in written


@pytest.mark.integration
def test_autogenerate_finds_nothing_to_do_at_head(
    monkeypatch: pytest.MonkeyPatch, database: Engine, scripts: Path
) -> None:
    migrate.migrate(database)
    use(monkeypatch, database)

    result = CliRunner().invoke(app, ["revision", "-m", "nothing", "--autogenerate"])

    assert result.exit_code == 0, result.output
    written = (scripts / "0004_nothing.py").read_text()
    assert "op." not in written  # the models and the migrations agree


@pytest.mark.integration
def test_autogenerate_writes_pgvector_columns_with_their_import(
    monkeypatch: pytest.MonkeyPatch, database: Engine, scripts: Path
) -> None:
    for script in scripts.glob("*.py"):
        script.unlink()  # with no revisions and an empty database, autogenerate writes every table
    use(monkeypatch, database)

    result = CliRunner().invoke(app, ["revision", "-m", "everything", "--autogenerate"])

    assert result.exit_code == 0, result.output
    written = (scripts / "0001_everything.py").read_text()
    assert "from pgvector.sqlalchemy import Vector" in written
    assert 'sa.Column("embedding", Vector(1024), nullable=False)' in written
