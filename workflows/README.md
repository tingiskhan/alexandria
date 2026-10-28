# alexandria-workflows

Runs the [ingest pipeline](../packages/ingest/README.md) with [Prefect](https://docs.prefect.io): retries, logs and a UI to start runs. `flows.py` is the only module in Alexandria that imports Prefect. All logic lives in alexandria-ingest, so it runs and is tested without a Prefect server.

## Flows

| Flow | What it does |
| --- | --- |
| `sync` | Ingests what changed in the configured source since the last run, plus files that failed before. Returns the number of files that still failed. |
| `reindex` | Re-chunks and re-embeds every stored document from its stored pages, e.g. after changing the embedding model. Does not touch the source. |

Neither flow has a schedule. Start them from the Prefect UI, with `prefect deployment run sync/sync`, or from an automation or webhook you set up in Prefect. Each deployment has a concurrency limit of 1, so a run that is started while another is still going waits for it to finish instead of competing for the GPU and the sync cursor.

`build_pipeline()` is the composition root. It loads all settings first, so missing configuration fails before any work starts. Then it waits for the database, creates the schema if needed, loads Docling and the embedding model, and checks that the model's dimension matches the schema.

## Tasks and retries

Each step is its own Prefect task, so a short outage only retries the step that hit it:

| Task | Retries | Touches |
| --- | --- | --- |
| `list changes` | 3 × 30 s | the source |
| `triage <path>` | 3 × 10 s | the database |
| `prepare <path>` | 2 × 60 s | the source, Docling, the embedder |
| `store <path>` | 3 × 20 s | the database |

For example, a Postgres restart during `store` retries only the upsert, not Docling. A file that still fails after its retries is recorded as `failed` and the run goes on. The run itself fails only if the listing still fails, or if a deletion fails (the cursor is then not saved, so the next run sees the deletion again).

Files are processed one at a time, because the GPU holds one Docling and embedding workload at a time.

## Running

```bash
export PREFECT_API_URL=http://<prefect-server>:4200/api   # or run `uv run prefect server start` locally
export PREFECT_LOGGING_EXTRA_LOGGERS=alexandria_ingest     # show pipeline logs in the Prefect UI
uv run --extra cu128 alexandria-workflows serve
```

## Tests

```bash
uv run pytest workflows    # runs the flows against fakes inside a temporary Prefect API
```
