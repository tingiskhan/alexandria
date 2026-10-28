from dataclasses import dataclass, field
from typing import Any

import dropbox
import pytest
from dropbox.exceptions import ApiError
from dropbox.files import DeletedMetadata, FileMetadata, FolderMetadata, ListFolderContinueError
from pydantic import SecretStr, ValidationError

from alexandria_core.models import FileChange
from alexandria_ingest.sources.dropbox import DropboxSettings, DropboxSource, make_dropbox_client


@dataclass
class Result:
    entries: list[Any]
    cursor: str
    has_more: bool = False


@dataclass
class Response:
    content: bytes


@dataclass
class FakeDropboxClient:
    """Answers list_folder with `listing` pages and list_folder_continue from `continued`, keyed by cursor."""

    listing: list[Result] = field(default_factory=list)
    continued: dict[str, Result] = field(default_factory=dict)
    reset_cursors: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def files_list_folder(self, path: str, *, recursive: bool) -> Any:
        self.calls.append(f"list:{path}:{recursive}")
        return self.listing[0]

    def files_list_folder_continue(self, cursor: str) -> Any:
        self.calls.append(f"continue:{cursor}")
        if cursor in self.reset_cursors:
            raise ApiError("req", ListFolderContinueError.reset, None, None)
        return self.continued[cursor]

    def files_download(self, path: str) -> Any:
        return object(), Response(content=f"bytes of {path}".encode())


H1, H2 = "1" * 64, "2" * 64


def file(path: str, content_hash: str = H1) -> FileMetadata:
    return FileMetadata(
        name=path.rsplit("/", 1)[-1], path_lower=path.lower(), path_display=path, content_hash=content_hash
    )


def test_first_run_lists_recursively_and_follows_has_more() -> None:
    client = FakeDropboxClient(
        listing=[Result(entries=[file("/A.pdf")], cursor="c1", has_more=True)],
        continued={
            "c1": Result(entries=[FolderMetadata(name="sub", path_lower="/sub", path_display="/sub")], cursor="c2")
        },
    )

    changes, cursor = DropboxSource(client, "").changes(None)

    assert changes == [FileChange(source_id="/a.pdf", path="/A.pdf", content_hash=H1)]
    assert cursor == "c2"
    assert client.calls == ["list::True", "continue:c1"]


def test_later_run_continues_from_cursor_and_maps_deletions() -> None:
    deleted = DeletedMetadata(name="Old.pdf", path_lower="/old.pdf", path_display="/Old.pdf")
    client = FakeDropboxClient(continued={"c1": Result(entries=[deleted, file("/New.pdf", H2)], cursor="c2")})

    changes, cursor = DropboxSource(client, "/papers").changes("c1")

    assert changes == [
        FileChange(source_id="/old.pdf", path="/Old.pdf", content_hash=None, deleted=True),
        FileChange(source_id="/new.pdf", path="/New.pdf", content_hash=H2),
    ]
    assert cursor == "c2"


def test_reset_cursor_falls_back_to_full_listing() -> None:
    client = FakeDropboxClient(listing=[Result(entries=[file("/A.pdf")], cursor="fresh")], reset_cursors={"stale"})

    changes, cursor = DropboxSource(client, "/papers").changes("stale")

    assert [c.source_id for c in changes] == ["/a.pdf"]
    assert cursor == "fresh"
    assert client.calls == ["continue:stale", "list:/papers:True"]


def test_other_api_errors_propagate() -> None:
    class Failing(FakeDropboxClient):
        def files_list_folder_continue(self, cursor: str) -> Any:
            raise ApiError("req", ListFolderContinueError.other, None, None)

    with pytest.raises(ApiError):
        DropboxSource(Failing(), "").changes("c1")


def test_download_returns_content() -> None:
    assert DropboxSource(FakeDropboxClient(), "").download("/a.pdf") == b"bytes of /a.pdf"


def test_settings_require_app_key_and_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ["APP_KEY", "REFRESH_TOKEN", "PATH"]:
        monkeypatch.delenv(f"ALEXANDRIA_DROPBOX_{name}", raising=False)

    with pytest.raises(ValidationError) as error:
        DropboxSettings()

    assert {e["loc"] for e in error.value.errors()} == {("app_key",), ("refresh_token",)}

    monkeypatch.setenv("ALEXANDRIA_DROPBOX_APP_KEY", "key")
    monkeypatch.setenv("ALEXANDRIA_DROPBOX_REFRESH_TOKEN", "secret-token")
    settings = DropboxSettings()
    assert settings.refresh_token.get_secret_value() == "secret-token"
    assert "secret-token" not in repr(settings)


def test_client_authenticates_with_the_refresh_token() -> None:
    settings = DropboxSettings(app_key="key", refresh_token=SecretStr("token"))

    client = make_dropbox_client(settings)

    assert isinstance(client, dropbox.Dropbox)
