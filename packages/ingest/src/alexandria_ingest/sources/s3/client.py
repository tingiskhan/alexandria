"""ObjectStore over boto3."""

from typing import Any

import boto3
from botocore.config import Config

from alexandria_ingest.sources.s3.settings import S3Settings


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
