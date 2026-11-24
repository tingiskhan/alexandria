# alexandria-ingest

Turns documents (PDF, Word, PowerPoint, Excel, HTML, Markdown, text, images, transcripts) from a file source into searchable chunks in the document store:

```text
FileSource.changes ──► download ──► FormatRouter ──► chunk_pages ──► Embedder ──► DocumentStore.upsert_document
(S3 / Dropbox / SharePoint)         Docling, or      heading-aware    BGE-M3       pages + chunks, one transaction
                                    transcripts;
                                    per-page Markdown
```

This package holds the steps only. Scheduling, retries and the composition root are in [`alexandria-workflows`](../../workflows), which is the only code that imports Prefect. The protocols the steps depend on (`FileSource`, `Extractor`, `Embedder`, `DocumentStore`) live in `alexandria_core.protocols`.

## Modules

| Module | What it does |
| --- | --- |
| `pipeline.py` | The steps for one file (`triage`, `prepare_file`, `store_document`, or all three via `process_file`), `sync_changes` for a whole sync, and `reindex_document` to re-embed what is already stored. Every dependency is a keyword argument. |
| `extract.py` | `DoclingExtractor`: document bytes to one Markdown string per page, with OCR for scanned pages and images and table structure recognition. The file name picks the format; formats without pages (Word, HTML, Markdown, text) come out as one page. Uses the GPU when one is available. `FormatRouter` hands each file to the extractor for its extension, and `make_extractor` builds the one a sync uses from `ExtractSettings`: transcripts to `TranscriptExtractor`, the rest to `DoclingExtractor`. |
| `transcript.py` | `TranscriptExtractor`: WebVTT and SubRip transcripts to one page per fixed block of time (`ALEXANDRIA_EXTRACT_TRANSCRIPT_BLOCK_MINUTES`), each starting with a `## [00:15:00–00:20:00]` heading, with cues merged into paragraphs by speaker. Page N is always block N; a block without speech is a page that says so. A small hand-written parser, no dependencies. |
| `metadata.py` | `MetadataReader`: a document's title, authors, year, DOI and type from its path and first page, asking a `BibliographyLookup` about a DOI it finds. `make_metadata_reader` builds it from `MetadataSettings`. |
| `crossref.py` | `CrossrefLookup`, the `BibliographyLookup` on Crossref's REST API. Failures are logged and return None, so they never stop an ingest. |
| `chunk.py` | `chunk_pages` and `embedding_text`. Pure functions, no I/O. |
| `boilerplate.py` | `strip_boilerplate`: drops running headers and a front-matter notice page from the chunker's input (not for transcripts). Pure functions, no I/O. |
| `sources/` | One package per file source (settings, protocols, client, source), plus `make_source()`, which builds the configured one. |
| `settings.py` | `ChunkSettings`, `BoilerplateSettings`, `ExtractSettings` (which file extensions to ingest), `MetadataSettings` and `SourceSettings`. Each source's settings sit next to the source itself. |
| `doctags.py` | Keeps literal `<` in DocTags model output, which docling-core's parser would otherwise drop along with the text after it. |
| `latex.py` | `normalize_math`: the stored maths convention, inline `\(...\)` and display `$$...$$` with no Markdown escapes inside. |
| `katex.py` | `katex_error`: checks a formula with the vendored KaTeX, without Node. |
| `cli.py` | The `alexandria-ingest` command. |

## How a sync works

`sync_changes(source_name, list_changes, store, process)`:

