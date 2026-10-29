# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change the database schema; the release notes will say so.

## Unreleased

### Added

- Ingest more than PDFs: Word, PowerPoint, Excel, HTML, Markdown and text by default, and images (OCR) or anything else Docling reads on request. `ALEXANDRIA_EXTRACT_FORMATS` lists the file extensions to ingest. Slides and sheets become pages; Word, HTML, Markdown and text files are a single page.
- `alexandria-ingest check-source` counts the files to ingest per extension.
- A SharePoint source (`ALEXANDRIA_SOURCE_KIND=sharepoint`): one document library, including Teams channel files, or a OneDrive for Business drive, through Microsoft Graph's delta feed. Signs in as an Entra ID app with a client secret; `Sites.Selected` limits it to the sites it is granted. `alexandria-ingest sharepoint-libraries` lists a site's libraries.

### Changed

- `Extractor.extract` takes the file name as well as the content: `extract(content, name)`.
- The sync cursor is stored under the source name plus the formats. The first sync after upgrading, and after any change to `ALEXANDRIA_EXTRACT_FORMATS`, lists the whole source once; already indexed files are skipped by content hash.

## 0.1.0

First public release.

- Sync PDFs from an S3-compatible store (AWS, RustFS, MinIO) or a Dropbox folder, incrementally, with deletions.
- Docling extraction to per-page Markdown, with OCR and table structure recognition.
- Heading-aware chunking that keeps tables whole.
- BGE-M3 embeddings, stored in Postgres with pgvector.
- Hybrid search: vector and full-text candidates fused with Reciprocal Rank Fusion.
- Read-only MCP server with `search`, `list_documents` and `get_pages`.
- Prefect `sync` and `reindex` flows with per-step retries, one run at a time.
- `cpu` and `cu128` extras to choose the PyTorch build.
- `compose.yaml` with Postgres + pgvector and RustFS for trying it locally.
