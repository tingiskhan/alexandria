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

## Packages

| Package | What | Heavy dependencies |
| --- | --- | --- |
| [`alexandria-core`](packages/core/README.md) | Domain types, protocols, schema, `PgStore`, hybrid search | SQLAlchemy, psycopg, pgvector |
| [`alexandria-embed`](packages/embed/README.md) | `SentenceTransformerEmbedder` (BGE-M3, 1024 dimensions) | PyTorch |
| [`alexandria-ingest`](packages/ingest/README.md) | File sources (S3, Dropbox, SharePoint), Docling extraction, chunking, the pipeline, a CLI | Docling, boto3, Dropbox SDK, azure-identity, httpx |
| [`alexandria-mcp`](packages/mcp/README.md) | Read-only MCP server | mcp SDK |
| [`alexandria-workflows`](workflows/README.md) | Prefect flows; the only code that imports Prefect | Prefect |

How the pieces fit together, and why, is in [docs/architecture.md](docs/architecture.md).

## Quick start

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

Then [run a sync](#ingest) and [connect an agent](#mcp-server). To sync from Dropbox or your own S3 store instead, change the source settings in `.env` (see [Sources](#sources)).

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
| `ALEXANDRIA_S3_ENDPOINT_URL` | `S3Settings` (ingest `sources/s3.py`) | no (AWS) | workflows | e.g. `http://rustfs:9000` |
| `ALEXANDRIA_S3_BUCKET` | `S3Settings` | yes (S3) | workflows | |
| `ALEXANDRIA_S3_PREFIX` | `S3Settings` | no (`""`) | workflows | Only sync keys under this prefix, e.g. `papers/` |
| `ALEXANDRIA_S3_ACCESS_KEY_ID` | `S3Settings` | yes (S3) | workflows | |
| `ALEXANDRIA_S3_SECRET_ACCESS_KEY` | `S3Settings` | yes (S3), secret | workflows | |
| `ALEXANDRIA_S3_REGION` | `S3Settings` | no (`us-east-1`) | workflows | |
| `ALEXANDRIA_DROPBOX_APP_KEY` | `DropboxSettings` (ingest `sources/dropbox.py`) | yes (Dropbox) | workflows | |
| `ALEXANDRIA_DROPBOX_REFRESH_TOKEN` | `DropboxSettings` | yes (Dropbox), secret | workflows | From `alexandria-ingest auth`; valid until revoked |
| `ALEXANDRIA_DROPBOX_PATH` | `DropboxSettings` | no (`""`) | workflows | Folder to sync, starting with `/`; `""` is the app folder root |
| `ALEXANDRIA_SHAREPOINT_TENANT_ID` | `SharePointSettings` (ingest `sources/sharepoint.py`) | yes (SharePoint) | workflows | The Entra ID tenant (directory) id |
| `ALEXANDRIA_SHAREPOINT_CLIENT_ID` | `SharePointSettings` | yes (SharePoint) | workflows | The app registration's application (client) id |
| `ALEXANDRIA_SHAREPOINT_CLIENT_SECRET` | `SharePointSettings` | yes (SharePoint), secret | workflows | |
| `ALEXANDRIA_SHAREPOINT_SITE` | `SharePointSettings` | yes, unless `DRIVE_ID` is set | workflows | e.g. `contoso.sharepoint.com:/sites/Finance` |
| `ALEXANDRIA_SHAREPOINT_LIBRARY` | `SharePointSettings` | no (`Documents`) | workflows | The document library in the site |
| `ALEXANDRIA_SHAREPOINT_DRIVE_ID` | `SharePointSettings` | no | workflows | A drive id instead of site and library, e.g. a OneDrive for Business drive |
| `ALEXANDRIA_SHAREPOINT_PATH` | `SharePointSettings` | no (`""`) | workflows | Folder to sync, starting with `/`; `""` is the whole library |
| `ALEXANDRIA_EMBEDDING_MODEL` | `EmbeddingSettings` (embed) | no (`BAAI/bge-m3`) | workflows, MCP | Must have 1024 dimensions (`EMBEDDING_DIMENSION` in `db.py`) |
| `ALEXANDRIA_EMBEDDING_REVISION` | `EmbeddingSettings` | no (pinned commit) | workflows, MCP | Ingest and MCP must use the same model and revision |
| `ALEXANDRIA_EMBEDDING_BATCH_SIZE` | `EmbeddingSettings` | no (`32`) | workflows | |
| `ALEXANDRIA_CHUNK_MAX_TOKENS` | `ChunkSettings` (ingest) | no (`500`) | workflows | |
| `ALEXANDRIA_EXTRACT_FORMATS` | `ExtractSettings` (ingest) | no (`pdf,docx,pptx,xlsx,html,htm,md,txt`) | workflows | File extensions to ingest, comma-separated; see [File formats](#file-formats) |

To add settings for a new component, define a `BaseSettings` subclass in its package with `model_config = settings_config("ALEXANDRIA_<COMPONENT>_")` from `alexandria_core.settings`, and load it in the entry points that use it.

## Sources

Pick one with `ALEXANDRIA_SOURCE_KIND`, then check that Alexandria can reach it and sees your documents:

```bash
uv run --extra cpu alexandria-ingest check-source
```

Documents are keyed by the source's file id (the S3 key or the Dropbox path), so a moved or renamed file is re-ingested. To add another kind of source, see [Adding a source](packages/ingest/README.md#adding-a-source).

### File formats

`ALEXANDRIA_EXTRACT_FORMATS` lists the file extensions to ingest; other files are skipped. Everything goes through Docling, so any extension Docling reads works. The default is `pdf,docx,pptx,xlsx,html,htm,md,txt`. Add `png,jpg,jpeg,tiff` to OCR images; an unknown extension fails at startup.

Page numbers follow the format: PDF pages, one page per PowerPoint slide and per Excel sheet, one page per image. Word, HTML, Markdown and text files have no pages, so each is stored as a single page 1.

Changing the list makes the next sync list the whole source once, so files of a newly added format are found; files that are already indexed are skipped by content hash. Removing a format doesn't remove documents already indexed in it.

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

## Ingest

Ingest runs as Prefect flows, served from the machine that does the work (ideally one with a GPU):

```bash
export PREFECT_API_URL=http://<prefect-server>:4200/api   # or run `uv run --extra cu128 prefect server start` locally
export PREFECT_LOGGING_EXTRA_LOGGERS=alexandria_ingest     # show pipeline logs in the Prefect UI
uv run --extra cu128 alexandria-workflows serve
```

This serves two deployments, `sync/sync` and `reindex/reindex`. Neither has a schedule: start a run from the Prefect UI, with `uv run --extra cu128 prefect deployment run sync/sync`, or from a Prefect automation or webhook. Each deployment runs one flow run at a time; a run started while another is going waits for it to finish.

- `sync` ingests what changed in the source since the last run, plus files that failed before. The first run creates the database tables.
- `reindex` re-chunks and re-embeds every stored document from its stored pages, without touching the source. Run it after changing the embedding model or the chunk size.

Within a run, the listing and each file's steps (triage, prepare, store) are separate Prefect tasks with their own retries, so a short outage of the source or Postgres only retries the step that hit it. A file that still fails after its retries is recorded with `status = failed` and retried on the next run. See [alexandria-workflows](workflows/README.md) for details.

## MCP server

```bash
uv run --extra cpu alexandria-mcp --transport stdio
```

It loads the embedding model on the CPU at startup, which takes a few seconds, and only reads from the database. `--transport streamable-http` serves on `127.0.0.1:8000` **without authentication**; don't expose it beyond localhost without an authenticating proxy in front. The tools are described in [alexandria-mcp](packages/mcp/README.md).

### Claude Desktop

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

Alexandria has no database migrations yet. The schema is created on the first sync, and existing tables are never altered. Until migrations exist, a release that changes the schema will say so in the [changelog](CHANGELOG.md), and upgrading means dropping the tables (`documents`, `pages`, `chunks`, `sync_state`) and syncing again.

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
