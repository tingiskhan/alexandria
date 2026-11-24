from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from alexandria_core.settings import DatabaseSettings, settings_config

URL = "postgresql+psycopg://alexandria:hunter2@db:5432/alexandria"


def test_reads_prefixed_env_and_hides_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_DATABASE_URL", URL)

    settings = DatabaseSettings()

    assert settings.url.get_secret_value() == URL
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)


def test_missing_required_value_fails_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALEXANDRIA_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="url"):
        DatabaseSettings()


def test_reads_secret_files_and_env_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "ALEXANDRIA_EXAMPLE_TOKEN").write_text("from-file\n")
    (tmp_path / "ALEXANDRIA_EXAMPLE_OTHER").write_text("from-file\n")
    monkeypatch.setenv("ALEXANDRIA_EXAMPLE_OTHER", "from-env")

    class ExampleSettings(BaseSettings):
        model_config = {**settings_config("ALEXANDRIA_EXAMPLE_"), "secrets_dir": tmp_path}

        token: str
        other: str

    settings = ExampleSettings()

    assert (settings.token, settings.other) == ("from-file", "from-env")


def test_empty_value_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_DATABASE_URL", "")
    with pytest.raises(ValidationError):
        DatabaseSettings()
