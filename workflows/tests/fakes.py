"""Fakes for the workflow smoke tests; same as packages/ingest/tests/fakes.py, kept separate per package."""

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

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
class FakeFileSource:
    """Returns queued batches of changes; each call to changes() pops the next batch."""

    files: dict[str, bytes] = field(default_factory=dict)
    batches: list[list[FileChange]] = field(default_factory=list)
    cursors_seen: list[str | None] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)

    def changes(self, cursor: str | None) -> tuple[list[FileChange], str]:
        self.cursors_seen.append(cursor)
        batch = self.batches.pop(0) if self.batches else []
        return batch, f"cursor-{len(self.cursors_seen)}"

    def download(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        return self.files[file_id]


class FakeExtractor:
    """Treats the bytes as UTF-8 with pages separated by form feeds; b"broken" raises. Records the file names."""

    def __init__(self, formats: Iterable[str] = ()) -> None:
        self.formats = frozenset(formats)
        self.names: list[str] = []

    def extract(self, content: bytes, name: str) -> list[Page]:
        self.names.append(name)
        if content == b"broken":
            raise ValueError("cannot parse PDF")
        return [Page(number=n, markdown=text) for n, text in enumerate(content.decode().split("\f"), start=1)]


class FakeEmbedder:
    model_name = "fake-embedder"
    dimension = 8

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()[: self.dimension]
        values = [b + 1.0 for b in digest]
        norm = math.sqrt(sum(v * v for v in values))
        return [v / norm for v in values]


@dataclass
class _StoredDocument:
    id: int
    doc: Document
    chunks: list[EmbeddedChunk]
    status: str = "ok"
    error: str | None = None


@dataclass
class InMemoryStore:
    documents: dict[str, _StoredDocument] = field(default_factory=dict)
    cursors: dict[str, str] = field(default_factory=dict)
    next_id: int = 1

    def upsert_document(self, doc: Document, chunks: Sequence[EmbeddedChunk]) -> None:
        existing = self.documents.get(doc.source_id)
        document_id = existing.id if existing else self._new_id()
        self.documents[doc.source_id] = _StoredDocument(id=document_id, doc=doc, chunks=list(chunks))

    def delete_document(self, source_id: str) -> None:
        prefix = source_id.rstrip("/") + "/"
        for key in [k for k in self.documents if k == source_id or k.startswith(prefix)]:
            del self.documents[key]

    def content_hash(self, source_id: str) -> str | None:
        stored = self.documents.get(source_id)
        return stored.doc.content_hash if stored and stored.status == "ok" else None

    def search(self, query: SearchQuery) -> list[SearchHit]:
        hits = [
            SearchHit(
                document_id=s.id,
                title=s.doc.title,
                path=s.doc.path,
                page_start=c.chunk.page_start,
                page_end=c.chunk.page_end,
                section=c.chunk.section,
                text=c.chunk.text,
                score=1.0,
            )
            for s in self._ok()
            if query.document_ids is None or s.id in query.document_ids
            for c in s.chunks
            if query.text.lower() in c.chunk.text.lower()
        ]
        return hits[: query.limit]

    def get_pages(self, document_id: int, first: int, last: int) -> list[Page]:
        return [p for s in self._ok() if s.id == document_id for p in s.doc.pages if first <= p.number <= last]

    def list_documents(self) -> list[DocumentSummary]:
        return [
            DocumentSummary(
                id=s.id,
                source_id=s.doc.source_id,
                content_hash=s.doc.content_hash,
                title=s.doc.title,
                path=s.doc.path,
                page_count=len(s.doc.pages),
                ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            for s in sorted(self._ok(), key=lambda s: s.doc.path)
        ]

    def get_cursor(self, source: str) -> str | None:
        return self.cursors.get(source)

    def save_cursor(self, source: str, cursor: str) -> None:
        self.cursors[source] = cursor

    def mark_failed(self, change: FileChange, error: str) -> None:
        existing = self.documents.get(change.source_id)
        doc = Document(
            source_id=change.source_id, path=change.path, title="", content_hash=change.content_hash or "", pages=()
        )
        self.documents[change.source_id] = _StoredDocument(
            id=existing.id if existing else self._new_id(), doc=doc, chunks=[], status="failed", error=error
        )

    def failed_documents(self) -> list[FileChange]:
        return [
            FileChange(source_id=s.doc.source_id, path=s.doc.path, content_hash=s.doc.content_hash or None)
            for s in self.documents.values()
            if s.status == "failed"
        ]

    def _ok(self) -> list[_StoredDocument]:
        return [s for s in self.documents.values() if s.status == "ok"]

    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id - 1
