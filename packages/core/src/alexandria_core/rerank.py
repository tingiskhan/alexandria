"""Optional reranking of hybrid-search hits with a `Reranker`, such as a cross-encoder."""

from dataclasses import replace

from alexandria_core.models import SearchHit
from alexandria_core.protocols import Reranker


def rerank(query: str, hits: list[SearchHit], reranker: Reranker, limit: int) -> list[SearchHit]:
    """Reorder `hits` by the reranker's score for `query` and return the best `limit`.

    Each returned hit carries the reranker's score. Ties keep their original (Reciprocal Rank Fusion) order.

    Args:
        query: The search query, as the user wrote it.
        hits: Candidates from hybrid search, best first.
        reranker: Scores each hit's text against the query.
        limit: How many hits to return.

    Returns:
        At most `limit` hits, best first.
    """
    if not hits:
        return []
    scores = reranker.score(query, [hit.text for hit in hits])
    if len(scores) != len(hits):
        raise ValueError(f"{reranker.model_name} returned {len(scores)} scores for {len(hits)} texts")
    order = sorted(range(len(hits)), key=lambda i: -scores[i])  # sorted is stable, so ties stay in RRF order
    return [replace(hits[i], score=scores[i]) for i in order[:limit]]
