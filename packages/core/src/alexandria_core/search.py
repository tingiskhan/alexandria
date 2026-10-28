"""Hybrid search: vector and full-text candidates fused with Reciprocal Rank Fusion, in one SQL statement."""

from pgvector.sqlalchemy import Vector
from sqlalchemy import Connection, bindparam, text

from alexandria_core.db import EMBEDDING_DIMENSION, TS_CONFIG
from alexandria_core.models import SearchHit, SearchQuery

CANDIDATES = 50
RRF_K = 60

# Vector and full-text candidates (top 50 each), fused with Reciprocal Rank Fusion.
# Ranks are numbered outside the LIMITed subqueries so the HNSW and GIN indexes can stop early.
# The document filter sits inside both subqueries, so a single-document search still gets full candidate lists.
_HYBRID_SQL = text(
    """
    WITH vec AS (
      SELECT id, row_number() OVER (ORDER BY distance) AS r
      FROM (
        SELECT id, embedding <=> :qvec AS distance
        FROM chunks
        WHERE model_name = :model
          AND (CAST(:doc_ids AS integer[]) IS NULL OR document_id = ANY(CAST(:doc_ids AS integer[])))
        ORDER BY distance
        LIMIT :candidates
      ) v
    ), fts AS (
      SELECT id, row_number() OVER (ORDER BY rank DESC) AS r
      FROM (
        SELECT id, ts_rank_cd(tsv, q) AS rank
        FROM chunks, websearch_to_tsquery(CAST(:cfg AS regconfig), :qtext) q
        WHERE tsv @@ q
          AND model_name = :model
          AND (CAST(:doc_ids AS integer[]) IS NULL OR document_id = ANY(CAST(:doc_ids AS integer[])))
        ORDER BY rank DESC
        LIMIT :candidates
      ) f
    ), fused AS (
      SELECT id, sum(1.0 / (:rrf_k + r)) AS score
      FROM (SELECT * FROM vec UNION ALL SELECT * FROM fts) u
      GROUP BY id
      ORDER BY score DESC
      LIMIT :k
    )
    SELECT c.document_id, d.title, d.path, c.page_start, c.page_end, c.section, c.text, fused.score
    FROM fused
    JOIN chunks c ON c.id = fused.id
    JOIN documents d ON d.id = c.document_id
    ORDER BY fused.score DESC, c.id
    """
).bindparams(bindparam("qvec", type_=Vector(EMBEDDING_DIMENSION)))


def hybrid_search(conn: Connection, query: SearchQuery) -> list[SearchHit]:
    """Run `query` and return the best chunks, best first.

    Must run inside a transaction: it sets `hnsw.iterative_scan` with SET LOCAL.
    """
    # Filtered HNSW scans can return fewer rows than LIMIT; iterative scans keep going (pgvector >= 0.8).
    conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows = conn.execute(
        _HYBRID_SQL,
        {
            "qvec": query.embedding,
            "qtext": query.text,
            "cfg": TS_CONFIG,
            "model": query.model_name,
            "doc_ids": list(query.document_ids) if query.document_ids is not None else None,
            "candidates": CANDIDATES,
            "rrf_k": RRF_K,
            "k": query.limit,
        },
    )
    return [
        SearchHit(
            document_id=row.document_id,
            title=row.title,
            path=row.path,
            page_start=row.page_start,
            page_end=row.page_end,
            section=row.section,
            text=row.text,
            score=float(row.score),
        )
        for row in rows
    ]
