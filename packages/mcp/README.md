# alexandria-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that lets an LLM agent (Claude Desktop, Claude Code, or any MCP client) search the library and read exact pages to cite them.

## Tools

| Tool | Arguments | Returns |
| --- | --- | --- |
| `search` | `query`, `document_ids` (optional), `limit` (1–50, default 10) | Ranked excerpts, each headed `[doc 12] Annual report 2025 — pp. 41–42` |
| `list_documents` | `path_prefix` (optional), `offset`, `limit` (1–500, default 200) | One line per document: id, title, page count, path and ingest date |
| `get_pages` | `document_id`, `first_page`, `last_page` (at most 20 pages) | The full Markdown of those pages |

All tools are annotated read-only. Output is plain text rather than JSON, which is easier for a model to read, and every response is capped at 40,000 characters so one call can't flood the agent's context. The tool docstrings are the prompts the agent sees, so edit them with care.

## Running

```bash
uv run --extra cpu alexandria-mcp --transport stdio
```

The server needs `ALEXANDRIA_DATABASE_URL` and the same embedding settings as ingest (see the [root README](../../README.md#configuration)). It loads the embedding model on the CPU at startup, which takes a few seconds, and never writes to the database.

`--transport streamable-http` serves MCP over HTTP on `127.0.0.1:8000`. It has **no authentication**: keep it on localhost, or put it behind a reverse proxy that authenticates before you expose it.

## Code

- `server.py`: `build_server(store, embedder)` builds the server from any `DocumentStore` and `Embedder`. Each tool validates its arguments, calls the store, and formats the result.
- `main.py`: the composition root. It loads settings, builds the real store and embedder, and runs the server.

## Tests

```bash
uv run pytest packages/mcp    # calls the tools in-process against fakes
```
