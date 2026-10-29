"""Prefect wiring. The only module that imports Prefect; all logic lives in alexandria_ingest."""

from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Annotated, cast

import typer
from prefect import flow, serve, task
from prefect.cache_policies import NO_CACHE
from prefect.client.orchestration import get_client
from prefect.client.schemas.actions import WorkPoolCreate
from prefect.deployments import deploy
from prefect.exceptions import ObjectAlreadyExists
from prefect.types.entrypoint import EntrypointType
from prefect.workers.process import ProcessWorker

from alexandria_core.db import EMBEDDING_DIMENSION, init_schema, make_engine, wait_for_database
from alexandria_core.models import DocumentSummary, FileChange
from alexandria_core.protocols import DocumentStore, Embedder, Extractor, FileSource
from alexandria_core.settings import DatabaseSettings
from alexandria_core.store import PgStore
from alexandria_embed.embedder import SentenceTransformerEmbedder
from alexandria_embed.settings import EmbeddingSettings
from alexandria_ingest.extract import DoclingExtractor
from alexandria_ingest.pipeline import (
    IndexedDocument,
    Outcome,
    cursor_key,
    prepare_file,
    reindex_document,
    store_document,
    sync_changes,
    triage,
)
from alexandria_ingest.settings import ChunkSettings, ExtractSettings
from alexandria_ingest.sources import make_source
from alexandria_workflows.settings import WorkflowSettings

if TYPE_CHECKING:
    from uuid import UUID

    from prefect.deployments.runner import RunnerDeployment


@dataclass(frozen=True)
class Pipeline:
    """Everything the flows need, built once per run by `build_pipeline`."""

    source_name: str
    source: FileSource
    extractor: Extractor
    embedder: Embedder
    store: DocumentStore
    max_tokens: int
    formats: frozenset[str]  # file extensions to ingest


def build_pipeline() -> Pipeline:
    """Composition root for the ingest job. Settings are loaded first, so missing config fails before any work."""
    database, embedding, chunk, extract = DatabaseSettings(), EmbeddingSettings(), ChunkSettings(), ExtractSettings()
    source = make_source()
    engine = make_engine(database.url.get_secret_value())
    wait_for_database(engine)
    init_schema(engine)
    embedder = SentenceTransformerEmbedder(embedding.model, embedding.revision, batch_size=embedding.batch_size)
    if embedder.dimension != EMBEDDING_DIMENSION:
        raise ValueError(
            f"{embedder.model_name} has {embedder.dimension} dimensions; the schema has {EMBEDDING_DIMENSION}"
        )
    return Pipeline(
        source_name=cursor_key(source.name, extract.formats),
        source=source.source,
        extractor=DoclingExtractor(extract.formats),
        embedder=embedder,
        store=PgStore(engine),
        max_tokens=chunk.max_tokens,
        formats=extract.formats,
    )


# Each step is its own task, split by what it touches, so a blip only retries that step: a database restart
# during the upsert does not rerun Docling. Dependencies are passed as task arguments, so input-based caching
# is off. Results (including vectors) stay in memory between tasks; nothing is persisted.
@task(retries=3, retry_delay_seconds=30, cache_policy=NO_CACHE, task_run_name="list changes")
def list_changes_task(source: FileSource, cursor: str | None) -> tuple[list[FileChange], str]:
    """List the source's changes since `cursor`."""
    return source.changes(cursor)


@task(retries=3, retry_delay_seconds=10, cache_policy=NO_CACHE, task_run_name="triage {change.path}")
def triage_task(change: FileChange, pipeline: Pipeline) -> Outcome | None:
    """Run `triage`."""
    return triage(change, store=pipeline.store, formats=pipeline.formats)


@task(retries=2, retry_delay_seconds=60, cache_policy=NO_CACHE, task_run_name="prepare {change.path}")
def prepare_file_task(change: FileChange, pipeline: Pipeline) -> IndexedDocument:
    """Run `prepare_file`: download, extract, chunk and embed."""
    return prepare_file(
        change,
        source=pipeline.source,
        extractor=pipeline.extractor,
        embedder=pipeline.embedder,
        max_tokens=pipeline.max_tokens,
    )


