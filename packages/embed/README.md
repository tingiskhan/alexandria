# alexandria-embed

`SentenceTransformerEmbedder`, the `Embedder` used by both ingest (documents, on the GPU when there is one) and the MCP server (queries, on the CPU). It is a package of its own so that PyTorch is only pulled in where it is needed.

- The model and its revision are pinned (`ALEXANDRIA_EMBEDDING_MODEL`, `ALEXANDRIA_EMBEDDING_REVISION`; BGE-M3 by default). Ingest and the MCP server must use the same pair: chunks record the model that embedded them, and search only compares vectors from the same model.
- Vectors are L2-normalised, so cosine distance in pgvector works.
- `encode_query` and `encode_document` apply any query or document prompt the model's config defines, so models that need an instruction prefix work without code changes.
- The schema stores 1024-dimensional vectors. Entry points refuse to start with a model of another dimension.

To change the model, set both settings and run the `reindex` flow. It re-embeds the stored pages; nothing is downloaded or extracted again.

## PyTorch build

PyTorch comes in through sentence-transformers. Choose the build when you install, from the repository root:

```bash
uv sync --extra cpu      # CPU only (any platform)
uv sync --extra cu128    # CUDA 12.8; needed for RTX 50xx (Blackwell) cards
```

## Tests

```bash
uv run --extra cu128 pytest packages/embed -m gpu    # loads the real model
```
