# Alexandria

[![CI](https://github.com/tingiskhan/alexandria/actions/workflows/ci.yml/badge.svg)](https://github.com/tingiskhan/alexandria/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Alexandria syncs documents (PDF, Word, PowerPoint, Excel, HTML, Markdown, text, and optionally images) from an S3-compatible store (AWS, RustFS, MinIO), Dropbox, or SharePoint and OneDrive for Business, indexes them in Postgres (pgvector plus full-text search), and lets an LLM agent search them and read exact pages over [MCP](https://modelcontextprotocol.io).

```text
S3/Dropbox ──► alexandria-workflows (Prefect) → alexandria-ingest ──► Postgres + pgvector ◄── alexandria-mcp ◄── Claude
               download → Docling → chunk → embed                    search · list_documents · get_pages
```

- **Extraction** with [Docling](https://github.com/docling-project/docling): per-page Markdown, OCR for scanned pages, tables kept as Markdown tables.
- **Hybrid search**: vector similarity (BGE-M3 by default) and Postgres full-text search, fused with Reciprocal Rank Fusion, so both paraphrases and exact terms (names, codes, numbers) are found.
- **Citable answers**: every hit carries its document and page range, and the agent can read whole pages to quote them.
- **Incremental sync**: only new and changed files are processed, deleted files leave the index, and a file that fails is retried on the next run.

It's built for one person's or one team's library on a home server or workstation. There is no web UI (the agent is the UI) and no access control beyond who can reach the database.

Alexandria only indexes documents that are already in your storage. It doesn't find, download or unlock content, and it doesn't change your files. Index only documents you have the right to use, and keep to their licence terms when you share what the agent finds.

## Packages

| Package | What | Heavy dependencies |
| --- | --- | --- |
| [`alexandria-core`](packages/core/README.md) | Domain types, protocols, schema and migrations, `PgStore`, hybrid search | SQLAlchemy, psycopg, pgvector |
| [`alexandria-embed`](packages/embed/README.md) | `SentenceTransformerEmbedder` (BGE-M3, 1024 dimensions) | PyTorch |
| [`alexandria-ingest`](packages/ingest/README.md) | File sources (S3, Dropbox, SharePoint), Docling extraction, chunking, the pipeline, a CLI | Docling, boto3, Dropbox SDK, azure-identity, httpx |
| [`alexandria-mcp`](packages/mcp/README.md) | Read-only MCP server | mcp SDK |
| [`alexandria-workflows`](workflows/README.md) | Prefect flows; the only code that imports Prefect | Prefect |

How the pieces fit together, and why, is in [docs/architecture.md](docs/architecture.md).

## Run with Docker

You need Docker with Compose. Nothing else: no clone, no Python. For ingest on an NVIDIA GPU, also install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) (on Windows, Docker Desktop with the WSL 2 backend provides GPU support).

```bash
mkdir alexandria && cd alexandria
curl -LO https://github.com/tingiskhan/alexandria/releases/latest/download/compose.yaml
curl -L -o .env https://github.com/tingiskhan/alexandria/releases/latest/download/env.example
# edit .env: POSTGRES_PASSWORD, COMPOSE_PROFILES (gpu or cpu), and your source (see Sources below)
docker compose up -d
```

This starts Postgres with pgvector, a Prefect server, a Prefect worker for ingest and the MCP server, all from the image `ghcr.io/tingiskhan/alexandria`. It comes in two variants: `<version>` with CUDA 12.8 PyTorch for the `gpu` worker, and `<version>-cpu`, which the `cpu` worker, the MCP server and the Prefect server use. The first start downloads the embedding model and Docling's models (a few GB) into the `models` volume.

- **Syncs** run every `ALEXANDRIA_SYNC_INTERVAL` (`1h` in the example `.env`). Follow them, or start one by hand, in the Prefect UI at <http://localhost:4200>. The one-off `deploy` service registers the `sync` and `reindex` deployments on the `alexandria` work pool on every `docker compose up`, and the worker runs them.
- **Check the source** before the first sync: `docker compose exec worker-gpu alexandria-ingest check-source` (or `worker-cpu`).
- **Try it without your own storage:** add `rustfs` to `COMPOSE_PROFILES` and set `ALEXANDRIA_S3_SECRET_ACCESS_KEY`. Then create the bucket `library` in the RustFS console at <http://localhost:9001> and upload a few documents.
- **Connect an agent:** see [Connect Claude](#connect-claude).

The Prefect UI and the MCP server are published on localhost only, and neither has authentication; see [SECURITY.md](SECURITY.md).

## Run from source

You need [uv](https://docs.astral.sh/uv/), Python 3.13, and Docker (or your own Postgres with pgvector 0.8 or newer, and your own S3 store or Dropbox).

```bash
git clone https://github.com/tingiskhan/alexandria && cd alexandria
uv sync --extra cpu                # or --extra cu128 on an NVIDIA GPU; see below
cp .env.example .env               # then set the passwords, see below
docker compose up -d               # Postgres + pgvector, and RustFS (S3 API on :9000, console on :9001)
```

Before starting compose, fill in two passwords in `.env`. Compose reads them from there too, and won't start while one is missing:

- `POSTGRES_PASSWORD`: the password for the local Postgres. `ALEXANDRIA_DATABASE_URL` refers to it as `${POSTGRES_PASSWORD}`, so you set it only once.
- `ALEXANDRIA_S3_SECRET_ACCESS_KEY`: the RustFS admin password, at least 8 characters. The admin user is `ALEXANDRIA_S3_ACCESS_KEY_ID` (`alexandria`).

Open the RustFS console at <http://localhost:9001/rustfs/console/>, log in with that access key and secret, create a bucket named `library` and upload a few PDFs or other documents. Then check that Alexandria sees them:

```bash
uv run --extra cpu alexandria-ingest check-source
```

Then [run a sync](#ingest) and [connect an agent](#connect-claude). To sync from Dropbox or your own S3 store instead, change the source settings in `.env` (see [Sources](#sources)).

### Choosing a PyTorch build

Docling and the embedding model run on PyTorch. Choose a build with an extra:

| Extra | Build | Use it for |
| --- | --- | --- |
| `cpu` | CPU only | The MCP server, and ingest on machines without an NVIDIA GPU. Ingest works, but OCR and embedding are much slower. |
| `cu128` | CUDA 12.8 | Ingest on an NVIDIA GPU, including RTX 50xx (Blackwell) cards. |

On macOS both extras install PyPI's build, which runs on the CPU (and Apple's MPS where PyTorch supports it).

`uv run` syncs the environment before it runs. Without the extra, it switches to PyPI's default PyTorch build. That swap is a large download, and when it switches between CUDA builds it can break the CUDA libraries. So pass the same extra to every `uv run`, as the examples here do, or run `uv sync --extra …` once and then use `uv run --no-sync` (or set `UV_NO_SYNC=1`). If a CUDA import fails with a missing `libcudnn` or similar after a switch, reinstall the NVIDIA packages:

```bash
uv sync --extra cu128 --reinstall
```

## Configuration

Each package owns the settings it uses, as a small pydantic-settings class next to its code, and each entry point loads only the classes it needs. Missing required values fail at startup. Values are read in this order of precedence:

1. Environment variables.
2. A `.env` file in the working directory.
3. One file per secret in `/run/secrets` (Kubernetes Secret volumes, Docker secrets), named after the variable, e.g. `/run/secrets/ALEXANDRIA_S3_SECRET_ACCESS_KEY`.

Secrets are `SecretStr`, so they never appear in logs or reprs. An empty value (`KEY=`) counts as unset. [`.env.example`](.env.example) lists every setting with a comment.

| Variable | Class | Required | Used by | Notes |
| --- | --- | --- | --- | --- |
| `ALEXANDRIA_DATABASE_URL` | `DatabaseSettings` (core) | yes, secret | workflows, MCP | `postgresql+psycopg://user:password@host:5432/alexandria` |
| `ALEXANDRIA_SOURCE_KIND` | `SourceSettings` (ingest) | yes | workflows | `s3`, `dropbox` or `sharepoint`; only that source's settings are loaded |
| `ALEXANDRIA_S3_ENDPOINT_URL` | `S3Settings` (ingest `sources/s3/`) | no (AWS) | workflows | e.g. `http://rustfs:9000` |
| `ALEXANDRIA_S3_BUCKET` | `S3Settings` | yes (S3) | workflows | |
| `ALEXANDRIA_S3_PREFIX` | `S3Settings` | no (`""`) | workflows | Only sync keys under this prefix, e.g. `papers/` |
| `ALEXANDRIA_S3_ACCESS_KEY_ID` | `S3Settings` | yes (S3) | workflows | |
| `ALEXANDRIA_S3_SECRET_ACCESS_KEY` | `S3Settings` | yes (S3), secret | workflows | |
| `ALEXANDRIA_S3_REGION` | `S3Settings` | no (`us-east-1`) | workflows | |
| `ALEXANDRIA_DROPBOX_APP_KEY` | `DropboxSettings` (ingest `sources/dropbox/`) | yes (Dropbox) | workflows | |
| `ALEXANDRIA_DROPBOX_REFRESH_TOKEN` | `DropboxSettings` | yes (Dropbox), secret | workflows | From `alexandria-ingest auth`; valid until revoked |
| `ALEXANDRIA_DROPBOX_PATH` | `DropboxSettings` | no (`""`) | workflows | Folder to sync, starting with `/`; `""` is the app folder root |
| `ALEXANDRIA_SHAREPOINT_TENANT_ID` | `SharePointSettings` (ingest `sources/sharepoint/`) | yes (SharePoint) | workflows | The Entra ID tenant (directory) id |
| `ALEXANDRIA_SHAREPOINT_CLIENT_ID` | `SharePointSettings` | yes (SharePoint) | workflows | The app registration's application (client) id |
| `ALEXANDRIA_SHAREPOINT_CLIENT_SECRET` | `SharePointSettings` | yes (SharePoint), secret | workflows | |
| `ALEXANDRIA_SHAREPOINT_SITE` | `SharePointSettings` | yes, unless `DRIVE_ID` is set | workflows | e.g. `contoso.sharepoint.com:/sites/Finance` |
| `ALEXANDRIA_SHAREPOINT_LIBRARY` | `SharePointSettings` | no (`Documents`) | workflows | The document library in the site |
| `ALEXANDRIA_SHAREPOINT_DRIVE_ID` | `SharePointSettings` | no | workflows | A drive id instead of site and library, e.g. a OneDrive for Business drive |
| `ALEXANDRIA_SHAREPOINT_PATH` | `SharePointSettings` | no (`""`) | workflows | Folder to sync, starting with `/`; `""` is the whole library |
| `ALEXANDRIA_EMBEDDING_MODEL` | `EmbeddingSettings` (embed) | no (`BAAI/bge-m3`) | workflows, MCP | Must have 1024 dimensions (`EMBEDDING_DIMENSION` in `db.py`) |
| `ALEXANDRIA_EMBEDDING_REVISION` | `EmbeddingSettings` | no (pinned commit) | workflows, MCP | Ingest and MCP must use the same model and revision |
| `ALEXANDRIA_EMBEDDING_BATCH_SIZE` | `EmbeddingSettings` | no (`32`) | workflows | |
| `ALEXANDRIA_RERANK_MODEL` | `RerankSettings` (embed) | no (off) | MCP | A cross-encoder that reranks search candidates, e.g. `BAAI/bge-reranker-v2-m3`; empty turns reranking off. Runs on the same device as the query embedder (CPU) |
| `ALEXANDRIA_RERANK_REVISION` | `RerankSettings` | no (default branch) | MCP | A commit of the reranker to pin |
| `ALEXANDRIA_RERANK_CANDIDATES` | `RerankSettings` | no (`50`) | MCP | How many hybrid-search hits to rerank (at least the requested limit); hybrid search yields at most 100 |
| `ALEXANDRIA_CHUNK_MAX_TOKENS` | `ChunkSettings` (ingest) | no (`500`) | workflows | |
| `ALEXANDRIA_BOILERPLATE_PATTERNS` | `BoilerplateSettings` (ingest) | no (`[]`) | workflows | JSON list of regexes (case-insensitive); matching lines are left out of chunks, e.g. `["^Downloaded from "]` |
| `ALEXANDRIA_BOILERPLATE_REPEAT_RATIO` | `BoilerplateSettings` | no (`0.6`) | workflows | A line on at least this fraction of a document's pages (digits ignored) is a running header and is left out of chunks |
| `ALEXANDRIA_BOILERPLATE_REPEAT_MIN_PAGES` | `BoilerplateSettings` | no (`4`) | workflows | Only look for running headers in documents with at least this many pages; a large value turns it off |
| `ALEXANDRIA_BOILERPLATE_FRONT_MATTER_PATTERNS` | `BoilerplateSettings` | no (copyright, terms of use, "downloaded from"…) | workflows | JSON list of regexes; the first page of a multi-page document is left out of chunks when paragraphs matching these make up most of it. `[]` turns it off. Stored pages are never changed; run `reindex` after changing these. Transcripts are never stripped |
| `ALEXANDRIA_EXTRACT_FORMATS` | `ExtractSettings` (ingest) | no (`pdf,docx,pptx,xlsx,html,htm,md,txt,vtt,srt`) | workflows | File extensions to ingest, comma-separated; see [File formats](#file-formats) |
| `ALEXANDRIA_EXTRACT_TRANSCRIPT_BLOCK_MINUTES` | `ExtractSettings` | no (`5`) | workflows | Minutes of a transcript per page; see [Transcripts](#transcripts) |
| `ALEXANDRIA_METADATA_CROSSREF` | `MetadataSettings` (ingest) | no (`true`) | workflows, `backfill-metadata` | Look up a DOI found on a document's first page on Crossref and prefer its title, authors and year; `false` keeps ingest offline |
| `ALEXANDRIA_METADATA_CROSSREF_MAILTO` | `MetadataSettings` | no | workflows, `backfill-metadata` | A contact email sent to Crossref, which gets its faster "polite" service |
| `ALEXANDRIA_METADATA_DOC_TYPES` | `MetadataSettings` | no (`papers=paper,books=book,policies=policy,transcripts=transcript`) | workflows, `backfill-metadata` | Document type per folder, comma-separated `folder=type`; types are `paper`, `book`, `policy`, `transcript`, `other`; see [Document metadata](#document-metadata) |
| `ALEXANDRIA_SYNC_INTERVAL` | `WorkflowSettings` (workflows) | no (none) | workflows | Run `sync` this often: seconds, `30m`, `1h`, `1d` or ISO 8601 (`PT1H`); unset means runs only start by hand |
| `ALEXANDRIA_MCP_HOST` | `McpSettings` (mcp) | no (`127.0.0.1`) | MCP | Where `--transport streamable-http` listens; the Docker image's compose file sets `0.0.0.0` inside the container |
| `ALEXANDRIA_MCP_PORT` | `McpSettings` | no (`8000`) | MCP | |
| `ALEXANDRIA_MCP_ALLOWED_HOSTS` | `McpSettings` | no | MCP | Other names clients reach the HTTP server by, comma-separated, e.g. your proxy's; requests for any other name than localhost are refused |

To add settings for a new component, define a `BaseSettings` subclass in its package with `model_config = settings_config("ALEXANDRIA_<COMPONENT>_")` from `alexandria_core.settings`, and load it in the entry points that use it.

## Sources

Pick one with `ALEXANDRIA_SOURCE_KIND`, then check that Alexandria can reach it and sees your documents:

```bash
uv run --extra cpu alexandria-ingest check-source
```

Documents are keyed by the source's file id (the S3 key or the Dropbox path), so a moved or renamed file is re-ingested. To add another kind of source, see [Adding a source](packages/ingest/README.md#adding-a-source).

### File formats

`ALEXANDRIA_EXTRACT_FORMATS` lists the file extensions to ingest; other files are skipped. Transcripts (`vtt`, `srt`) are read by Alexandria itself and everything else goes through Docling, so any extension Docling reads works. The default is `pdf,docx,pptx,xlsx,html,htm,md,txt,vtt,srt`. Add `png,jpg,jpeg,tiff` to OCR images; an unknown extension fails at startup.

Page numbers follow the format: PDF pages, one page per PowerPoint slide and per Excel sheet, one page per image, one page per block of time in a transcript. Word, HTML, Markdown and text files have no pages, so each is stored as a single page 1.

Changing the list makes the next sync list the whole source once, so files of a newly added format are found; files that are already indexed are skipped by content hash. Removing a format doesn't remove documents already indexed in it.

### Transcripts

Timestamped transcripts, WebVTT (`.vtt`) and SubRip (`.srt`), are cut into fixed blocks of time, 5 minutes unless `ALEXANDRIA_EXTRACT_TRANSCRIPT_BLOCK_MINUTES` says otherwise, and each block is one page that starts with its time range:

```markdown
## [00:15:00–00:20:00]

Alice: What we found was that the effect only shows up in winter.

Bob: And only in the north.
```

So `search` and `get_pages` work as for any other document, a search hit names its time range, and a citation points to a time: page N always covers minutes (N−1)×5 to N×5. A block in which nothing was said is still a page, `(nothing transcribed in this block)`, so the numbering never shifts. Consecutive cues are merged into paragraphs, with speaker names when the transcript has them (`<v Alice>` in WebVTT). Transcripts get the type `transcript` wherever they are, and their title is the one in a WebVTT header (`WEBVTT - Episode 12`), or else the file name; they are not read for authors or looked up on Crossref.

Alexandria doesn't transcribe audio. Use a speech-to-text tool that writes WebVTT or SubRip into the source folder, e.g. `whisper talk.mp3 --output_format vtt`. Changing the block length applies to transcripts ingested afterwards; a transcript already indexed is cut again only when its file changes.

### S3 / RustFS / MinIO

Point `ALEXANDRIA_S3_*` at the S3 API (for RustFS that's port 9000 by default, not the console on 9001), using an access key that can list and read the bucket. Requests use path-style addressing, which RustFS and MinIO expect.

S3 has no change feed, so each sync lists the bucket prefix and compares it with the previous listing, which is kept as the sync cursor. Changed files are found by ETag, and keys missing since the last run are deleted from the index.

### Dropbox

1. Create a scoped app at <https://www.dropbox.com/developers/apps> with **App folder** access and the permissions `files.metadata.read` and `files.content.read`.
2. Run the one-off PKCE flow with the app's key. No app secret is needed:

   ```bash
   uv run --extra cpu alexandria-ingest auth --app-key <app key>
   ```

   Open the link, allow access, and paste the code. Then store the app key as `ALEXANDRIA_DROPBOX_APP_KEY` and the printed refresh token as `ALEXANDRIA_DROPBOX_REFRESH_TOKEN`. The refresh token stays valid until you revoke the app; the SDK uses it to get short-lived access tokens as needed.

### SharePoint / OneDrive for Business / Teams

Alexandria reads one document library through Microsoft Graph, signed in as an app of its own, so syncs run without a user. Teams channel files live in the team's SharePoint site (library `Documents`, one folder per channel), so they work the same way.

1. In the [Entra admin center](https://entra.microsoft.com), under **App registrations**, register an app (single tenant, no redirect URI). Note its **Application (client) id** and **Directory (tenant) id**, and create a **client secret** under **Certificates & secrets**.
2. Under **API permissions**, add the Microsoft Graph *application* permission `Sites.Selected`, and have an admin **grant admin consent**. `Sites.Selected` on its own gives access to nothing.
3. Have an admin give the app read access to the site, for example in [Graph Explorer](https://developer.microsoft.com/graph/graph-explorer), signed in as someone allowed to manage the site's permissions:

   ```http
   GET  https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/Finance      → note "id"
   POST https://graph.microsoft.com/v1.0/sites/{site id}/permissions
   {"roles": ["read"], "grantedToIdentities": [{"application": {"id": "<client id>", "displayName": "Alexandria"}}]}
   ```

   The broader alternative is the application permission `Files.Read.All`, which reads every site and OneDrive in the tenant.
4. Set `ALEXANDRIA_SOURCE_KIND=sharepoint` and the `ALEXANDRIA_SHAREPOINT_*` settings, then list the site's libraries and check access:

   ```bash
   uv run --extra cpu alexandria-ingest sharepoint-libraries
   uv run --extra cpu alexandria-ingest check-source
   ```

For a OneDrive for Business drive, set `ALEXANDRIA_SHAREPOINT_DRIVE_ID` instead of the site (`GET /users/{user}/drive` returns its id); the app then needs `Files.Read.All`.

Files are keyed by path, as with Dropbox, so a renamed or moved file, or the contents of a renamed folder, is ingested again under its new path.

### Document metadata

Each document gets a title, authors, a year, a DOI and a type, which `list_documents` and search results show so an agent can tell documents apart and cite them, and which both tools can filter on (`doc_type`, `year_from`, `year_to`, next to the folder filter `path_prefix`). During ingest they're read from the document's first page: the first heading that isn't a section name is the title, a line of names below it the authors, and a copyright notice or a date the year. If that page has a DOI, Crossref is asked about it and what it has wins (turn this off with `ALEXANDRIA_METADATA_CROSSREF=false`). Whatever isn't found stays empty, and the title falls back to the file name.

The type comes from the folder: `ALEXANDRIA_METADATA_DOC_TYPES` maps folders to `paper`, `book`, `policy`, `transcript` or `other`, e.g. `papers=paper,reports/policy=policy`. The deepest matching folder wins, folders are matched without regard to case, and a document in no listed folder gets Crossref's type for its DOI, or `other`. Folders are paths as the source shows them, so with an S3 prefix or a Dropbox path they start with that prefix or path.

Documents ingested before metadata existed keep their file-name titles until they change. To fill in their metadata without downloading or extracting them again, run once:

```bash
uv run --extra cpu alexandria-ingest backfill-metadata --dry-run   # show what would change
uv run --extra cpu alexandria-ingest backfill-metadata
```

It reads each document's stored first page, plus a Crossref lookup per DOI found, and lists the documents whose metadata changed. Chunks are embedded with their document's title, so run the `reindex` flow afterwards for search to use the new titles too.

### Duplicates

The same file under two paths (a copy in another folder, a second upload) is searched once. During ingest, each document's extracted text, with whitespace collapsed, is hashed into a content digest. A document whose digest matches a searchable document's is stored as its duplicate: its pages are kept, so `get_pages` can read it, but it isn't embedded and `search` never returns it. `list_documents` marks it `DUPLICATE of doc N`, so the copy can be removed at the source.

The digest is taken from the text, not the file's bytes, so a re-saved or re-exported copy whose bytes differ still matches, and documents already stored get a digest from their stored pages without being downloaded again. The cost is that a copy is still downloaded and extracted before it is recognised; only embedding is skipped. The source's own content hash can't be used: it is per source (an S3 ETag differs between a single and a multipart upload of the same bytes).

When the original is deleted, fails to ingest, or changes so that its text no longer matches, its duplicates are recorded as failed, and the next sync ingests them again like any failed file: the first one becomes searchable, any others its duplicates.

Documents ingested before duplicates were detected have no digest. To give them one and list the copies already in the library, run once:

```bash
uv run --extra cpu alexandria-ingest duplicates
```

It prints one line per copy: its id and path, the id and path of the document search uses instead, and whether the copy is still searched. Copies ingested before this release are, until they're deleted at the source or re-ingested (the `reindex` flow re-embeds every document and turns such copies into duplicates).

### Headers and front matter

Running headers (the journal name and page number on every page) and a leading page of publisher notices (terms of use, copyright, "downloaded from") add the same text to many chunks, which crowds out real matches in search. So they're left out of what is chunked and embedded; see the `ALEXANDRIA_BOILERPLATE_*` settings. This only affects the search index: the stored pages, and so `get_pages`, keep every notice exactly as it is in the file, and your files are never changed.

## Ingest

Ingest runs as Prefect flows, served from the machine that does the work (ideally one with a GPU):

```bash
export PREFECT_API_URL=http://<prefect-server>:4200/api   # or run `uv run --extra cu128 prefect server start` locally
export PREFECT_LOGGING_EXTRA_LOGGERS=alexandria_ingest     # show pipeline logs in the Prefect UI
uv run --extra cu128 alexandria-workflows serve
```

This serves two deployments, `sync/sync` and `reindex/reindex`, from that process. To run them on a Prefect worker instead, register them once with `uv run alexandria-workflows deploy` (on the work pool `alexandria`, created if missing; `--work-pool` picks another) and start a worker where the work happens: `uv run --extra cu128 prefect worker start --pool alexandria`. With `ALEXANDRIA_SYNC_INTERVAL` set, `sync` runs on that interval; otherwise, and always for `reindex`, start a run from the Prefect UI, with `uv run --extra cu128 prefect deployment run sync/sync`, or from a Prefect automation or webhook. Each deployment runs one flow run at a time; a run started while another is going waits for it to finish.

- `sync` ingests what changed in the source since the last run, plus files that failed before. The first run creates the database tables.
- `reindex` re-chunks and re-embeds every stored document from its stored pages, without touching the source. Run it after changing the embedding model or the chunk size.

Within a run, the listing and each file's steps (triage, prepare, store) are separate Prefect tasks with their own retries, so a short outage of the source or Postgres only retries the step that hit it. A file that still fails after its retries is recorded with `status = failed` and retried on the next run. See [alexandria-workflows](workflows/README.md) for details.

## MCP server

```bash
uv run --extra cpu alexandria-mcp --transport stdio
```

It loads the embedding model on the CPU at startup, which takes a few seconds, and only reads from the database, apart from migrating it to the newest schema on start (see [Upgrading](#upgrading)). `--transport streamable-http` serves on `127.0.0.1:8000` (`ALEXANDRIA_MCP_HOST`, `ALEXANDRIA_MCP_PORT`) **without authentication**; don't expose it beyond localhost without an authenticating proxy in front. The tools are described in [alexandria-mcp](packages/mcp/README.md).

## Evaluating retrieval

`alexandria-eval` scores retrieval against questions with known answers: for each, the document (by path) and the pages that answer it. It asks each question through the same search the MCP server uses (same settings, same embedding model on the CPU, and the same reranker when `ALEXANDRIA_RERANK_MODEL` is set) and prints recall@5, recall@10 and mean reciprocal rank (MRR) at two levels: **document** (a hit from the expected document) and **page** (a hit from that document whose pages overlap the expected range).

The questions are about your own library, so they aren't in the repository. Copy the example and replace its made-up entries with 30–50 questions of your own:

```bash
cp eval/questions.example.yaml eval/questions.yaml                 # git-ignored
make eval                                                          # = uv run --no-sync alexandria-eval
uv run --extra cpu alexandria-eval eval/questions.yaml --details   # also list each question's ranks
```

It needs `ALEXANDRIA_DATABASE_URL` and the embedding settings, like the MCP server, and like the server it only reads from the database, apart from migrating it to the newest schema on start. It is not part of CI, because it needs a real library: run it before and after a change that touches retrieval (chunking, metadata, the search query, the embedding model, reranking) and paste both tables into the pull request. A warning lists expected paths that aren't in the library.

## Connect Claude

### With Docker

The compose file runs the MCP server over HTTP on <http://localhost:8000/mcp>.

**Claude Code** connects to it directly:

```bash
claude mcp add --transport http alexandria http://localhost:8000/mcp
```

**Claude Desktop** starts its own stdio server in the running container, which already has the settings and the model cache. Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "alexandria": {
      "command": "docker",
      "args": ["exec", "-i", "alexandria-mcp-1", "alexandria-mcp", "--transport", "stdio"]
    }
  }
}
```

Each session loads the embedding model first, which takes a few seconds.

### From source

Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "alexandria": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/alexandria", "--extra", "cpu", "alexandria-mcp", "--transport", "stdio"]
    }
  }
}
```

`--directory` makes the repository's `.env` the configuration. If Claude Desktop runs on Windows and the repository lives in WSL, launch it through `wsl.exe`:

```json
{
  "mcpServers": {
    "alexandria": {
      "command": "wsl.exe",
      "args": ["--cd", "/path/to/alexandria", "--", "/home/<you>/.local/bin/uv", "run", "--extra", "cpu", "alexandria-mcp", "--transport", "stdio"]
    }
  }
}
```

If the same checkout also runs ingest on the GPU, use `--extra cu128` here too, so the two don't keep switching the environment's PyTorch build.

## Upgrading

With Docker, the downloaded `compose.yaml` pins the release it came from. To upgrade, read the [release notes](https://github.com/tingiskhan/alexandria/releases), download the new release's `compose.yaml` (or set `ALEXANDRIA_VERSION` in `.env`), and run `docker compose up -d`.

The database schema is versioned with [Alembic](https://alembic.sqlalchemy.org/) migrations, which ship inside the `alexandria-core` package (and so in the image). Every sync, the MCP server, `alexandria-eval`, `alexandria-ingest backfill-metadata` and `alexandria-ingest duplicates` migrate the database to the newest schema on start, so upgrading needs nothing else: start the new release and the first process to reach the database migrates it. They take a Postgres advisory lock while they do, so a worker and the MCP server starting at the same time don't both migrate.

A database created before migrations existed has the tables but no `alembic_version` table. The first start of a release with migrations works out which revision its tables match (the first release's tables, with the document metadata columns, or also with the duplicate detection columns), records it, and upgrades from there; the data stays as it is.

`alexandria-db` runs the migrations by hand, with the same `ALEXANDRIA_DATABASE_URL`, e.g. to migrate before starting anything or to go back after a failed upgrade:

```bash
alexandria-db current                         # the database's revision
alexandria-db history                         # every revision, newest first
alexandria-db upgrade                         # to the newest revision (head); or name one
alexandria-db downgrade 0002_document_metadata  # back to a revision; data in what it removes is lost
```

With Docker: `docker compose exec worker-gpu alexandria-db current` (or `worker-cpu`, or `mcp`). Back up the database (`pg_dump`) before downgrading: `downgrade base` drops every table.

## Development

```bash
uv sync --extra cu128                      # or --extra cpu
uv run --extra cu128 ruff check && uv run --extra cu128 ruff format --check && uv run --extra cu128 ty check
uv run --extra cu128 pytest --cov          # unit + integration (needs Docker); fails below 95% coverage
uv run --extra cu128 pytest -m gpu         # real Docling and embedding model on CUDA
```

CI runs the same checks, with coverage, on the CPU build. The GPU tests only run locally.

See [CONTRIBUTING.md](CONTRIBUTING.md) for how the code is organised and what a change needs.

## License

[MIT](LICENSE)
