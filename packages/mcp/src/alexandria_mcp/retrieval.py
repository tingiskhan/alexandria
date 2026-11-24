"""Search as the MCP server serves it: hybrid search, then optional reranking. Shared with `alexandria-eval`."""

from collections.abc import Sequence
from dataclasses import dataclass

from alexandria_core.models import DocumentFilter, SearchHit, SearchQuery
from alexandria_core.protocols import DocumentStore, Embedder, Reranker
from alexandria_core.rerank import rerank

RERANK_CANDIDATES = 50


@dataclass(frozen=True)
class Retrieval:
    """What `search` runs on: the store, the query embedder and an optional reranker.

    Attributes:
        store: Where documents are searched and read.
        embedder: Embeds search queries; must be the model that embedded the chunks.
        reranker: Reorders search candidates when given. None keeps the hybrid-search (RRF) order.
        rerank_candidates: How many hybrid-search hits the reranker scores, or the requested limit if larger.
    """

    store: DocumentStore
    embedder: Embedder
    reranker: Reranker | None = None
    rerank_candidates: int = RERANK_CANDIDATES

    def search(
        self,
        query: str,
        *,
        limit: int,
        document_ids: Sequence[int] | None = None,
        filters: DocumentFilter | None = None,
    ) -> list[SearchHit]:
        """Return at most `limit` hits for `query`, best first, reranked when there is a reranker.

        Args:
            query: The search query, as the user wrote it.
            limit: How many hits to return.
            document_ids: Search only these documents; None or empty searches everything.
            filters: Search only documents matching these; None filters nothing.

        Returns:
            The hits. With a reranker, more candidates are fetched; the filters go into that query, so every
            candidate already matches them.
        """
        hits = self.store.search(
            SearchQuery(
                text=query,
                embedding=self.embedder.embed_query(query),
                model_name=self.embedder.model_name,
                document_ids=tuple(document_ids) if document_ids else None,
                filters=filters or DocumentFilter(),
                limit=max(limit, self.rerank_candidates) if self.reranker else limit,
            )
        )
        return rerank(query, hits, self.reranker, limit) if self.reranker else hits
