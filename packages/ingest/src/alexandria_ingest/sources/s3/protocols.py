"""What S3Source needs from a bucket, so tests can pass a fake."""

from typing import Protocol


class ObjectStore(Protocol):
    """The objects of one bucket, so tests can pass a fake."""

    def list_etags(self, prefix: str) -> dict[str, str]:
        """Every object key under `prefix`, mapped to its ETag. Folder markers are left out."""
        ...

    def read(self, key: str) -> bytes:
        """The object's bytes."""
        ...
