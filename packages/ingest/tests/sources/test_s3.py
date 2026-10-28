import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import HttpWaitStrategy

from alexandria_core.models import FileChange
from alexandria_ingest.sources.s3 import S3Settings, S3Source, make_s3_bucket


@dataclass
class FakeObjectStore:
    objects: dict[str, str] = field(default_factory=dict)  # key -> etag
    listed: list[str] = field(default_factory=list)

    def list_etags(self, prefix: str) -> dict[str, str]:
        self.listed.append(prefix)
        return {k: e for k, e in self.objects.items() if k.startswith(prefix)}

    def read(self, key: str) -> bytes:
        return f"bytes of {key}".encode()


def test_first_run_reports_every_file_under_the_prefix() -> None:
    objects = FakeObjectStore({"papers/b.pdf": "e2", "papers/a.pdf": "e1", "other/x.pdf": "e9"})

    changes, cursor = S3Source(objects, "papers/").changes(None)

    assert changes == [
        FileChange(source_id="papers/a.pdf", path="/papers/a.pdf", content_hash="e1"),
        FileChange(source_id="papers/b.pdf", path="/papers/b.pdf", content_hash="e2"),
    ]
    assert json.loads(cursor) == {"papers/a.pdf": "e1", "papers/b.pdf": "e2"}
    assert objects.listed == ["papers/"]


def test_later_run_reports_only_new_changed_and_deleted_files() -> None:
    objects = FakeObjectStore({"a.pdf": "e1", "b.pdf": "e2", "c.pdf": "e3"})
    source = S3Source(objects, "")
    _, cursor = source.changes(None)

    objects.objects = {"a.pdf": "e1", "b.pdf": "e2-new", "d.pdf": "e4"}
    changes, cursor = source.changes(cursor)

    assert changes == [
        FileChange(source_id="b.pdf", path="/b.pdf", content_hash="e2-new"),
        FileChange(source_id="d.pdf", path="/d.pdf", content_hash="e4"),
        FileChange(source_id="c.pdf", path="/c.pdf", content_hash=None, deleted=True),
    ]
    assert source.changes(cursor)[0] == []


def test_empty_bucket() -> None:
    assert S3Source(FakeObjectStore(), "").changes(None) == ([], "{}")


def test_download_reads_the_object() -> None:
    assert S3Source(FakeObjectStore(), "").download("papers/a.pdf") == b"bytes of papers/a.pdf"


RUSTFS_IMAGE = "rustfs/rustfs:1.0.3"
ACCESS_KEY, SECRET_KEY = "alexandria", "alexandria-secret"


@pytest.fixture(scope="module")
def rustfs() -> Iterator[S3Settings]:
    container = (
        DockerContainer(RUSTFS_IMAGE)
        .with_env("RUSTFS_ACCESS_KEY", ACCESS_KEY)
        .with_env("RUSTFS_SECRET_KEY", SECRET_KEY)
        .with_exposed_ports(9000)
        .waiting_for(HttpWaitStrategy(9000, "/health").for_status_code(200))
    )
    with container:
        yield S3Settings(
            endpoint_url=f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}",
            bucket="pdfs",
            access_key_id=ACCESS_KEY,
            secret_access_key=SECRET_KEY,
        )


@pytest.mark.integration
def test_against_rustfs(rustfs: S3Settings) -> None:
    client: Any = boto3.client(  # sets up the bucket the way a user would
        "s3", endpoint_url=rustfs.endpoint_url, aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY
    )
    client.create_bucket(Bucket="pdfs")
    client.put_object(Bucket="pdfs", Key="papers/", Body=b"")
    client.put_object(Bucket="pdfs", Key="papers/a b.pdf", Body=b"%PDF-1")
    source = S3Source(make_s3_bucket(rustfs), "papers/")

    changes, cursor = source.changes(None)
    assert [(c.source_id, c.deleted) for c in changes] == [("papers/a b.pdf", False)]
    assert source.download("papers/a b.pdf") == b"%PDF-1"

    client.put_object(Bucket="pdfs", Key="papers/a b.pdf", Body=b"%PDF-2")
    client.put_object(Bucket="pdfs", Key="papers/c.pdf", Body=b"%PDF-3")
    changes, cursor = source.changes(cursor)
    assert [c.source_id for c in changes] == ["papers/a b.pdf", "papers/c.pdf"]

    client.delete_object(Bucket="pdfs", Key="papers/c.pdf")
    changes, _ = source.changes(cursor)
    assert [(c.source_id, c.deleted) for c in changes] == [("papers/c.pdf", True)]
