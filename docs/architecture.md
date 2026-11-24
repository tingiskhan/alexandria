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
| `alexandria-embed` | `SentenceTransformerEmbedder`, `CrossEncoderReranker` | core, sentence-transformers (PyTorch) |
| `alexandria-ingest` | Sources (S3, Dropbox), `DoclingExtractor`, `TranscriptExtractor`, `chunk_pages`, the pipeline steps, a CLI | core, embed, Docling, boto3, Dropbox SDK |
| `alexandria-mcp` | The MCP server | core, embed, mcp |
| `alexandria-workflows` | The Prefect flows and the ingest composition root | core, embed, ingest, Prefect |

Only ingest writes to the database. The MCP server only reads, so a failed ingest run never breaks searching. The one exception is schema: on start it runs the migrations (`migrate`), so an upgraded server works before the next sync.

## Data model

Postgres with the `vector` extension, mapped with SQLAlchemy 2.0. The ORM classes stay inside `db.py` and `store.py`; the rest of the code only sees the dataclasses.

| Table | Columns | Indexes and constraints |
| --- | --- | --- |
| `documents` | `id`, `source_id`, `path`, `title`, `authors` (`text[]`), `year`, `doi`, `doc_type`, `content_hash`, `page_count`, `status` (`ok`/`failed`), `error`, `ingested_at`, `content_digest`, `duplicate_of` | unique `source_id`; btree on `content_digest` and `duplicate_of`; `duplicate_of` references `documents.id` `ON DELETE SET NULL` |
| `pages` | `id`, `document_id`, `page_number`, `markdown` | unique (`document_id`, `page_number`), cascade delete |
| `chunks` | `id`, `document_id`, `page_start`, `page_end`, `section`, `text`, `embedding vector(1024)`, `model_name`, `tsv` (generated) | HNSW on `embedding` (cosine), GIN on `tsv`, btree on `document_id`, cascade delete |
| `sync_state` | `source`, `cursor`, `updated_at` | primary key `source` |

