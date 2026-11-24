"""What DropboxSource needs from the Dropbox SDK, so tests can pass a fake."""

from typing import Any, Protocol


class DropboxClient(Protocol):
    """The part of dropbox.Dropbox the source uses, so tests can pass a fake."""

    def files_list_folder(self, path: str, *, recursive: bool) -> Any:
        """List a folder; returns a `ListFolderResult`."""
        ...

    def files_list_folder_continue(self, cursor: str) -> Any:
        """Continue a listing from a cursor; returns a `ListFolderResult`."""
        ...

    def files_download(self, path: str) -> Any:
        """Download a file; returns `(FileMetadata, requests.Response)`."""
        ...
