"""FileSource over an S3 bucket prefix: RustFS, MinIO, AWS or anything else that speaks the S3 API."""

from alexandria_ingest.sources.s3.client import Boto3Bucket, make_s3_bucket
from alexandria_ingest.sources.s3.protocols import ObjectStore
from alexandria_ingest.sources.s3.settings import S3Settings
from alexandria_ingest.sources.s3.source import S3Source

__all__ = ["Boto3Bucket", "ObjectStore", "S3Settings", "S3Source", "make_s3_bucket"]
