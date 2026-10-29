"""DropboxSource: change detection with Dropbox's own cursor."""

import logging

from dropbox.exceptions import ApiError
from dropbox.files import DeletedMetadata, FileMetadata

from alexandria_core.models import FileChange
from alexandria_ingest.sources.dropbox.protocols import DropboxClient

log = logging.getLogger(__name__)


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
