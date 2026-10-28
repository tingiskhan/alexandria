# alexandria-core

The shared base of Alexandria: domain types, the protocols every adapter implements, the Postgres schema, `PgStore` and hybrid search. It depends on SQLAlchemy, psycopg and pgvector, but not on PyTorch, Docling or Prefect, so every other package can import it cheaply.

## Modules

| Module | What it holds |
| --- | --- |
| `models.py` | Frozen dataclasses passed between packages: `FileChange`, `Page`, `Document`, `Chunk`, `EmbeddedChunk`, `SearchQuery`, `SearchHit`, `DocumentSummary`. |
| `protocols.py` | `FileSource`, `Extractor`, `Embedder` and `DocumentStore`. Code depends on these, never on a concrete adapter, so tests pass fakes. |
| `db.py` | SQLAlchemy tables, `make_engine`, `wait_for_database` and `init_schema`. `EMBEDDING_DIMENSION` (1024) and `TS_CONFIG` (`simple`) are fixed here. |
| `store.py` | `PgStore`, the Postgres `DocumentStore`. |
| `search.py` | `hybrid_search`: vector and full-text candidates fused with Reciprocal Rank Fusion. |
| `settings.py` | `settings_config`, the convention every settings class follows, and `DatabaseSettings`. |

The ORM rows stay inside `db.py` and `store.py`. Everything else sees only the dataclasses from `models.py`.

## Schema

| Table | Holds |
| --- | --- |
| `documents` | One row per source file: `source_id` (unique), path, title, content hash, page count, `status` (`ok` or `failed`) and the last error. |
| `pages` | The extracted Markdown of every page, so the agent can read exact pages, not just chunks. |
| `chunks` | Chunk text, page range, heading path, `embedding vector(1024)`, the model that made it, and a generated `tsvector`. HNSW index on the embedding (cosine), GIN on the `tsvector`. |
| `sync_state` | The cursor for each source, keyed by source name. |

Pages and chunks are deleted with their document (`ON DELETE CASCADE`). `init_schema` creates the `vector` extension and any missing tables. It does not migrate existing tables; see [Upgrading](../../README.md#upgrading).

## Search

`hybrid_search` runs one SQL statement:

1. The 50 nearest chunks by cosine distance (HNSW, with `hnsw.iterative_scan` so filtered searches still return 50).
2. The 50 best chunks by `ts_rank_cd` for `websearch_to_tsquery`, so quotes and `-exclude` work.
3. Both lists fused with Reciprocal Rank Fusion (`k = 60`), so the two scores never need to be on the same scale.

Only chunks embedded by the query's model are searched, and the optional `document_ids` filter is applied inside both candidate lists. Requires pgvector 0.8 or newer.

## Tests

```bash
uv run pytest packages/core    # integration tests start pgvector/pgvector in Docker via testcontainers
```
