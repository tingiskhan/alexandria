# alexandria-core

The shared base of Alexandria: domain types, the protocols every adapter implements, the Postgres schema and its migrations, `PgStore` and hybrid search. It depends on SQLAlchemy, Alembic, psycopg and pgvector, but not on PyTorch, Docling or Prefect, so every other package can import it cheaply.

## Modules

| Module | What it holds |
| --- | --- |
| `models.py` | Frozen dataclasses passed between packages: `FileChange`, `Page`, `Document`, `Chunk`, `EmbeddedChunk`, `SearchQuery`, `SearchHit`, `DocumentSummary`. |
| `protocols.py` | `FileSource`, `Extractor`, `Embedder`, `Reranker` and `DocumentStore`. Code depends on these, never on a concrete adapter, so tests pass fakes. |
| `db.py` | SQLAlchemy tables, `make_engine` and `wait_for_database`. `EMBEDDING_DIMENSION` (1024) and `TS_CONFIG` (`simple`) are fixed here. |
| `migrate.py` | `migrate`: runs the Alembic migrations under an advisory lock, stamping a database from before migrations with the revision it matches first. Also `downgrade`, `alembic_config` and `next_revision_id`. |
| `migrations/` | Alembic's `env.py`, the revision template and `versions/`, one file per revision. |
| `cli.py` | The `alexandria-db` command: `upgrade`, `downgrade`, `current`, `history` and `revision`. |
| `store.py` | `PgStore`, the Postgres `DocumentStore`. |
| `search.py` | `hybrid_search`: vector and full-text candidates fused with Reciprocal Rank Fusion. |
| `rerank.py` | `rerank`: reorders search hits by a `Reranker`'s scores, for the optional reranking step. |
| `settings.py` | `settings_config`, the convention every settings class follows, and `DatabaseSettings`. |

The ORM rows stay inside `db.py` and `store.py`. Everything else sees only the dataclasses from `models.py`.

## Schema

| Table | Holds |
| --- | --- |
| `documents` | One row per source file: `source_id` (unique), path, metadata, content hash, page count, `status` (`ok` or `failed`), the last error, the content digest of its text and, for a duplicate, `duplicate_of` (the searchable document with the same text). |
| `pages` | The extracted Markdown of every page, so the agent can read exact pages, not just chunks. |
| `chunks` | Chunk text, page range, heading path, `embedding vector(1024)`, the model that made it, and a generated `tsvector`. HNSW index on the embedding (cosine), GIN on the `tsvector`. |
| `sync_state` | The cursor for each source, keyed by source name. |

Pages and chunks are deleted with their document (`ON DELETE CASCADE`). A duplicate has pages but no chunks, so search never returns it; `PgStore` records it as failed, to be ingested again, when its original is deleted, fails or changes. The migrations create the `vector` extension and the tables, and change them; every entry point calls `migrate` on start. See [Upgrading](../../README.md#upgrading), and [Adding a migration](../../CONTRIBUTING.md#adding-a-migration) for changing the schema.

## Search

`hybrid_search` runs one SQL statement:

1. The 50 nearest chunks by cosine distance (HNSW, with `hnsw.iterative_scan` so filtered searches still return 50).
2. The 50 best chunks by `ts_rank_cd` for `websearch_to_tsquery`, so quotes and `-exclude` work.
3. Both lists fused with Reciprocal Rank Fusion (`k = 60`), so the two scores never need to be on the same scale.

Only chunks embedded by the query's model are searched. The optional `document_ids` and `DocumentFilter` (`path_prefix`, `doc_type`, `year_from`, `year_to`) filters are applied inside both candidate lists, so a filtered search still fills its limit when enough chunks match. `PgStore.list_documents` takes the same `DocumentFilter`, translated by the same `document_conditions`, so both agree on what a filter matches. Requires pgvector 0.8 or newer.

The MCP server can rerank the fused hits with a cross-encoder (`ALEXANDRIA_RERANK_MODEL`): it asks `hybrid_search` for more hits (`ALEXANDRIA_RERANK_CANDIDATES`, 50 by default), and `rerank` reorders them by the reranker's score and keeps the requested number. The SQL is the same either way.

## Tests

```bash
uv run pytest packages/core    # integration tests start pgvector/pgvector in Docker via testcontainers
```
