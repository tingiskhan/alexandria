"""The pipeline against a real Postgres: the same file under two paths is searched once."""

from collections.abc import Iterator, Sequence
from functools import partial

import pytest
from sqlalchemy import Engine
from testcontainers.community.postgres import PostgresContainer

from alexandria_core.db import EMBEDDING_DIMENSION, make_engine
from alexandria_core.migrate import migrate
from alexandria_core.models import FileChange, SearchQuery
from alexandria_core.store import PgStore
from alexandria_ingest.metadata import MetadataReader
from alexandria_ingest.pipeline import process_file, sync_changes

from .fakes import FakeEmbedder, FakeExtractor, FakeFileSource

pytestmark = pytest.mark.integration

SOURCE = "s3:library/|pdf"
PAPER = b"# Widgets Revisited\n\nWidgets work, mostly.\f# Results\n\nThey work in 9 of 10 trials."


class WideEmbedder(FakeEmbedder):
    """FakeEmbedder padded to the schema's dimension. Every text gets the same direction, which is all search needs."""

    dimension = EMBEDDING_DIMENSION

    def __init__(self) -> None:
        self.calls = 0

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    with PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as postgres:
        engine = make_engine(postgres.get_connection_url())
        migrate(engine)
        yield engine
        engine.dispose()


def sync(source: FakeFileSource, store: PgStore, embedder: WideEmbedder) -> list[FileChange]:
    process = partial(
        process_file,
        source=source,
        extractor=FakeExtractor(),
        embedder=embedder,
        store=store,
        max_tokens=500,
        formats=frozenset({"pdf"}),
        metadata_reader=MetadataReader(),
    )
    return sync_changes(SOURCE, source.changes, store, process)


def search(store: PgStore, text: str) -> set[str]:
    query = SearchQuery(text=text, embedding=WideEmbedder().embed_query(text), model_name="fake-embedder")
    return {hit.path for hit in store.search(query)}


def test_the_same_pdf_under_two_paths_yields_one_searchable_document(engine: Engine) -> None:
    store, embedder = PgStore(engine), WideEmbedder()
    original = FileChange(source_id="papers/widgets.pdf", path="/papers/widgets.pdf", content_hash="etag-1")
    # Another upload of the same file: S3 gives it another ETag (e.g. multipart), so content_hash can't match it.
    copy = FileChange(source_id="inbox/widgets (1).pdf", path="/inbox/widgets (1).pdf", content_hash="etag-2-3")
    source = FakeFileSource(
        files={original.source_id: PAPER, copy.source_id: PAPER},
        batches=[[original, copy], [FileChange(original.source_id, original.path, None, deleted=True)], []],
    )

    assert sync(source, store, embedder) == []

    assert embedder.calls == 1  # the copy was not embedded
    assert search(store, "trials") == {"/papers/widgets.pdf"}
    listed = {d.path: d for d in store.list_documents()}
    assert listed["/inbox/widgets (1).pdf"].duplicate_of == listed["/papers/widgets.pdf"].id

    # Deleting the original releases the copy, and the next sync makes it searchable.
    sync(source, store, embedder)
    assert search(store, "trials") == set()
    sync(source, store, embedder)
    assert search(store, "trials") == {"/inbox/widgets (1).pdf"}
    [promoted] = store.list_documents()
    assert promoted.duplicate_of is None
