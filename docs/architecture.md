# Architecture

Alexandria keeps a library of documents searchable for an LLM agent. It syncs PDFs and other documents (Word, PowerPoint, Excel, HTML, Markdown, text, images) from a file source, extracts them to per-page Markdown, chunks and embeds them, stores everything in Postgres, and serves search and page reads over MCP. The agent must be able to search one document or all of them, and read exact pages to cite.

## Goals and non-goals

**In scope**

- Incremental sync of an S3 bucket prefix, a Dropbox folder, or a SharePoint/OneDrive for Business library: new, changed and deleted files.
- Documents to Markdown, with OCR for scanned pages and table structure recognition.
- Local embedding, on a GPU when there is one.
- Hybrid search: vector similarity plus Postgres full-text search.
- An MCP server with a small set of read-only tools.

**Not in scope**

- A web UI. The agent is the UI.
- Hosting an LLM. The agent runs elsewhere (Claude Desktop, Claude Code, or any MCP client).
- Structured field extraction across documents.
- Multiple users or permissions. Whoever can reach the database or the MCP server can read everything.

## Principles

Simplicity comes first. When two designs work, pick the one with fewer files, fewer abstractions and fewer dependencies.

1. **Protocols, not inheritance.** Every swappable dependency (file source, extractor, embedder, store) is a `typing.Protocol` in `alexandria_core.protocols`. No abstract base classes or class hierarchies.
2. **Dependencies are passed in.** Constructors and functions take what they need as arguments. There is no DI framework and no global state. Each entry point has one composition root that builds the real objects: `build_pipeline()` for ingest and `main()` for the MCP server.
3. **Functions first.** A class only when it holds state or configuration, such as a loaded model or a database engine.
4. **Plain data.** Domain objects are frozen dataclasses (`alexandria_core.models`). Pydantic only at boundaries that need validation: settings and MCP tool arguments.
5. **No speculative generality.** No plugin systems, registries or config-driven class loading. Add an abstraction when the second real implementation arrives.
6. **Synchronous by default.** Ingest is a batch job. Async only where a library requires it.
7. **Configuration from the environment**, through one small pydantic-settings class per component, loaded only at composition roots.
8. **Orchestration stays at the edge.** Only `alexandria_workflows.flows` imports Prefect. The pipeline is plain functions that run and are tested without a Prefect server.

## Packages

The code is a uv workspace of five packages, split by dependency weight. The MCP server must not pull in Docling, and only the code that embeds pulls in PyTorch.

```text
alexandria-core ◄── alexandria-embed ◄── alexandria-ingest ◄── alexandria-workflows
       ▲                   ▲
       └───────── alexandria-mcp
```

| Package | Holds | Depends on |
| --- | --- | --- |
| `alexandria-core` | Models, protocols, schema, `PgStore`, hybrid search, the settings convention | SQLAlchemy, psycopg, pgvector, pydantic-settings |
| `alexandria-embed` | `SentenceTransformerEmbedder` | core, sentence-transformers (PyTorch) |
| `alexandria-ingest` | Sources (S3, Dropbox), `DoclingExtractor`, `chunk_pages`, the pipeline steps, a CLI | core, embed, Docling, boto3, Dropbox SDK |
| `alexandria-mcp` | The MCP server | core, embed, mcp |
| `alexandria-workflows` | The Prefect flows and the ingest composition root | core, embed, ingest, Prefect |

Only ingest writes to the database. The MCP server only reads, so a failed ingest run never breaks searching.

## Data model

Postgres with the `vector` extension, mapped with SQLAlchemy 2.0. The ORM classes stay inside `db.py` and `store.py`; the rest of the code only sees the dataclasses.

| Table | Columns | Indexes and constraints |
| --- | --- | --- |
| `documents` | `id`, `source_id`, `path`, `title`, `content_hash`, `page_count`, `status` (`ok`/`failed`), `error`, `ingested_at` | unique `source_id` |
| `pages` | `id`, `document_id`, `page_number`, `markdown` | unique (`document_id`, `page_number`), cascade delete |
| `chunks` | `id`, `document_id`, `page_start`, `page_end`, `section`, `text`, `embedding vector(1024)`, `model_name`, `tsv` (generated) | HNSW on `embedding` (cosine), GIN on `tsv`, btree on `document_id`, cascade delete |
| `sync_state` | `source`, `cursor`, `updated_at` | primary key `source` |

