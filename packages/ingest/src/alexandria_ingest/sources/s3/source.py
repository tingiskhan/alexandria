"""S3Source: change detection by comparing listings."""

import json

from alexandria_core.models import FileChange
from alexandria_ingest.sources.s3.protocols import ObjectStore


class S3Source:
    """FileSource over a bucket prefix. Files are identified by object key.

    S3 has no change feed, so each run lists the prefix and compares it with the previous listing. The cursor
    is that listing as JSON ({key: etag}), which is how deletions are found.
    """

    def __init__(self, objects: ObjectStore, prefix: str) -> None:
        self._objects = objects
        self._prefix = prefix

    def changes(self, cursor: str | None) -> tuple[list[FileChange], str]:
        """New and changed objects (by ETag) and deleted ones, compared with the listing in `cursor`."""
        previous: dict[str, str] = json.loads(cursor) if cursor else {}
        current = self._objects.list_etags(self._prefix)
        changes = [
            FileChange(source_id=key, path=f"/{key}", content_hash=etag)
            for key, etag in sorted(current.items())
            if previous.get(key) != etag
        ]
        changes += [
            FileChange(source_id=key, path=f"/{key}", content_hash=None, deleted=True)
            for key in sorted(previous.keys() - current.keys())
        ]
        return changes, json.dumps(current, sort_keys=True)

    def download(self, file_id: str) -> bytes:
        """The object's bytes; `file_id` is its key."""
        return self._objects.read(file_id)
