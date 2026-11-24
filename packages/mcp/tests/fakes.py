from collections.abc import Sequence
from dataclasses import dataclass, field

from alexandria_core.models import (
    Document,
    DocumentFilter,
    DocumentMetadata,
    DocumentSummary,
    EmbeddedChunk,
    FileChange,
    Page,
    SearchHit,
    SearchQuery,
)


@dataclass
class FakeStore:
    """Read side of DocumentStore with canned data; records the queries and filters it receives.

    Filtering is the real store's job, so `list_documents` returns `documents` as they are. Writes are not used here.
    """

    hits: list[SearchHit] = field(default_factory=list)
    documents: list[DocumentSummary] = field(default_factory=list)
    pages: dict[int, list[Page]] = field(default_factory=dict)
    queries: list[SearchQuery] = field(default_factory=list)
    listed: list[DocumentFilter | None] = field(default_factory=list)

    def search(self, query: SearchQuery) -> list[SearchHit]:
        self.queries.append(query)
        return self.hits[: query.limit]

    def get_pages(self, document_id: int, first: int, last: int) -> list[Page]:
        return [p for p in self.pages.get(document_id, []) if first <= p.number <= last]

    def list_documents(self, filters: DocumentFilter | None = None) -> list[DocumentSummary]:
        self.listed.append(filters)
        return self.documents

    def upsert_document(self, doc: Document, chunks: Sequence[EmbeddedChunk]) -> None:
        raise NotImplementedError

    def original_of(self, doc: Document) -> int | None:
        raise NotImplementedError

    def update_content_digest(self, document_id: int, digest: str | None) -> None:
        raise NotImplementedError

    def update_metadata(self, document_id: int, metadata: DocumentMetadata) -> None:
        raise NotImplementedError

    def delete_document(self, source_id: str) -> None:
        raise NotImplementedError

    def content_hash(self, source_id: str) -> str | None:
        raise NotImplementedError

    def get_cursor(self, source: str) -> str | None:
        raise NotImplementedError

    def save_cursor(self, source: str, cursor: str) -> None:
        raise NotImplementedError

    def mark_failed(self, change: FileChange, error: str) -> None:
        raise NotImplementedError

    def failed_documents(self) -> list[FileChange]:
        raise NotImplementedError


class FakeEmbedder:
    model_name = "fake-embedder"
    dimension = 2

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return [float(len(text)), 1.0]


@dataclass
class FakeReranker:
    """Scores a text by how often the query occurs in it; records the texts it was asked to score."""

    model_name: str = "fake-reranker"
    calls: list[list[str]] = field(default_factory=list)

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        self.calls.append(list(texts))
        return [float(t.count(query)) for t in texts]


@dataclass
class AnsweringStore(FakeStore):
    """FakeStore whose search returns canned hits per query text, for the retrieval evaluation."""

    answers: dict[str, list[SearchHit]] = field(default_factory=dict)

    def search(self, query: SearchQuery) -> list[SearchHit]:
        self.queries.append(query)
        return self.answers.get(query.text, [])[: query.limit]
