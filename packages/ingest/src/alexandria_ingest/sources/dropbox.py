"""FileSource over a Dropbox folder, via the official SDK and an offline refresh token (`alexandria-ingest auth`)."""

import logging
from typing import Any, Protocol

import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import DeletedMetadata, FileMetadata
from pydantic import SecretStr
from pydantic_settings import BaseSettings

from alexandria_core.models import FileChange
from alexandria_core.settings import settings_config

log = logging.getLogger(__name__)


class DropboxSettings(BaseSettings):
    """Dropbox app and folder (ALEXANDRIA_DROPBOX_*)."""

    model_config = settings_config("ALEXANDRIA_DROPBOX_")

    app_key: str
    refresh_token: SecretStr  # from `alexandria-ingest auth`; valid until the app is revoked
    path: str = ""  # folder to sync, starting with "/"; "" is the app folder root


class DropboxClient(Protocol):
    """The part of dropbox.Dropbox this module uses, so tests can pass a fake."""

    def files_list_folder(self, path: str, *, recursive: bool) -> Any:
        """List a folder; returns a `ListFolderResult`."""
        ...

    def files_list_folder_continue(self, cursor: str) -> Any:
        """Continue a listing from a cursor; returns a `ListFolderResult`."""
        ...

    def files_download(self, path: str) -> Any:
        """Download a file; returns `(FileMetadata, requests.Response)`."""
        ...


class DropboxSource:
    """FileSource over a Dropbox folder. Files are identified by path_lower, because deletions only carry a path."""

    def __init__(self, client: DropboxClient, path: str) -> None:
        self._client = client
        self._path = path

    def changes(self, cursor: str | None) -> tuple[list[FileChange], str]:
        """Files added, changed or deleted since `cursor` (everything, recursively, when it is None).

        Uses Dropbox's own cursor. A reset cursor falls back to a full listing; unchanged files are then
        skipped by content hash.
        """
        if cursor is None:
            result = self._client.files_list_folder(self._path, recursive=True)
        else:
            try:
                result = self._client.files_list_folder_continue(cursor)
            except ApiError as error:
                if not error.error.is_reset():
                    raise
                # The cursor was invalidated: list everything again. Content hashes skip unchanged files.
                log.warning("Dropbox cursor was reset; doing a full listing of %r", self._path)
                result = self._client.files_list_folder(self._path, recursive=True)

        entries = list(result.entries)
        while result.has_more:
            result = self._client.files_list_folder_continue(result.cursor)
            entries.extend(result.entries)

        changes = [
            FileChange(
                source_id=entry.path_lower,
                path=entry.path_display,
                content_hash=entry.content_hash if isinstance(entry, FileMetadata) else None,
                deleted=isinstance(entry, DeletedMetadata),
            )
            for entry in entries
            if isinstance(entry, FileMetadata | DeletedMetadata)
        ]
        cursor_out: str = result.cursor
        return changes, cursor_out

    def download(self, file_id: str) -> bytes:
        """The file's bytes; `file_id` is its path_lower."""
        _, response = self._client.files_download(file_id)
        content: bytes = response.content
        return content


def make_dropbox_client(settings: DropboxSettings) -> dropbox.Dropbox:
    """A client that refreshes its short-lived access token from the offline (PKCE) refresh token."""
    return dropbox.Dropbox(oauth2_refresh_token=settings.refresh_token.get_secret_value(), app_key=settings.app_key)