- `pages` keeps the full Markdown of every page, so the agent can read exact pages, not just chunks.
- `tsv` is `to_tsvector('simple', text)`. The `simple` configuration doesn't stem or drop stop words, so it works for any language, at some cost in recall for English.
- The vector dimension (1024) is part of the schema. A model with another dimension needs a new column or table and a full re-index. Models of the same dimension can be swapped with the `reindex` flow, because every chunk records the model that embedded it and search only compares vectors from the same model.
- `authors`, `year`, `doi` and `doc_type` are nullable: they came after the first release, and rows from before read as unknown (`doc_type` as `other`) until `alexandria-ingest backfill-metadata` fills them in. In code they are one `DocumentMetadata` (with `title`) on `Document`, `DocumentSummary` and `SearchHit`.
- `content_digest` and `duplicate_of` find the same document under another path; see [Duplicates](#duplicates). `content_hash` can't: it is the source's own hash, which differs between sources and, for S3 multipart uploads, between copies of the same bytes.
- The schema is created and changed only by Alembic migrations, in `alexandria_core/migrations/versions/`; the ORM classes describe it but never create it. `0001_initial_schema` creates the `vector` extension and the first release's tables, `0002_document_metadata` adds the metadata columns and `0003_duplicate_detection` the duplicate columns. A test upgrades an empty database and has Alembic compare it with the ORM classes, so the two can't drift apart.
- Every entry point calls `alexandria_core.migrate.migrate(engine)` on start, after `wait_for_database`: the Prefect flows' `build_pipeline`, the MCP server and `alexandria-eval` (`build_retrieval`), and the `alexandria-ingest` commands that need the newest columns. It runs in one transaction holding a Postgres advisory lock (`pg_advisory_xact_lock`), so processes that start together migrate one after the other and a failed migration changes nothing. Alembic is configured in code (`alembic_config`), not from an `alembic.ini`, so it finds the migrations wherever the package is installed.
- A database from before migrations has the tables but no `alembic_version`. Older releases added columns with `ALTER TABLE … ADD COLUMN IF NOT EXISTS` (`add_missing_columns`), each revision's columns together, so `migrate` reads the `documents` columns to tell which revision it is at (`detect_revision`), stamps that revision and upgrades from there. Only the revisions from before migrations need to be recognised this way. See [Upgrading](../README.md#upgrading).

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
2. **Prepare** (source, Docling, embedder): download, extract, chunk and embed. It writes nothing, so it's safe to retry. A document with the same text as a searchable one isn't chunked or embedded (see [Duplicates](#duplicates)).
3. **Store**: upsert the document. Its pages and chunks are replaced in one transaction, so a search never sees half a document.

If a file still fails after its retries, it is recorded with `status = failed` (and its old pages and chunks are removed, so search never serves stale content), and the run moves on. One bad file must not stop a sync. The cursor is saved at the end of the run, and failed files are retried on the next one. A failed deletion is the exception: it ends the run without saving the cursor, so the next run reports the deletion again.

### Duplicates

`content_digest(pages)` is a SHA-256 of the extracted text with whitespace collapsed; a document without text has none and is never a duplicate. Text rather than file bytes, because a re-saved copy with different bytes still matches, and stored documents get a digest from their stored pages with no download. The price is that a copy is still downloaded and extracted; only embedding is skipped.

`prepare_file` asks `DocumentStore.original_of(document)` for a searchable document (`ok`, not a duplicate, another `source_id`) with the same digest, the oldest if several, and skips embedding when there is one. `upsert_document` checks again in its transaction and decides: a duplicate is stored with its pages, no chunks and `duplicate_of` set. So search needs no filter of its own, and `get_pages` still reads the copy.

A duplicate must become searchable again when its original goes away. `PgStore` does this in the same transaction as the change: when an original is deleted, marked failed, or upserted with different text (or as a duplicate itself), its duplicates are recorded as `failed`, with their pages removed. The next sync retries failed files, as for any other failure, so the first one becomes the new original and the others its duplicates. This re-extracts the copy, which is rare, and reuses the retry path instead of adding a step to every sync. `ON DELETE SET NULL` only keeps the reference valid if a row is deleted outside Alexandria; such a copy stays listed but unsearched until its file changes.

Documents from before digests existed have none, so they match nothing. `alexandria-ingest duplicates` computes their digests from stored pages, stores them, and lists the copies; it doesn't change what is searched. The `reindex` flow re-upserts every document, which turns such older copies into duplicates.

### Extraction

`DoclingExtractor` converts a document with OCR and table structure recognition on, and exports Markdown per page, so page numbers survive for citations. The file name picks Docling's format. PowerPoint slides and Excel sheets become pages; formats without pages (Word, HTML, Markdown, text) are stored as a single page 1. Control characters, which PDFs with broken font encodings produce, are removed. Docling uses the GPU when there is one.

Transcripts (`.vtt`, `.srt`) skip Docling: `make_extractor` builds a `FormatRouter` (itself an `Extractor`) that sends them to `TranscriptExtractor`, a small hand-written parser. Docling reads WebVTT too, but as one page without times, and SubRip not at all. A transcript's pages are fixed blocks of time (`ALEXANDRIA_EXTRACT_TRANSCRIPT_BLOCK_MINUTES`, 5 by default), each starting with a `## [00:15:00–00:20:00]` heading, so `search` and `get_pages` need no changes: a chunk's section is its time range, and a page number is a time. Page N is always block N, so a block without speech is stored as a page that says so instead of being left out; leaving it out would shift the numbering and make the page count disagree with the stored pages. Cues are assigned to the block they start in and merged into paragraphs, broken on a change of speaker, a pause of 2 seconds or more, or a sentence end after about 600 characters.

Stored Markdown has one maths convention: inline `\(...\)`, display `$$...$$`, with no Markdown escapes inside either (`latex.py`). `\(` rather than `$` because `$` also occurs in prose. Every formula is checked with KaTeX during ingest, in-process through dukpy. A formula that fails is logged and the document is stored anyway, because most failures are model reading errors; `alexandria-ingest check-formulas` reports them for everything stored.

### Metadata

`MetadataReader` works out a document's `DocumentMetadata` from its path and first page, during `prepare_file`. Title, authors and year are heuristics on the first page's Markdown (the first heading that isn't a section name, a line of names below it, a copyright notice or the latest date). A DOI on that page is looked up through a `BibliographyLookup` (`CrossrefLookup` in production, a fake in tests), whose answer wins field by field; a lookup that fails returns None, so metadata never fails an ingest. The type comes from `ALEXANDRIA_METADATA_DOC_TYPES` (deepest matching folder), else Crossref's type, else `other`. The title falls back to the file stem. Transcripts are recognised by extension and skip all of this: their type is `transcript` whatever the folder, their title is the WebVTT header's (written as a `#` heading above page 1's time range) or the file stem, and they are never looked up, since a DOI or a date said aloud says nothing about the recording.

`alexandria-ingest backfill-metadata` runs the same reader over every stored document's stored page 1 and stores the result with `DocumentStore.update_metadata`, without touching pages or chunks.

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

Hybrid search runs in one SQL statement. Vector similarity and full-text search each return their top 50 chunks, and the two lists are merged with Reciprocal Rank Fusion (`score = Σ 1 / (60 + rank)`).

- **Why hybrid:** embeddings miss exact terms (names, codes, numbers), and full-text search misses paraphrases. RRF combines ranks, so the two scores never need to be on the same scale.
- **Filters:** the optional `document_ids` and the `DocumentFilter` on `SearchQuery` (case-insensitive `path_prefix`, `doc_type`, inclusive `year_from` / `year_to`) are applied inside both candidate lists, not after fusion, so a filtered search still gets full candidate lists and fills its limit when enough chunks match. The `DocumentFilter` becomes `document_id IN (SELECT id FROM documents WHERE …)`; only the conditions a query sets are added, from fixed SQL fragments with bound values (`LIKE` wildcards in the prefix are escaped). pgvector's `hnsw.iterative_scan` keeps a filtered HNSW scan going until it has enough rows (pgvector 0.8 or newer); for a selective filter Postgres may instead scan the matching documents' chunks exactly, which is cheap. `list_documents` filters in SQL with the same conditions.
- **No indexes on `documents.doc_type` or `year`:** the filter subquery reads the `documents` table, which has one row per file (thousands, not millions), so a sequential scan is cheap; the join into `chunks` uses the existing `chunks.document_id` index. Revisit if the library grows by orders of magnitude.
- **Full-text query:** `websearch_to_tsquery`, so the agent can use quotes and `-exclude`.
- **Optional reranking:** with `ALEXANDRIA_RERANK_MODEL` set (off by default), the MCP server asks for the top 50 fused hits (`ALEXANDRIA_RERANK_CANDIDATES`, or the requested limit if larger), scores each chunk's text against the query with a cross-encoder (`Reranker`, implemented by `CrossEncoderReranker` in `alexandria-embed`), and returns the best `limit` by that score. Search filters go into the candidate query, so every reranked candidate already matches them. It runs on the same device as the query embedder. Off, the RRF order is returned unchanged. Measure it on your own queries before turning it on: it costs one cross-encoder pass over the candidates per search.

## MCP server

One server, built with the official `mcp` SDK, exposing three read-only tools: `search`, `list_documents` and `get_pages`. The tool docstrings are prompts for the model: what each tool is for, when to call `get_pages` after `search`, and that answers should cite title and pages.

- **Output** is plain text with a header per hit, e.g. `[doc 12] Annual report 2025 — Jane Doe (2025) — pp. 41–42`, which is easier for a model to use than nested JSON. `list_documents` ends a duplicate's line with `DUPLICATE of doc N`.
- **Limits** keep one call from flooding the agent's context: at most 50 search results, 20 pages, 500 listed documents per call, and 40,000 characters per response.
- **Thin layer:** each tool validates its arguments, calls the store and the embedder, and formats the result. `build_server(store, embedder, reranker)` takes any implementations of the protocols, so tests pass fakes. `search` goes through `Retrieval` (`retrieval.py`), which `alexandria-eval` uses too, so the evaluation measures exactly what the server serves.
- **Transports:** stdio for local clients such as Claude Desktop. Streamable HTTP binds to localhost and has no authentication; exposing it needs an authenticating proxy.

## Deployment

One Docker image (`Dockerfile`) holds all of Alexandria; the command picks the role, and by default it only prints help. In `deploy/compose.yaml`, a one-off `alexandria-workflows deploy` registers the flows on a process work pool, `prefect worker start` runs them, and `alexandria-mcp` and `prefect server start` run the other two services. Deployments use module-path entrypoints, so the worker loads the flows from the installed package. It's built in two variants from the root project's extras, `cu128` (tag `<version>`) and `cpu` (tag `<version>-cpu`). uv applies those extras only to the workspace root, so a smaller MCP-only image can't select the CPU PyTorch build from the lockfile, which is why every role shares the image. CUDA comes from the PyTorch wheels, so the base is `python:3.13-slim` and the host needs only the driver and the NVIDIA Container Toolkit.

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