@task(retries=3, retry_delay_seconds=20, cache_policy=NO_CACHE, task_run_name="store {indexed.document.path}")
def store_document_task(indexed: IndexedDocument, pipeline: Pipeline) -> None:
    """Run `store_document`."""
    store_document(indexed, store=pipeline.store)


def process_change(change: FileChange, pipeline: Pipeline) -> Outcome:
    """process_file, as one task per step."""
    outcome = triage_task(change, pipeline)
    if outcome is not None:
        return outcome
    store_document_task(prepare_file_task(change, pipeline), pipeline)
    return "ingested"


@task(cache_policy=NO_CACHE, task_run_name="reindex {summary.path}")
def reindex_document_task(summary: DocumentSummary, pipeline: Pipeline) -> None:
    """Run `reindex_document`."""
    reindex_document(summary, embedder=pipeline.embedder, store=pipeline.store, max_tokens=pipeline.max_tokens)


@flow(name="sync")
def sync_flow() -> int:
    """Ingest what changed in the source since the last run. Returns the number of files that failed.

    Files run one at a time: the GPU holds one Docling + embedding workload at a time. A file that still
    fails after retries is recorded as failed and the run completes; only a listing that still fails after
    retries fails the run.
    """
    pipeline = build_pipeline()
    failed = sync_changes(
        pipeline.source_name,
        partial(list_changes_task, pipeline.source),
        pipeline.store,
        partial(process_change, pipeline=pipeline),
    )
    return len(failed)


@flow(name="reindex")
def reindex_flow() -> None:
    """Re-chunk and re-embed every stored document with the current model, without touching the source."""
    pipeline = build_pipeline()
    for summary in pipeline.store.list_documents():
        reindex_document_task(summary, pipeline)


app = typer.Typer(name="alexandria-workflows", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Alexandria Prefect workflows."""


def deployments() -> list["RunnerDeployment"]:
    """The sync and reindex deployments. `sync` runs every ALEXANDRIA_SYNC_INTERVAL, if that is set."""
    interval = WorkflowSettings().interval
    # One run at a time per deployment: overlapping syncs would compete for the GPU and race on the cursor.
    # The entrypoint is the installed module rather than this file's path, so any worker with Alexandria
    # installed can load the flows. to_deployment returns a coroutine only when called from async code.
    module = EntrypointType.MODULE_PATH
    sync = sync_flow.to_deployment(name="sync", concurrency_limit=1, entrypoint_type=module, interval=interval)
    reindex = reindex_flow.to_deployment(name="reindex", concurrency_limit=1, entrypoint_type=module)
    return [cast("RunnerDeployment", sync), cast("RunnerDeployment", reindex)]


@app.command(name="serve")
def serve_command() -> None:
    """Serve the sync and reindex deployments from this process.

    `sync` runs every ALEXANDRIA_SYNC_INTERVAL, if that is set. Otherwise, and always for `reindex`, start runs
    from Prefect.
    """
    serve(*deployments())


@app.command(name="deploy")
def deploy_command(
    work_pool: Annotated[str, typer.Option(help="The process work pool to run on; created if missing.")] = "alexandria",
) -> None:
    """Register the sync and reindex deployments on a work pool, then exit.

    A worker runs them: `prefect worker start --pool <work pool>`, wherever Alexandria and its settings are.
    Deploying again updates the deployments, for example after changing ALEXANDRIA_SYNC_INTERVAL.
    """
    template = ProcessWorker.get_default_base_job_template()
    with get_client(sync_client=True) as client, suppress(ObjectAlreadyExists):
        client.create_work_pool(WorkPoolCreate(name=work_pool, type="process", base_job_template=template))
    to_deploy = deployments()
    created = deploy(*to_deploy, work_pool_name=work_pool, print_next_steps_message=False, ignore_warnings=True)
    created = cast("list[UUID]", created)  # like to_deployment, a coroutine only from async code
    if len(created) != len(to_deploy):  # deploy reports failures in its output instead of raising
        raise typer.Exit(1)
