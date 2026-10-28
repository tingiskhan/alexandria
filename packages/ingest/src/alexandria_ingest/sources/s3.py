"""FileSource over an S3 bucket prefix: RustFS, MinIO, AWS or anything else that speaks the S3 API."""

import json
from typing import Any, Protocol

import boto3
from botocore.config import Config
from pydantic import SecretStr
from pydantic_settings import BaseSettings

from alexandria_core.models import FileChange
from alexandria_core.settings import settings_config


class S3Settings(BaseSettings):
    """Bucket, prefix and credentials (ALEXANDRIA_S3_*)."""

    model_config = settings_config("ALEXANDRIA_S3_")

    endpoint_url: str | None = None  # e.g. http://rustfs:9000; None means AWS
    bucket: str
    prefix: str = ""  # sync only keys under this prefix, e.g. "papers/"
    access_key_id: str
    secret_access_key: SecretStr
    region: str = "us-east-1"


class ObjectStore(Protocol):
    """The objects of one bucket, so tests can pass a fake."""

    def list_etags(self, prefix: str) -> dict[str, str]:
        """Every object key under `prefix`, mapped to its ETag. Folder markers are left out."""
        ...

    def read(self, key: str) -> bytes:
        """The object's bytes."""
        ...


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


class Boto3Bucket:
    """ObjectStore over one bucket. boto3 only takes AWS's parameter names (Bucket, Key, Prefix); they stay in here."""

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def list_etags(self, prefix: str) -> dict[str, str]:
        """Every object key under `prefix`, mapped to its ETag. Folder markers are left out."""
        pages = self._client.get_paginator("list_objects_v2").paginate(Bucket=self._bucket, Prefix=prefix)
        return {
            obj["Key"]: obj["ETag"].strip('"')
            for page in pages
            for obj in page.get("Contents", [])
            if not obj["Key"].endswith("/")  # folder markers
        }

    def read(self, key: str) -> bytes:
        """The object's bytes."""
        content: bytes = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        return content


def make_s3_bucket(settings: S3Settings) -> Boto3Bucket:
    """A boto3-backed bucket for the configured endpoint and credentials."""
    client = boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key.get_secret_value(),
        region_name=settings.region,
        # Self-hosted stores like RustFS serve buckets by path, not as subdomains.
        config=Config(s3={"addressing_style": "path"}),
    )
    return Boto3Bucket(client, settings.bucket)
