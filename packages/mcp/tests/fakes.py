from collections.abc import Sequence
from dataclasses import dataclass, field

from alexandria_core.models import (
    Document,
    DocumentSummary,
    EmbeddedChunk,
    FileChange,
    Page,
    SearchHit,
    SearchQuery,
)


@dataclass
class FakeStore:
    """Read side of DocumentStore with canned data; records the queries it receives. Writes are not used here."""

    hits: list[SearchHit] = field(default_factory=list)
    documents: list[DocumentSummary] = field(default_factory=list)
    pages: dict[int, list[Page]] = field(default_factory=dict)
    queries: list[SearchQuery] = field(default_factory=list)

    def search(self, query: SearchQuery) -> list[SearchHit]:
        self.queries.append(query)
        return self.hits[: query.limit]

    def get_pages(self, document_id: int, first: int, last: int) -> list[Page]:
        return [p for p in self.pages.get(document_id, []) if first <= p.number <= last]

    def list_documents(self) -> list[DocumentSummary]:
        return self.documents

    def upsert_document(self, doc: Document, chunks: Sequence[EmbeddedChunk]) -> None:
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
