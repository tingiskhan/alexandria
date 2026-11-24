# alexandria-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that lets an LLM agent (Claude Desktop, Claude Code, or any MCP client) search the library and read exact pages to cite them.

## Tools

| Tool | Arguments | Returns |
| --- | --- | --- |
| `search` | `query`, and optionally `document_ids`, `path_prefix`, `doc_type`, `year_from`, `year_to`; `limit` (1–50, default 10) | Ranked excerpts, each headed `[doc 12] Annual report 2025 — Jane Doe (2025) — pp. 41–42` (authors and year where known) |
| `list_documents` | optionally `path_prefix`, `doc_type`, `year_from`, `year_to`; `offset`, `limit` (1–500, default 200) | One line per document: id, title, authors, year, type, page count, DOI, path and ingest date; a duplicate ends in `DUPLICATE of doc N` |
| `get_pages` | `document_id`, `first_page`, `last_page` (at most 20 pages) | The full Markdown of those pages; a transcript's pages are blocks of time, each starting with its time range |

The filters work the same in both tools and are applied in SQL: `path_prefix` is a case-insensitive path prefix (one folder, e.g. `/reports/2025`), `doc_type` one of `paper`, `book`, `policy`, `transcript`, `other`, and `year_from` / `year_to` an inclusive range that leaves out documents with an unknown year. All filters given must match. A filtered search still returns `limit` hits when that many match.

All tools are annotated read-only. Output is plain text rather than JSON, which is easier for a model to read, and every response is capped at 40,000 characters so one call can't flood the agent's context. The tool docstrings are the prompts the agent sees, so edit them with care.

## Running

```bash
uv run --extra cpu alexandria-mcp --transport stdio
```

The server needs `ALEXANDRIA_DATABASE_URL` and the same embedding settings as ingest (see the [root README](../../README.md#configuration)). It loads the embedding model on the CPU at startup, which takes a few seconds, waits for the database, and migrates it to the newest schema (`migrate`); otherwise it never writes to the database. Setting `ALEXANDRIA_RERANK_MODEL` (e.g. `BAAI/bge-reranker-v2-m3`) also loads a cross-encoder on the CPU that reranks the top `ALEXANDRIA_RERANK_CANDIDATES` (50) search candidates; it is off by default.

`--transport streamable-http` serves MCP over HTTP on `127.0.0.1:8000`, or wherever `ALEXANDRIA_MCP_HOST` and `ALEXANDRIA_MCP_PORT` say (`McpSettings` in `settings.py`); the Docker deployment sets `0.0.0.0` inside the container and publishes the port on localhost. It has **no authentication**: keep it on localhost, or put it behind a reverse proxy that authenticates before you expose it. It refuses requests addressed to anything but localhost, so web pages can't reach it by DNS rebinding; add a proxy's name to `ALEXANDRIA_MCP_ALLOWED_HOSTS`.

## Code

- `server.py`: `build_server(store, embedder, reranker)` builds the server from any `DocumentStore`, `Embedder` and optional `Reranker`. Each tool validates its arguments, calls the store, and formats the result.
- `retrieval.py`: `Retrieval`, the search both the `search` tool and `alexandria-eval` run: hybrid search, then, with a reranker, more candidates (all matching the filters) reordered before returning `limit`.
- `main.py`: the composition root. `build_retrieval()` loads settings, waits for the database, migrates it, and builds the real store, embedder and (when `ALEXANDRIA_RERANK_MODEL` is set) reranker; `main` serves them.
- `settings.py`: `McpSettings`, where the HTTP transport listens.
- `evaluation.py`: loads a questions file (`eval/questions.yaml` by default; see `eval/questions.example.yaml`), runs each question through `Retrieval.search`, as the `search` tool does, and scores recall@k and MRR at document and page level. `eval_cli.py` is the `alexandria-eval` command around it, built exactly like the server (`build_retrieval()` in `main.py`), so it measures what the server serves, reranker included. See [Evaluating retrieval](../../README.md#evaluating-retrieval).

## Tests

```bash
uv run pytest packages/mcp    # calls the tools in-process against fakes
```
