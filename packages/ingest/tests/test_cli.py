from typing import Any

import pytest
from dropbox.exceptions import AuthError
from pydantic import ValidationError
from typer.testing import CliRunner

from alexandria_core.models import Document, DocumentMetadata, Page
from alexandria_ingest import cli, sources
from alexandria_ingest.sources.sharepoint import GraphError

from .fakes import InMemoryStore
from .sources.test_dropbox import FakeDropboxClient
from .sources.test_s3 import FakeObjectStore
from .sources.test_sharepoint import ROOT, FakeGraph, file, page


@pytest.fixture(autouse=True)
def source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "ALEXANDRIA_S3_BUCKET": "pdfs",
        "ALEXANDRIA_S3_PREFIX": "papers/",
        "ALEXANDRIA_S3_ACCESS_KEY_ID": "id",
        "ALEXANDRIA_S3_SECRET_ACCESS_KEY": "secret",
        "ALEXANDRIA_DROPBOX_APP_KEY": "key",
        "ALEXANDRIA_DROPBOX_REFRESH_TOKEN": "token",
        "ALEXANDRIA_SHAREPOINT_TENANT_ID": "tenant",
        "ALEXANDRIA_SHAREPOINT_CLIENT_ID": "app",
        "ALEXANDRIA_SHAREPOINT_CLIENT_SECRET": "secret",
        "ALEXANDRIA_SHAREPOINT_SITE": "contoso.sharepoint.com:/sites/Finance",
    }.items():
        monkeypatch.setenv(name, value)


def test_check_source_lists_the_files_to_ingest_from_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_SOURCE_KIND", "s3")
    monkeypatch.setenv("ALEXANDRIA_EXTRACT_FORMATS", "pdf,docx")
    objects = FakeObjectStore(
        {"papers/a.pdf": "e1", "papers/b.PDF": "e2", "papers/plan.docx": "e3", "papers/notes.txt": "e4"}
    )
    monkeypatch.setattr(sources, "make_s3_bucket", lambda _settings: objects)

    result = CliRunner().invoke(cli.app, ["check-source"])

    assert result.exit_code == 0, result.output
    assert "s3:pdfs/papers/: 4 files, 3 to ingest" in result.output
    assert "  1 docx, 2 pdf" in result.output
    assert "/papers/plan.docx" in result.output
    assert "/papers/notes.txt" not in result.output


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


def test_check_source_reports_unknown_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_SOURCE_KIND", "s3")
    monkeypatch.setenv("ALEXANDRIA_EXTRACT_FORMATS", "pdf,pfd")

    result = CliRunner().invoke(cli.app, ["check-source"])

    assert result.exit_code == 2
    assert "cannot read 'pfd'" in result.output


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


def test_check_source_lists_the_files_to_ingest_from_sharepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_SOURCE_KIND", "sharepoint")
    graph = FakeGraph(pages={None: page(ROOT, file("f1", "Plan.docx"), file("f2", "logo.svg"))})
    monkeypatch.setattr(sources, "make_sharepoint_client", lambda _settings: graph)

    result = CliRunner().invoke(cli.app, ["check-source"])

    assert result.exit_code == 0, result.output
    assert "sharepoint:contoso.sharepoint.com:/sites/Finance/Documents/: 2 files, 1 to ingest" in result.output
    assert "/Plan.docx" in result.output


def test_check_source_reports_sharepoint_access_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Denied(FakeGraph):
        def delta_page(self, link: str | None) -> Any:
            raise GraphError("HTTP 403 accessDenied: Either scp or roles claim need to be present in the token.")

    monkeypatch.setenv("ALEXANDRIA_SOURCE_KIND", "sharepoint")
    monkeypatch.setattr(sources, "make_sharepoint_client", lambda _settings: Denied())

    result = CliRunner().invoke(cli.app, ["check-source"])

    assert result.exit_code == 1
    assert "access failed: HTTP 403 accessDenied" in result.output


class FakeLibraries:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def libraries(self) -> list[tuple[str, str]]:
        if self.error:
            raise self.error
        return [("Documents", "b!doc"), ("Site Assets", "b!assets")]