- `pages` keeps the full Markdown of every page, so the agent can read exact pages, not just chunks.
- `tsv` is `to_tsvector('simple', text)`. The `simple` configuration doesn't stem or drop stop words, so it works for any language, at some cost in recall for English.
- The vector dimension (1024) is part of the schema. A model with another dimension needs a new column or table and a full re-index. Models of the same dimension can be swapped with the `reindex` flow, because every chunk records the model that embedded it and search only compares vectors from the same model.
- The schema is created with `CREATE EXTENSION IF NOT EXISTS vector` and `metadata.create_all()`. There are no migrations yet; see [Upgrading](../README.md#upgrading).

## Ingest

A sync asks the source what changed since the cursor stored for it, and processes only those files, plus files that failed on earlier runs. It is idempotent: running it twice in a row does nothing the second time.

The cursor is stored under the source's name plus the sorted list of formats to ingest (`s3:library/|docx,pdf`). A source only reports changed files, so without that, files of a newly added format, skipped before, would never come up again. A new key starts with a full listing, and content hashes keep indexed files from being processed again. The cost: on S3, a file deleted between the last sync and a change of formats is not noticed, because there is no previous listing to compare with.

### Sources

A source implements `FileSource`: `changes(cursor)` returns the changed files and a new cursor, and `download(file_id)` returns a file's bytes. A source has two jobs: report every deletion, and give each file a hash that changes when its content does.

- **S3** (`S3Source`) has no change feed, so each sync lists the prefix and compares it with the previous listing, which is the cursor (`{key: etag}` as JSON). Works with AWS, RustFS, MinIO and anything else that speaks S3, using path-style addressing.
- **Dropbox** (`DropboxSource`) uses Dropbox's own cursor (`files/list_folder/continue`). If Dropbox resets the cursor, it falls back to a full listing, and content hashes keep unchanged files from being processed again. Files are keyed by `path_lower`, because deletions only carry a path. Authentication uses an offline refresh token from a one-off PKCE flow (`alexandria-ingest auth`), with no app secret.
- **SharePoint / OneDrive for Business** (`SharePointSource`) uses Microsoft Graph's delta feed on a drive. In these drives the feed carries no paths, and renaming or deleting a folder doesn't report the files in it, so the cursor holds the delta link plus every item's parent and name by id (`{"delta": link, "items": {id: [parent, name, folder, hash]}}`). Each run applies the delta to that map, rebuilds the paths and diffs them with the previous run: a moved file becomes a deletion of its old path and a change at the new one, and the files of a deleted folder become deletions. Files are keyed by lower-cased path, like Dropbox, so the store needs nothing source-specific. An expired delta link (`410 resyncRequired`) restarts the listing, and the same diff keeps unchanged files from being reported. The hash is `quickXorHash`, or the `cTag` when there is none. Sign-in is app-only with a client secret via azure-identity; requests are plain httpx, retried on 429/503/504 after `Retry-After`. Microsoft's Graph SDK isn't used: it is async-only and very large, and this source makes three kinds of request.

### One file

Each change goes through up to three steps. They are separate functions so a scheduler can retry each one on its own:

1. **Triage** (store only): a deletion removes the document with its pages and chunks; a file whose extension isn't in `ALEXANDRIA_EXTRACT_FORMATS` is skipped; a file whose hash is already stored is skipped.
2. **Prepare** (source, Docling, embedder): download, extract, chunk and embed. It writes nothing, so it's safe to retry.
3. **Store**: upsert the document. Its pages and chunks are replaced in one transaction, so a search never sees half a document.

If a file still fails after its retries, it is recorded with `status = failed` (and its old pages and chunks are removed, so search never serves stale content), and the run moves on. One bad file must not stop a sync. The cursor is saved at the end of the run, and failed files are retried on the next one. A failed deletion is the exception: it ends the run without saving the cursor, so the next run reports the deletion again.

### Extraction

`DoclingExtractor` converts a document with OCR and table structure recognition on, and exports Markdown per page, so page numbers survive for citations. The file name picks Docling's format. PowerPoint slides and Excel sheets become pages; formats without pages (Word, HTML, Markdown, text) are stored as a single page 1. Control characters, which PDFs with broken font encodings produce, are removed. Docling uses the GPU when there is one.

### Chunking

`chunk_pages(pages, max_tokens)` is a pure function:

- It splits on Markdown headings first, then packs paragraphs up to about `max_tokens` (estimated as characters / 4).
- Tables are never split, unless a single table exceeds the limit. Then it is split by rows, with the header repeated in each part.
- Each chunk records its page range and heading path. The embedded text is prefixed with `<title> > <heading path>`, so short chunks keep their context. The stored text is the plain chunk.

### Embedding

`SentenceTransformerEmbedder` loads a pinned model and revision (BGE-M3 by default), applies the model's query and document prompts if it defines any, and L2-normalises vectors so cosine distance works. Ingest embeds on the GPU in batches; the MCP server embeds queries on the CPU, which is fast enough for short text. Both must use the same model and revision.

### Orchestration

`alexandria-workflows` serves two Prefect deployments, `sync` and `reindex`, without schedules: runs are started from the Prefect UI, the CLI, or an automation. Each deployment allows one run at a time, because overlapping syncs would compete for the GPU and race on the cursor.

The listing and each file's three steps are separate Prefect tasks with their own retries. Results stay in memory between tasks and are never persisted. Files are processed one at a time, because the GPU holds one Docling and embedding workload at a time.

## Retrieval

Hybrid search runs in one SQL statement. Vector similarity and full-text search each return their top 50 chunks, and the two lists are merged with Reciprocal Rank Fusion (`score = Σ 1 / (60 + rank)`). There is no reranker.

- **Why hybrid:** embeddings miss exact terms (names, codes, numbers), and full-text search misses paraphrases. RRF combines ranks, so the two scores never need to be on the same scale.
- **Filter:** the optional `document_ids` filter is applied inside both candidate lists, not after fusion, so a single-document search still gets full candidate lists. pgvector's `hnsw.iterative_scan` keeps the filtered HNSW scan going until it has enough rows (pgvector 0.8 or newer).
- **Full-text query:** `websearch_to_tsquery`, so the agent can use quotes and `-exclude`.

## MCP server

One server, built with the official `mcp` SDK, exposing three read-only tools: `search`, `list_documents` and `get_pages`. The tool docstrings are prompts for the model: what each tool is for, when to call `get_pages` after `search`, and that answers should cite title and pages.

- **Output** is plain text with a header per hit, e.g. `[doc 12] Annual report 2025 — pp. 41–42`, which is easier for a model to use than nested JSON.
- **Limits** keep one call from flooding the agent's context: at most 50 search results, 20 pages, 500 listed documents per call, and 40,000 characters per response.
- **Thin layer:** each tool validates its arguments, calls the store and the embedder, and formats the result. `build_server(store, embedder)` takes any implementations of the protocols, so tests pass fakes.
- **Transports:** stdio for local clients such as Claude Desktop. Streamable HTTP binds to localhost and has no authentication; exposing it needs an authenticating proxy.

## Deployment

One Docker image (`Dockerfile`) holds all of Alexandria; the command picks the role: `alexandria-workflows serve` (the default), `alexandria-mcp`, or `prefect server start`. It's built in two variants from the root project's extras, `cu128` (tag `<version>`) and `cpu` (tag `<version>-cpu`). uv applies those extras only to the workspace root, so a smaller MCP-only image can't select the CPU PyTorch build from the lockfile, which is why every role shares the image. CUDA comes from the PyTorch wheels, so the base is `python:3.13-slim` and the host needs only the driver and the NVIDIA Container Toolkit.

Models aren't baked in: they download on first start into `HF_HOME` (`/models`), a volume shared by the worker and the MCP server. The Prefect server keeps its SQLite database in `PREFECT_HOME` (`/data/prefect`), another volume.

`deploy/compose.yaml` and `deploy/.env.example` are attached to every GitHub release. The release workflow (`.github/workflows/release.yml`) pins the compose file's image tag to the release, pushes both variants to GHCR, and lets GitHub generate the release notes from the merged pull requests (grouped by `.github/release.yml`). A test in `workflows/tests/test_deploy.py` checks that every setting in `deploy/.env.example` is documented.

## Testing

pytest, with hand-written fakes instead of a mocking library. Because the fakes satisfy the protocols, the type checker keeps them in sync with the real code.

| Layer | How | Marker |
| --- | --- | --- |
| Chunking | Pure function, Markdown fixtures | none |
| Pipeline | Fake source, extractor, embedder and in-memory store | none |
| Sources | Fake SDK clients; S3 also against a RustFS container; SharePoint's HTTP client against `httpx.MockTransport` | `integration` for RustFS |
| Store and search | Real Postgres with pgvector, via testcontainers | `integration` |
| MCP server | `build_server` with fakes, tools called in-process | none |
| Workflows | The real flows with fakes, inside a temporary Prefect API | none |
| Extractor and embedder | Real Docling and the real model on small fixtures | `gpu` |

The default run excludes `gpu` and needs Docker for the integration tests. Coverage must stay at 95% or above.
