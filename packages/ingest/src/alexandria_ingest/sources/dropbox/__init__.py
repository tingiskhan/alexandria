"""FileSource over a Dropbox folder, via the official SDK and an offline refresh token (`alexandria-ingest auth`)."""

from alexandria_ingest.sources.dropbox.client import make_dropbox_client
from alexandria_ingest.sources.dropbox.protocols import DropboxClient
from alexandria_ingest.sources.dropbox.settings import DropboxSettings
from alexandria_ingest.sources.dropbox.source import DropboxSource

__all__ = ["DropboxClient", "DropboxSettings", "DropboxSource", "make_dropbox_client"]
