"""File sources: adapters that implement alexandria_core.protocols.FileSource, one module per backend.

To add a backend: write a module here with a `<Name>Settings` class (prefix ALEXANDRIA_<NAME>_), a source class
with `changes(cursor)` and `download(file_id)`, and a factory that builds its client. Then add its name to
`SourceSettings.kind` and a case to `make_source`.
"""

from dataclasses import dataclass

from alexandria_core.protocols import FileSource
from alexandria_ingest.settings import SourceSettings
from alexandria_ingest.sources.dropbox import DropboxSettings, DropboxSource, make_dropbox_client
from alexandria_ingest.sources.s3 import S3Settings, S3Source, make_s3_bucket
from alexandria_ingest.sources.sharepoint import SharePointSettings, SharePointSource, make_sharepoint_client


@dataclass(frozen=True)
class ConfiguredSource:
    """The source to sync from, and the name its sync cursor is stored under."""

    name: str  # keys the sync cursor, so a different bucket or folder starts from scratch
    source: FileSource


def make_source() -> ConfiguredSource:
    """Build the source chosen by ALEXANDRIA_SOURCE_KIND. Only that backend's settings are loaded."""
    match SourceSettings().kind:
        case "dropbox":
            dropbox = DropboxSettings()
            return ConfiguredSource(
                f"dropbox:{dropbox.path or '/'}", DropboxSource(make_dropbox_client(dropbox), dropbox.path)
            )
        case "s3":
            s3 = S3Settings()
            return ConfiguredSource(f"s3:{s3.bucket}/{s3.prefix}", S3Source(make_s3_bucket(s3), s3.prefix))
        case "sharepoint":
            sharepoint = SharePointSettings()
            drive = sharepoint.drive_id or f"{sharepoint.site}/{sharepoint.library}"
            return ConfiguredSource(
                f"sharepoint:{drive}{sharepoint.path or '/'}",
                SharePointSource(make_sharepoint_client(sharepoint), sharepoint.path),
            )
