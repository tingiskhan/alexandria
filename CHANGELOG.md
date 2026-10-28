# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change the database schema; the release notes will say so.

## Unreleased

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
