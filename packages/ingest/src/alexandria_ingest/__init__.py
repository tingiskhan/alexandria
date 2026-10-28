"""Alexandria ingest: turns PDFs from a file source into searchable, embedded chunks in the document store.

- `sources`: where PDFs come from (S3/RustFS, Dropbox), behind the `FileSource` protocol.
- `extract`: PDF to per-page Markdown with Docling.
- `chunk`: per-page Markdown to heading-aware chunks.
- `pipeline`: the steps that tie them together, with every dependency passed in.
- `cli`: the `alexandria-ingest` command (Dropbox `auth`, `check-source`).

Scheduling lives in alexandria-workflows; nothing here imports Prefect.
"""