def test_sharepoint_libraries_lists_names_and_drive_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "make_sharepoint_client", lambda _settings: FakeLibraries())

    result = CliRunner().invoke(cli.app, ["sharepoint-libraries"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["Documents\tb!doc", "Site Assets\tb!assets"]


def test_sharepoint_libraries_reports_access_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    error = GraphError("Sign-in failed: AADSTS7000215")
    monkeypatch.setattr(cli, "make_sharepoint_client", lambda _settings: FakeLibraries(error))

    result = CliRunner().invoke(cli.app, ["sharepoint-libraries"])

    assert result.exit_code == 1
    assert "contoso.sharepoint.com:/sites/Finance: access failed: Sign-in failed" in result.output


def test_sharepoint_libraries_reports_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALEXANDRIA_SHAREPOINT_CLIENT_SECRET")

    result = CliRunner().invoke(cli.app, ["sharepoint-libraries"])

    assert result.exit_code == 2
    assert "client_secret" in result.output


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch) -> tuple[InMemoryStore, list[object]]:
    """A store with one paper titled after its file and one document whose metadata is already right."""
    store = InMemoryStore()
    paper = (Page(1, "# Widgets Revisited\n\nJane Doe\n\n© 2020"), Page(2, "More"))
    store.upsert_document(Document("p", "/papers/w.pdf", DocumentMetadata(title="w"), "h", paper), [])
    store.upsert_document(Document("n", "/notes.md", DocumentMetadata(title="notes"), "h", (Page(1, "x"),)), [])
    schema_inits: list[object] = []
    monkeypatch.setenv("ALEXANDRIA_DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("ALEXANDRIA_METADATA_CROSSREF", "false")
    monkeypatch.setattr(cli, "migrate", schema_inits.append)
    monkeypatch.setattr(cli, "PgStore", lambda _engine: store)
    return store, schema_inits


WIDGETS = DocumentMetadata(title="Widgets Revisited", authors=("Jane Doe",), year=2020, doc_type="paper")


def test_backfill_metadata_rereads_stored_first_pages(library: tuple[InMemoryStore, list[object]]) -> None:
    store, schema_inits = library

    result = CliRunner().invoke(cli.app, ["backfill-metadata"])

    assert result.exit_code == 0, result.output
    assert len(schema_inits) == 1
    assert result.stdout == "1\t/papers/w.pdf\tWidgets Revisited\tJane Doe\t2020\t-\tpaper\n"
    assert "changed the metadata of 1 of 2 documents" in result.stderr
    assert store.documents["p"].doc.metadata == WIDGETS
    assert store.documents["n"].doc.metadata == DocumentMetadata(title="notes")


def test_backfill_metadata_dry_run_stores_nothing(library: tuple[InMemoryStore, list[object]]) -> None:
    store, _ = library

    result = CliRunner().invoke(cli.app, ["backfill-metadata", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "would change the metadata of 1 of 2 documents" in result.stderr
    assert store.documents["p"].doc.metadata == DocumentMetadata(title="w")


def test_backfill_metadata_reports_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CliRunner().invoke(cli.app, ["backfill-metadata"])

    assert result.exit_code == 2
    assert "Configuration is incomplete" in result.output


def test_describe_lists_every_field() -> None:
    metadata = DocumentMetadata(title="T", authors=("A B", "C D"), year=1999, doi="10.1/x", doc_type="book")

    assert cli.describe(metadata) == "T\tA B; C D\t1999\t10.1/x\tbook"


@pytest.fixture
def copies(monkeypatch: pytest.MonkeyPatch) -> tuple[InMemoryStore, list[object]]:
    """Two copies of a paper from before duplicates were detected, a third detected on ingest, and another paper."""
    store = InMemoryStore()
    paper = (Page(1, "# Widgets\n\nThey work."),)
    for source_id in ("a", "b", "c"):
        store.upsert_document(Document(source_id, f"/{source_id}.pdf", DocumentMetadata(title="w"), "h", paper), [])
    store.upsert_document(Document("d", "/d.pdf", DocumentMetadata(title="d"), "h", (Page(1, "Other"),)), [])
    for key in ("a", "b"):  # ingested by an older release: no digest, and both searched
        store.documents[key].digest = None
        store.documents[key].duplicate_of = None
    schema_inits: list[object] = []
    monkeypatch.setenv("ALEXANDRIA_DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setattr(cli, "migrate", schema_inits.append)
    monkeypatch.setattr(cli, "PgStore", lambda _engine: store)
    return store, schema_inits


def test_duplicates_lists_copies_and_stores_missing_digests(copies: tuple[InMemoryStore, list[object]]) -> None:
    store, schema_inits = copies

    result = CliRunner().invoke(cli.app, ["duplicates"])

    assert result.exit_code == 0, result.output
    assert len(schema_inits) == 1
    assert result.stdout == "2\t/b.pdf\t1\t/a.pdf\tsearched\n3\t/c.pdf\t1\t/a.pdf\tnot searched\n"
    assert "2 copies of 1 documents among 4; 1 of the copies are searched" in result.stderr
    assert store.documents["a"].digest == store.documents["c"].digest


def test_duplicates_reports_missing_settings() -> None:
    result = CliRunner().invoke(cli.app, ["duplicates"])

    assert result.exit_code == 2
    assert "Configuration is incomplete" in result.output
