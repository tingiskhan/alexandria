from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from prefect.testing.utilities import prefect_test_harness

from alexandria_core.db import EMBEDDING_DIMENSION
from alexandria_core.models import Document, EmbeddedChunk, FileChange
from alexandria_ingest.sources import ConfiguredSource
from alexandria_workflows import flows

from .fakes import FakeEmbedder, FakeExtractor, FakeFileSource, InMemoryStore


@pytest.fixture(scope="module", autouse=True)
def prefect_api() -> Iterator[None]:
    with prefect_test_harness():
        yield


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> flows.Pipeline:
    """Replace the composition root's real adapters with fakes; the flow and task wiring stays real."""
    source = FakeFileSource(
        files={"/good.pdf": b"# Intro\n\nHello", "/bad.pdf": b"broken"},
        batches=[
            [
                FileChange(source_id="/good.pdf", path="/good.pdf", content_hash="h1"),
                FileChange(source_id="/bad.pdf", path="/bad.pdf", content_hash="h1"),
            ]
        ],
    )
    pipeline = flows.Pipeline(
        source_name="dropbox:/",
        source=source,
        extractor=FakeExtractor(),
        embedder=FakeEmbedder(),
        store=InMemoryStore(),
        max_tokens=500,
        formats=frozenset({"pdf"}),
    )

    monkeypatch.setattr(flows, "build_pipeline", lambda: pipeline)
    for step in (flows.list_changes_task, flows.triage_task, flows.prepare_file_task, flows.store_document_task):
        monkeypatch.setattr(step, "retries", 0)
    return pipeline


def test_sync_flow_completes_with_failures_recorded(pipeline: flows.Pipeline) -> None:
    state = flows.sync_flow(return_state=True)

    assert state.is_completed()
    assert state.result() == 1
    assert pipeline.store.content_hash("/good.pdf") == "h1"
    assert pipeline.store.failed_documents() == [FileChange(source_id="/bad.pdf", path="/bad.pdf", content_hash="h1")]
    assert pipeline.store.get_cursor("dropbox:/") == "cursor-1"


def test_reindex_flow_reembeds_stored_documents(pipeline: flows.Pipeline) -> None:
    flows.sync_flow()

    flows.reindex_flow()

    assert [d.path for d in pipeline.store.list_documents()] == ["/good.pdf"]


def retry_once(monkeypatch: pytest.MonkeyPatch, step: Any) -> None:
    monkeypatch.setattr(step, "retries", 1)
    monkeypatch.setattr(step, "retry_delay_seconds", 0)


def test_failed_upsert_is_retried_without_preparing_again(
    pipeline: flows.Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    retry_once(monkeypatch, flows.store_document_task)
    store = cast("InMemoryStore", pipeline.store)
    upsert, attempts = store.upsert_document, []

    def flaky_upsert(document: Document, chunks: list[EmbeddedChunk]) -> None:
        attempts.append(document.path)
        if len(attempts) == 1:
            raise ConnectionError("the database system is not yet accepting connections")
        upsert(document, chunks)

    monkeypatch.setattr(store, "upsert_document", flaky_upsert)

    assert flows.sync_flow() == 1  # only /bad.pdf

    assert attempts == ["/good.pdf", "/good.pdf"]
    assert cast("FakeFileSource", pipeline.source).downloads == ["/good.pdf", "/bad.pdf"]  # Docling ran once each
    assert store.content_hash("/good.pdf") == "h1"


def test_failed_listing_is_retried(pipeline: flows.Pipeline, monkeypatch: pytest.MonkeyPatch) -> None:
    retry_once(monkeypatch, flows.list_changes_task)
    source = cast("FakeFileSource", pipeline.source)
    changes, attempts = source.changes, []

    def flaky_changes(cursor: str | None) -> tuple[list[FileChange], str]:
        attempts.append(cursor)
        if len(attempts) == 1:
            raise ConnectionError("RustFS is restarting")
        return changes(cursor)

    monkeypatch.setattr(source, "changes", flaky_changes)

    state = flows.sync_flow(return_state=True)

    assert state.is_completed()
    assert attempts == [None, None]
    assert pipeline.store.content_hash("/good.pdf") == "h1"


def test_other_formats_are_skipped_without_preparing(pipeline: flows.Pipeline) -> None:
    source = cast("FakeFileSource", pipeline.source)
    source.batches = [[FileChange(source_id="/notes.txt", path="/notes.txt", content_hash="h1")]]

    assert flows.sync_flow() == 0
    assert source.downloads == []


class StubEmbedder:
    dimension = EMBEDDING_DIMENSION

    def __init__(self, model: str, revision: str, batch_size: int = 32) -> None:
        self.model_name = model


@pytest.fixture
def real_wiring(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Undo the `pipeline` fixture's shortcut and stub only the heavy adapters, recording the startup steps."""
    steps: list[str] = []
    monkeypatch.setenv("ALEXANDRIA_DATABASE_URL", "postgresql+psycopg://user:secret@db/alexandria")
    monkeypatch.setenv("ALEXANDRIA_CHUNK_MAX_TOKENS", "300")
    monkeypatch.setenv("ALEXANDRIA_EXTRACT_FORMATS", "pdf,docx")
    source = FakeFileSource()
    monkeypatch.setattr(flows, "make_source", lambda: ConfiguredSource("s3:pdfs/", source))
    monkeypatch.setattr(flows, "make_engine", lambda url: steps.append(f"engine {url}") or "engine")
    monkeypatch.setattr(flows, "wait_for_database", lambda engine: steps.append("wait"))
    monkeypatch.setattr(flows, "init_schema", lambda engine: steps.append("schema"))
    monkeypatch.setattr(flows, "SentenceTransformerEmbedder", StubEmbedder)
    monkeypatch.setattr(flows, "DoclingExtractor", FakeExtractor)
    return steps


def test_build_pipeline_wires_settings_and_adapters(real_wiring: list[str]) -> None:
    pipeline = flows.build_pipeline()

    assert real_wiring == ["engine postgresql+psycopg://user:secret@db/alexandria", "wait", "schema"]
    assert pipeline.source_name == "s3:pdfs/|docx,pdf"  # a new format set starts from a full listing
    assert pipeline.max_tokens == 300
    assert pipeline.formats == frozenset({"pdf", "docx"})
    assert pipeline.embedder.model_name == "BAAI/bge-m3"
    assert isinstance(pipeline.extractor, FakeExtractor)
    assert pipeline.extractor.formats == frozenset({"pdf", "docx"})


def test_build_pipeline_refuses_a_model_that_does_not_fit_the_schema(
    real_wiring: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(StubEmbedder, "dimension", 768)

    with pytest.raises(ValueError, match="768 dimensions; the schema has 1024"):
        flows.build_pipeline()


def serve(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    served: list[Any] = []
    monkeypatch.setattr(flows, "serve", lambda *deployments: served.extend(deployments))
    flows.serve_command()
    return served


def test_serve_runs_both_deployments_one_run_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALEXANDRIA_SYNC_INTERVAL", raising=False)

    served = serve(monkeypatch)

    assert [d.name for d in served] == ["sync", "reindex"]
    assert all(d.schedules == [] for d in served)
    assert all(d.concurrency_limit == 1 for d in served)


def test_an_interval_schedules_sync_but_not_reindex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_SYNC_INTERVAL", "30m")

    sync, reindex = serve(monkeypatch)

    assert [s.schedule.interval for s in sync.schedules] == [timedelta(minutes=30)]
    assert reindex.schedules == []
