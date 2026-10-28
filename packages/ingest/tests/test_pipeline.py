from functools import partial

import pytest

from alexandria_core.models import FileChange
from alexandria_ingest.pipeline import (
    cursor_key,
    prepare_file,
    process_file,
    reindex_document,
    sync_changes,
    triage,
)

from .fakes import FakeEmbedder, FakeExtractor, FakeFileSource, InMemoryStore

SOURCE = "dropbox:/papers"
PDF_ONLY = frozenset({"pdf"})


def pdf(path: str, content_hash: str = "h1") -> FileChange:
    return FileChange(source_id=path.lower(), path=path, content_hash=content_hash)


def run(
    source: FakeFileSource,
    store: InMemoryStore,
    formats: frozenset[str] = PDF_ONLY,
    extractor: FakeExtractor | None = None,
) -> list[FileChange]:
    process = partial(
        process_file,
        source=source,
        extractor=extractor or FakeExtractor(),
        embedder=FakeEmbedder(),
        store=store,
        max_tokens=500,
        formats=formats,
    )
    return sync_changes(SOURCE, source.changes, store, process)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


def test_ingests_pages_and_chunks(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/a.pdf": b"# Intro\n\nHello\fWorld"}, batches=[[pdf("/a.pdf")]])

    assert run(source, store) == []

    stored = store.documents["/a.pdf"]
    assert stored.doc.title == "a"
    assert [p.number for p in stored.doc.pages] == [1, 2]
    assert [(c.chunk.section, c.chunk.text) for c in stored.chunks] == [("Intro", "# Intro\n\nHello\n\nWorld")]
    assert all(c.model_name == "fake-embedder" for c in stored.chunks)


def test_same_hash_is_skipped(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/a.pdf": b"text"}, batches=[[pdf("/a.pdf")], [pdf("/a.pdf")]])

    run(source, store)
    run(source, store)

    assert source.downloads == ["/a.pdf"]


def test_changed_hash_is_reingested(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/a.pdf": b"text"}, batches=[[pdf("/a.pdf")], [pdf("/a.pdf", "h2")]])

    run(source, store)
    run(source, store)

    assert source.downloads == ["/a.pdf", "/a.pdf"]
    assert store.content_hash("/a.pdf") == "h2"


def test_formats_not_listed_are_skipped(store: InMemoryStore) -> None:
    source = FakeFileSource(batches=[[pdf("/notes.txt")]])

    run(source, store)

    assert source.downloads == []
    assert store.documents == {}


def test_listed_formats_are_ingested_by_file_name(store: InMemoryStore) -> None:
    source = FakeFileSource(
        files={"/notes/plan.docx": b"# Plan\n\nShip it", "/a.pdf": b"text"},
        batches=[[pdf("/Notes/Plan.DOCX"), pdf("/a.pdf"), pdf("/Notes/todo.md")]],
    )
    extractor = FakeExtractor()

    assert run(source, store, formats=frozenset({"docx", "pdf"}), extractor=extractor) == []

    assert extractor.names == ["Plan.DOCX", "a.pdf"]
    assert store.documents["/notes/plan.docx"].doc.title == "Plan"
    assert "/notes/todo.md" not in store.documents


def test_deletion_removes_document(store: InMemoryStore) -> None:
    deleted = FileChange(source_id="/a.pdf", path="/a.pdf", content_hash=None, deleted=True)
    source = FakeFileSource(files={"/a.pdf": b"text"}, batches=[[pdf("/a.pdf")], [deleted]])

    run(source, store)
    run(source, store)

    assert store.documents == {}


def test_failure_is_recorded_and_run_continues(store: InMemoryStore) -> None:
    source = FakeFileSource(
        files={"/bad.pdf": b"broken", "/good.pdf": b"text"}, batches=[[pdf("/bad.pdf"), pdf("/good.pdf")]]
    )

    failed = run(source, store)

    assert failed == [pdf("/bad.pdf")]
    assert store.documents["/bad.pdf"].status == "failed"
    assert store.documents["/bad.pdf"].error == "ValueError: cannot parse PDF"
    assert store.content_hash("/good.pdf") == "h1"
    assert store.get_cursor(SOURCE) == "cursor-1"


def test_failed_deletion_ends_run_without_saving_cursor(store: InMemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    deleted = FileChange(source_id="/a.pdf", path="/a.pdf", content_hash=None, deleted=True)
    source = FakeFileSource(files={"/a.pdf": b"text"}, batches=[[pdf("/a.pdf")], [deleted]])
    run(source, store)

    def unavailable(source_id: str) -> None:
        raise ConnectionError("the database system is shutting down")

    monkeypatch.setattr(store, "delete_document", unavailable)

    with pytest.raises(ConnectionError):
        run(source, store)

    assert store.get_cursor(SOURCE) == "cursor-1"
    assert store.failed_documents() == []


def test_failed_document_is_retried_next_run(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/a.pdf": b"broken"}, batches=[[pdf("/a.pdf")]])
    run(source, store)

    source.files["/a.pdf"] = b"fixed"
    assert run(source, store) == []

    assert store.content_hash("/a.pdf") == "h1"
    assert source.downloads == ["/a.pdf", "/a.pdf"]


def test_cursor_is_saved_and_passed_to_next_run(store: InMemoryStore) -> None:
    source = FakeFileSource()

    run(source, store)
    run(source, store)

    assert source.cursors_seen == [None, "cursor-1"]
    assert store.get_cursor(SOURCE) == "cursor-2"


def test_second_run_without_changes_does_nothing(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/a.pdf": b"text"}, batches=[[pdf("/a.pdf")]])
    run(source, store)
    before = dict(store.documents)

    run(source, store)

    assert store.documents == before
    assert source.downloads == ["/a.pdf"]


def test_reindex_rebuilds_chunks_from_stored_pages(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/a.pdf": b"# Intro\n\nHello\fWorld"}, batches=[[pdf("/a.pdf")]])
    run(source, store)
    before = store.documents["/a.pdf"]

    class NewEmbedder(FakeEmbedder):
        model_name = "new-model"

    [summary] = store.list_documents()
    reindex_document(summary, embedder=NewEmbedder(), store=store, max_tokens=500)

    after = store.documents["/a.pdf"]
    assert after.id == before.id
    assert after.doc == before.doc
    assert [c.chunk for c in after.chunks] == [c.chunk for c in before.chunks]
    assert {c.model_name for c in after.chunks} == {"new-model"}
    assert source.downloads == ["/a.pdf"]


def test_triage_leaves_new_files_of_listed_formats_to_prepare_and_store(store: InMemoryStore) -> None:
    formats = frozenset({"pdf", "md"})
    assert triage(pdf("/a.pdf"), store=store, formats=formats) is None
    assert triage(pdf("/README.MD"), store=store, formats=formats) is None
    assert triage(pdf("/notes.txt"), store=store, formats=formats) == "skipped"
    assert triage(pdf("/Makefile"), store=store, formats=formats) == "skipped"


def test_cursor_key_changes_with_the_formats() -> None:
    assert cursor_key("s3:library/", {"pdf", "docx"}) == "s3:library/|docx,pdf"
    assert cursor_key("s3:library/", {"pdf"}) != cursor_key("s3:library/", {"pdf", "docx"})


def test_prepare_file_embeds_without_storing(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/a.pdf": b"# Intro\n\nHello"})

    extractor = FakeExtractor()

    indexed = prepare_file(pdf("/a.pdf"), source=source, extractor=extractor, embedder=FakeEmbedder(), max_tokens=500)

    assert extractor.names == ["a.pdf"]
    assert indexed.document.title == "a"
    assert [c.chunk.text for c in indexed.chunks] == ["# Intro\n\nHello"]
    assert store.documents == {}
