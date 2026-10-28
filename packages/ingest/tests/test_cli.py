from typing import Any

import pytest
from dropbox.exceptions import AuthError
from pydantic import ValidationError
from typer.testing import CliRunner

from alexandria_ingest import cli, sources

from .sources.test_dropbox import FakeDropboxClient
from .sources.test_s3 import FakeObjectStore


@pytest.fixture(autouse=True)
def source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "ALEXANDRIA_S3_BUCKET": "pdfs",
        "ALEXANDRIA_S3_PREFIX": "papers/",
        "ALEXANDRIA_S3_ACCESS_KEY_ID": "id",
        "ALEXANDRIA_S3_SECRET_ACCESS_KEY": "secret",
        "ALEXANDRIA_DROPBOX_APP_KEY": "key",
        "ALEXANDRIA_DROPBOX_REFRESH_TOKEN": "token",
    }.items():
        monkeypatch.setenv(name, value)


def test_check_source_lists_pdfs_from_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_SOURCE_KIND", "s3")
    objects = FakeObjectStore({"papers/a.pdf": "e1", "papers/notes.txt": "e2"})
    monkeypatch.setattr(sources, "make_s3_bucket", lambda _settings: objects)

    result = CliRunner().invoke(cli.app, ["check-source"])

    assert result.exit_code == 0, result.output
    assert "s3:pdfs/papers/: 2 files, 1 PDFs" in result.output
    assert "/papers/a.pdf" in result.output


def test_check_source_reports_access_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Rejected(FakeDropboxClient):
        def files_list_folder(self, path: str, *, recursive: bool) -> Any:
            raise AuthError("req", "invalid_access_token")

    monkeypatch.setenv("ALEXANDRIA_SOURCE_KIND", "dropbox")
    monkeypatch.setattr(sources, "make_dropbox_client", lambda _settings: Rejected())

    result = CliRunner().invoke(cli.app, ["check-source"])

    assert result.exit_code == 1
    assert "dropbox:/: access failed" in result.output


def test_check_source_reports_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_SOURCE_KIND", "s3")
    monkeypatch.delenv("ALEXANDRIA_S3_BUCKET")

    result = CliRunner().invoke(cli.app, ["check-source"])

    assert result.exit_code == 2
    assert "Configuration is incomplete" in result.output
    assert "bucket" in result.output


def test_source_kind_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALEXANDRIA_SOURCE_KIND", raising=False)
    with pytest.raises(ValidationError, match="kind"):
        sources.make_source()


def test_check_source_shortens_long_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_SOURCE_KIND", "s3")
    objects = FakeObjectStore({f"papers/{n:02}.pdf": "e" for n in range(12)})
    monkeypatch.setattr(sources, "make_s3_bucket", lambda _settings: objects)

    result = CliRunner().invoke(cli.app, ["check-source"])

    assert "/papers/09.pdf" in result.output
    assert "/papers/10.pdf" not in result.output
    assert "… and 2 more" in result.output


def test_auth_prints_the_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeFlow:
        def __init__(self, app_key: str, *, use_pkce: bool, token_access_type: str) -> None:
            assert (app_key, use_pkce, token_access_type) == ("my-key", True, "offline")

        def start(self) -> str:
            return "https://dropbox.example/authorize"

        def finish(self, code: str) -> Any:
            assert code == "the-code"
            return type("Result", (), {"refresh_token": "refresh-123"})()

    monkeypatch.setattr(cli, "DropboxOAuth2FlowNoRedirect", FakeFlow)

    result = CliRunner().invoke(cli.app, ["auth", "--app-key", "my-key"], input=" the-code \n")

    assert result.exit_code == 0, result.output
    assert "https://dropbox.example/authorize" in result.output
    assert result.output.rstrip().endswith("ALEXANDRIA_DROPBOX_REFRESH_TOKEN=refresh-123")
