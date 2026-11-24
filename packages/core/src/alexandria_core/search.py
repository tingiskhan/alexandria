"""Hybrid search: vector and full-text candidates fused with Reciprocal Rank Fusion, in one SQL statement."""

from functools import cache

from pgvector.sqlalchemy import Vector
from sqlalchemy import Connection, TextClause, bindparam, text

from alexandria_core.db import EMBEDDING_DIMENSION, TS_CONFIG, document_metadata
from alexandria_core.models import DocumentFilter, SearchHit, SearchQuery

CANDIDATES = 50
RRF_K = 60

# Vector and full-text candidates (top 50 each), fused with Reciprocal Rank Fusion.
# Ranks are numbered outside the LIMITed subqueries so the HNSW and GIN indexes can stop early.
# The document filters sit inside both subqueries, so a filtered search still gets full candidate lists and `:k`
# matching hits when that many exist. /*documents*/ is replaced by the `documents` conditions a query sets, if any.
_HYBRID_TEMPLATE = """
    WITH vec AS (
      SELECT id, row_number() OVER (ORDER BY distance) AS r
      FROM (
        SELECT id, embedding <=> :qvec AS distance
        FROM chunks
        WHERE model_name = :model
          AND (CAST(:doc_ids AS integer[]) IS NULL OR document_id = ANY(CAST(:doc_ids AS integer[])))
          /*documents*/
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
          /*documents*/
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
    SELECT c.document_id, d.title, d.authors, d.year, d.doi, d.doc_type, d.path,
           c.page_start, c.page_end, c.section, c.text, fused.score
    FROM fused
    JOIN chunks c ON c.id = fused.id
    JOIN documents d ON d.id = c.document_id
    ORDER BY fused.score DESC, c.id
    """


def document_conditions(filters: DocumentFilter) -> tuple[list[str], dict[str, object]]:
    """Translate `filters` into SQL conditions on the `documents` table's columns, and their bind parameters.

    The conditions name columns without a table, so they fit any statement that selects from `documents` alone.
    They are fixed strings; values only ever travel as bind parameters.

    Args:
        filters: The filters to translate.

    Returns:
        The conditions, all of which must hold (empty when nothing filters), and the parameters they bind.
    """
    conditions: list[str] = []
    params: dict[str, object] = {}
    if filters.path_prefix:
        conditions.append(r"path ILIKE :path_pattern ESCAPE '\'")
        params["path_pattern"] = _escape_like(filters.path_prefix) + "%"
    if filters.doc_type is not None:
        # Rows from before the doc_type column existed hold NULL, which reads as "other".
        conditions.append("coalesce(doc_type, 'other') = :doc_type")
        params["doc_type"] = filters.doc_type
    if filters.year_from is not None:
        conditions.append("year >= :year_from")
        params["year_from"] = filters.year_from
    if filters.year_to is not None:
        conditions.append("year <= :year_to")
        params["year_to"] = filters.year_to
    return conditions, params


def _escape_like(value: str) -> str:
    """Escape LIKE's wildcards (and the escape character itself) so `value` matches literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@cache
def _hybrid_sql(conditions: tuple[str, ...]) -> TextClause:
    """The hybrid statement with `conditions` on `documents` in both candidate subqueries, built once per set."""
    # The conditions are fixed strings from `document_conditions`; values are bind parameters.
    where = " AND ".join(conditions)
    document_filter = f"AND document_id IN (SELECT id FROM documents WHERE {where})" if conditions else ""  # noqa: S608
    statement = text(_HYBRID_TEMPLATE.replace("/*documents*/", document_filter))
    return statement.bindparams(bindparam("qvec", type_=Vector(EMBEDDING_DIMENSION)))


def hybrid_search(conn: Connection, query: SearchQuery) -> list[SearchHit]:
    """Run `query` and return the best chunks, best first.

    Must run inside a transaction: it sets `hnsw.iterative_scan` with SET LOCAL.
    """
    # Filtered HNSW scans can return fewer rows than LIMIT; iterative scans keep going (pgvector >= 0.8).
    conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    conditions, filter_params = document_conditions(query.filters)
    rows = conn.execute(
        _hybrid_sql(tuple(conditions)),
        {
            **filter_params,
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
            metadata=document_metadata(row.title, row.authors, row.year, row.doi, row.doc_type),
            path=row.path,
            page_start=row.page_start,
            page_end=row.page_end,
            section=row.section,
            text=row.text,
            score=float(row.score),
        )
        for row in rows
    ]