1. Reads the cursor stored under `source_name` (the source's name plus its formats, from `cursor_key`, so adding a format triggers one full listing) and calls `list_changes` (normally `source.changes`) to get what changed since then.
2. Adds documents that failed on an earlier run, so they are retried.
3. Calls `process` for each change. If a file raises an exception, it is recorded with `status = failed` and the run continues.
4. Saves the new cursor, even if some files failed. Those files are picked up again by step 2 next time.

Each change goes through up to three steps. `process_file` runs them in a row. They are separate functions so that a scheduler can retry each one on its own. In alexandria-workflows each step is a Prefect task, so a database restart during the upsert doesn't rerun Docling.

1. **`triage`** (store only) handles what doesn't need a download:
   - deleted: removes the document, along with its pages and chunks;
   - extension not in `ALEXANDRIA_EXTRACT_FORMATS`: skips it;
   - content hash already stored: skips it, so a full re-listing costs nothing for files that haven't changed.

   Otherwise it returns `None`.
2. **`prepare_file`** (source, Docling, embedder) downloads, extracts, chunks and embeds the file, and returns an `IndexedDocument`. It writes nothing, so it is safe to retry.
3. **`store_document`** upserts the result. The upsert replaces the document's pages and chunks in one transaction, so a search never sees half a document.

## Chunking

`chunk_pages(pages, max_tokens)` cuts on Markdown headings first, then packs paragraphs up to `max_tokens` (estimated as 4 characters per token):

- Each chunk records the pages it spans and its heading path (`Results > Revenue`). The heading line stays as the chunk's first line.
- Tables are never split, unless a single table is over the limit. Then it is split by rows, with the header repeated in each part.
- `embedding_text(title, chunk)` puts `title > heading path` in front of the chunk text before embedding, so short chunks keep their context. The stored text is the plain chunk.

Before chunking, `strip_boilerplate(pages, rules)` removes text that isn't part of the document (`ALEXANDRIA_BOILERPLATE_*`):

- lines matching a configured regex, on every page;
- running headers: lines that appear on most pages (60% by default) of a document with at least 4 pages. Case, spacing, heading markers and digits are ignored, so a header carrying the page number still counts as repeated. Table rows and lines with fewer than three letters (`$$`, rules) never do;
- a front-matter first page (publisher notice, terms of use) of a multi-page document, when paragraphs matching the front-matter regexes make up at least half of its text.

Only the chunker's input is stripped. Stored pages keep everything the extractor returned, so `get_pages` still shows the page as it is in the file, and a skipped page keeps its number (its Markdown is just empty for chunking). Because the rules apply to stored pages too, `reindex` picks up changed rules without downloading anything.

Transcripts (`doc_type` `transcript`) are never stripped: their pages are blocks of time with no running headers or front matter, and a line said in most blocks (`Alice: Yes.`) is speech, not a header. The content digest that finds duplicates is taken over the stored pages, so stripping never changes which documents count as the same.

## Sources

`ALEXANDRIA_SOURCE_KIND` picks the source. Run `alexandria-ingest check-source` to check access and see which files a sync would pick up.

| Kind | Module | Change detection | `source_id` |
| --- | --- | --- | --- |
| `s3` | `sources/s3/` | Lists the prefix each run and compares it with the previous listing, which is stored as the cursor (`{key: etag}`) | Object key |
| `dropbox` | `sources/dropbox/` | Dropbox's own cursor (`files/list_folder/continue`), with a full listing if the cursor is reset | `path_lower` (Dropbox reports deletions only by path) |
| `sharepoint` | `sources/sharepoint/` | Microsoft Graph's delta feed on the drive, plus an id → (parent, name) map in the cursor to rebuild the paths the feed leaves out; a full listing if the delta link expires | Lower-cased path |

The settings each source needs are listed in the [root README](../../README.md#configuration) and in [`.env.example`](../../.env.example).

### Adding a source

1. Create a package `sources/<name>/`, laid out like the existing ones:
   - `settings.py`: a `<Name>Settings(BaseSettings)` class, with `model_config = settings_config("ALEXANDRIA_<NAME>_")`;
   - `protocols.py`: the small Protocol the source needs from the vendor SDK or API, so tests can pass a fake (see `DropboxClient`, `ObjectStore` and `GraphClient`);
   - `client.py`: the real implementation of that Protocol, and a factory that builds it from the settings;
   - `source.py`: the source class, with `changes(cursor) -> (changes, new_cursor)` and `download(file_id) -> bytes`;
   - `__init__.py`: re-exports of the public names.

   Anything else gets its own module too, like SharePoint's `errors.py` and `items.py` (the id → path map).
2. Add the name to `SourceSettings.kind`, and add a case to `make_source()` in `sources/__init__.py`.
3. Catch the SDK's exceptions in `check_source` in `cli.py`.

A source has two jobs: report every deletion and give each file a stable content hash. The pipeline takes care of the rest, including skipping unchanged files.

## CLI

```bash
uv run --extra cpu alexandria-ingest check-source             # check access to the configured source and list the files to ingest
uv run --extra cpu alexandria-ingest check-formulas           # run every stored formula through KaTeX, list failures per document and page
uv run --extra cpu alexandria-ingest backfill-metadata        # read metadata again from stored first pages (--dry-run to preview)
uv run --extra cpu alexandria-ingest duplicates               # list documents with the same content; stores missing content digests
uv run --extra cpu alexandria-ingest auth --app-key <key>     # Dropbox only: one-off consent, prints the refresh token
uv run --extra cpu alexandria-ingest sharepoint-libraries     # SharePoint only: the site's document libraries and drive ids
```

## Tests

```bash
uv run --extra cu128 pytest packages/ingest              # unit tests, plus the RustFS integration test (needs Docker)
uv run --extra cu128 pytest packages/ingest -m gpu       # real Docling on tests/data/*.pdf
```

`tests/fakes.py` has in-memory versions of every protocol, used by the pipeline tests. The source tests use fakes for the SDK clients, plus an integration test against a RustFS container. `test_extract.py` runs real Docling (`gpu`); `test_extract_wrapper.py` covers the extractor's own logic with a fake converter.
