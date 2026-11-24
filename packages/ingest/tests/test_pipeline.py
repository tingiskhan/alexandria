from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest

from alexandria_core.models import (
    Document,
    DocumentMetadata,
    DocumentSummary,
    FileChange,
    Page,
    SearchQuery,
    content_digest,
)
from alexandria_core.protocols import Extractor
from alexandria_ingest.boilerplate import BoilerplateRules
from alexandria_ingest.chunk import embedding_text
from alexandria_ingest.metadata import MetadataReader
from alexandria_ingest.pipeline import (
    cursor_key,
    duplicate_groups,
    prepare_file,
    process_file,
    read_stored_metadata,
    reindex_document,
    stored_content_digest,
    sync_changes,
    triage,
)
from alexandria_ingest.transcript import TranscriptExtractor

from .fakes import FakeBibliographyLookup, FakeEmbedder, FakeExtractor, FakeFileSource, InMemoryStore

SOURCE = "dropbox:/papers"
PDF_ONLY = frozenset({"pdf"})


def pdf(path: str, content_hash: str = "h1") -> FileChange:
    return FileChange(source_id=path.lower(), path=path, content_hash=content_hash)


def run(
    source: FakeFileSource,
    store: InMemoryStore,
    formats: frozenset[str] = PDF_ONLY,
    extractor: Extractor | None = None,
) -> list[FileChange]:
    process = partial(
        process_file,
        source=source,
        extractor=extractor or FakeExtractor(),
        embedder=FakeEmbedder(),
        store=store,
        max_tokens=500,
        formats=formats,
        metadata_reader=MetadataReader(),
    )
    return sync_changes(SOURCE, source.changes, store, process)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


def test_ingests_pages_and_chunks(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/a.pdf": b"# Intro\n\nHello\fWorld"}, batches=[[pdf("/a.pdf")]])

    assert run(source, store) == []

    stored = store.documents["/a.pdf"]
    assert stored.doc.metadata.title == "a"
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
    assert store.documents["/notes/plan.docx"].doc.metadata.title == "Plan"
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

    indexed = prepare_file(
        pdf("/a.pdf"),
        source=source,
        extractor=extractor,
        embedder=FakeEmbedder(),
        store=store,
        max_tokens=500,
        metadata_reader=MetadataReader(),
    )

    assert extractor.names == ["a.pdf"]
    assert indexed.document.metadata.title == "a"
    assert [c.chunk.text for c in indexed.chunks] == ["# Intro\n\nHello"]
    assert store.documents == {}


WIDGETS = b"# Widgets Revisited\n\nJane Doe\n\ndoi:10.1000/w\f# Results\n\nThey work."


def test_prepare_file_reads_metadata_from_the_first_page_and_embeds_the_title() -> None:
    registered = DocumentMetadata(title="Widgets, Revisited", authors=("Jane Doe",), year=2021, doi="10.1000/w")
    lookup = FakeBibliographyLookup(known={"10.1000/w": registered})
    reader = MetadataReader(doc_types={"/papers": "paper"}, lookup=lookup)
    source = FakeFileSource(files={"/papers/w.pdf": WIDGETS})

    indexed = prepare_file(
        pdf("/papers/w.pdf"),
        source=source,
        extractor=FakeExtractor(),
        embedder=FakeEmbedder(),
        store=InMemoryStore(),
        max_tokens=500,
        metadata_reader=reader,
    )

    assert indexed.document.metadata == replace(registered, doc_type="paper")
    first = indexed.chunks[0]
    expected = FakeEmbedder().embed_documents([embedding_text("Widgets, Revisited", first.chunk)])
    assert first.embedding == expected[0]


def test_read_stored_metadata_uses_the_stored_first_page(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/papers/w.pdf": WIDGETS, "/empty.pdf": b""})
    source.batches = [[pdf("/papers/w.pdf"), pdf("/empty.pdf")]]
    run(source, store)
    reader = MetadataReader(doc_types={"/papers": "paper"})

    found = {s.path: read_stored_metadata(s, store=store, metadata_reader=reader) for s in store.list_documents()}

    assert found["/papers/w.pdf"] == DocumentMetadata(
        title="Widgets Revisited", authors=("Jane Doe",), doi="10.1000/w", doc_type="paper"
    )
    assert found["/empty.pdf"] == DocumentMetadata(title="empty")
    assert source.downloads == ["/papers/w.pdf", "/empty.pdf"]  # only by the sync


def test_read_stored_metadata_of_a_document_without_pages() -> None:
    store = InMemoryStore()
    store.upsert_document(
        Document(source_id="x", path="/x.pdf", metadata=DocumentMetadata(title="x"), content_hash="h", pages=()), []
    )
    [summary] = store.list_documents()

    assert read_stored_metadata(summary, store=store, metadata_reader=MetadataReader()) == DocumentMetadata(title="x")


def test_a_webvtt_transcript_is_stored_one_page_per_time_block(store: InMemoryStore) -> None:
    content = (Path(__file__).parent / "data" / "talk.vtt").read_bytes()
    source = FakeFileSource(files={"/podcasts/talk.vtt": content}, batches=[[pdf("/podcasts/talk.vtt")]])

    assert run(source, store, formats=frozenset({"vtt"}), extractor=TranscriptExtractor(block_minutes=5)) == []

    [summary] = store.list_documents()
    assert summary.page_count == 3
    assert summary.metadata == DocumentMetadata(title="Widgets in Winter", doc_type="transcript")
    [page] = store.get_pages(summary.id, 3, 3)
    assert page.markdown == "## [00:10:00–00:15:00]\n\nAlice: Welcome back after the break."
    sections = {c.chunk.page_start: c.chunk.section for c in store.documents["/podcasts/talk.vtt"].chunks}
    assert sections[3] == "Widgets in Winter > [00:10:00–00:15:00]"  # so search hits show the time range
    assert read_stored_metadata(summary, store=store, metadata_reader=MetadataReader()) == summary.metadata


class CountingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        self.embedded: list[str] = []

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return super().embed_documents(texts)


def test_the_same_pdf_under_two_paths_yields_one_searchable_document(store: InMemoryStore) -> None:
    paper = b"# Widgets\n\nThey work.\fMore pages"
    source = FakeFileSource(
        files={"/papers/w.pdf": paper, "/inbox/w (1).pdf": paper},
        batches=[[pdf("/papers/w.pdf", "h1"), pdf("/inbox/w (1).pdf", "h2")]],
    )
    embedder = CountingEmbedder()
    outcomes: list[object] = []

    def process(change: FileChange) -> object:
        outcome = process_file(
            change,
            source=source,
            extractor=FakeExtractor(),
            embedder=embedder,
            store=store,
            max_tokens=500,
            formats=PDF_ONLY,
            metadata_reader=MetadataReader(),
        )
        outcomes.append(outcome)
        return outcome

    sync_changes(SOURCE, source.changes, store, process)

    assert outcomes == ["ingested", "duplicate"]
    assert len(embedder.embedded) == 1  # the second one isn't embedded
    hits = store.search(SearchQuery(text="they work", embedding=[], model_name="fake-embedder"))
    assert {h.path for h in hits} == {"/papers/w.pdf"}
    listed = {d.path: d for d in store.list_documents()}
    assert listed["/inbox/w (1).pdf"].duplicate_of == listed["/papers/w.pdf"].id


def test_a_duplicate_becomes_searchable_after_its_original_is_deleted(store: InMemoryStore) -> None:
    deleted = FileChange(source_id="/a.pdf", path="/a.pdf", content_hash=None, deleted=True)
    source = FakeFileSource(
        files={"/a.pdf": b"same", "/b.pdf": b"same"},
        batches=[[pdf("/a.pdf"), pdf("/b.pdf")], [deleted]],
    )
    run(source, store)

    run(source, store)  # deletes /a.pdf; /b.pdf is released, and retried by the next run
    run(source, store)

    [promoted] = store.list_documents()
    assert (promoted.path, promoted.duplicate_of) == ("/b.pdf", None)
    assert store.documents["/b.pdf"].chunks
    assert source.downloads == ["/a.pdf", "/b.pdf", "/b.pdf"]


def test_reindex_leaves_duplicates_alone(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/a.pdf": b"same", "/b.pdf": b"same"}, batches=[[pdf("/a.pdf"), pdf("/b.pdf")]])
    run(source, store)
    embedder = CountingEmbedder()

    for summary in store.list_documents():
        reindex_document(summary, embedder=embedder, store=store, max_tokens=500)

    assert len(embedder.embedded) == 1
    assert store.documents["/b.pdf"].chunks == []


def test_stored_content_digest_computes_and_stores_a_missing_digest(store: InMemoryStore) -> None:
    store.upsert_document(Document("a", "/a.pdf", DocumentMetadata(title="a"), "h", (Page(1, "Some  text"),)), [])
    [summary] = store.list_documents()
    store.update_content_digest(summary.id, None)  # as for a document from an older release
    [legacy] = store.list_documents()

    digest = stored_content_digest(legacy, store=store)

    assert digest == content_digest([Page(1, "Some text")])
    assert store.list_documents()[0].content_digest == digest
    assert stored_content_digest(store.list_documents()[0], store=store) == digest


def test_stored_content_digest_of_a_document_without_text(store: InMemoryStore) -> None:
    store.upsert_document(Document("a", "/a.pdf", DocumentMetadata(title="a"), "h", ()), [])
    [summary] = store.list_documents()

    assert stored_content_digest(summary, store=store) is None
    assert store.list_documents()[0].content_digest is None


def summary_of(document_id: int, duplicate_of: int | None = None) -> DocumentSummary:
    return DocumentSummary(
        id=document_id,
        source_id=str(document_id),
        content_hash="h",
        metadata=DocumentMetadata(title=str(document_id)),
        path=f"/{document_id}.pdf",
        page_count=1,
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
        duplicate_of=duplicate_of,
    )


def test_duplicate_groups_put_the_searched_document_first() -> None:
    summaries = [summary_of(1, duplicate_of=3), summary_of(2), summary_of(3), summary_of(4), summary_of(5)]

    groups = duplicate_groups(summaries, ["x", "y", "x", "x", None])

    assert [[s.id for s in group] for group in groups] == [[3, 1, 4]]


HEADER = "Annual Report 2024 – Example Corp"
NOTICE = "© 2024 Example Corp. All rights reserved. May not be reproduced without permission."
PARTS = ("Outlook", "Revenue", "Costs", "Risks")


def report() -> bytes:
    """A front-matter notice page followed by pages that all carry the same running header."""
    body = [f"{HEADER}\n\n# {part}\n\nWhat we say about {part.lower()}." for part in PARTS]
    return "\f".join([NOTICE, *body]).encode()


def test_boilerplate_is_kept_out_of_chunks_but_not_out_of_pages(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/r.pdf": report()}, batches=[[pdf("/r.pdf")]])
    process = partial(
        process_file,
        source=source,
        extractor=FakeExtractor(),
        embedder=FakeEmbedder(),
        store=store,
        max_tokens=500,
        formats=PDF_ONLY,
        metadata_reader=MetadataReader(),
        boilerplate=BoilerplateRules(),
    )

    assert sync_changes(SOURCE, source.changes, store, process) == []

    stored = store.documents["/r.pdf"]
    assert [p.number for p in stored.doc.pages] == [1, 2, 3, 4, 5]
    assert stored.doc.pages[0].markdown == NOTICE
    assert all(HEADER in p.markdown for p in stored.doc.pages[1:])
    chunks = [c.chunk for c in stored.chunks]
    assert [(c.page_start, c.section) for c in chunks] == [(2, "Outlook"), (3, "Revenue"), (4, "Costs"), (5, "Risks")]
    assert not any(HEADER in c.text or "rights reserved" in c.text for c in chunks)


def test_reindex_applies_the_current_boilerplate_rules(store: InMemoryStore) -> None:
    source = FakeFileSource(files={"/r.pdf": report()}, batches=[[pdf("/r.pdf")]])
    run(source, store)
    assert any(HEADER in c.chunk.text for c in store.documents["/r.pdf"].chunks)

    [summary] = store.list_documents()
    reindex_document(summary, embedder=FakeEmbedder(), store=store, max_tokens=500, boilerplate=BoilerplateRules())

    assert not any(HEADER in c.chunk.text for c in store.documents["/r.pdf"].chunks)
    assert source.downloads == ["/r.pdf"]


def test_transcripts_are_not_stripped_of_boilerplate(store: InMemoryStore) -> None:
    # Bob says the same thing in every block, and the first block reads like a copyright notice: in a PDF these
    # would be a running header and a front-matter page, in a transcript they are speech.
    cues = ["Alice: This talk is copyright the speakers, all rights reserved, see https://example.org/terms."]
    blocks = [cues[0], *(f"Alice: Part {n} is about {topic}." for n, topic in enumerate(PARTS, 2))]
    vtt = "WEBVTT\n\n" + "\n".join(
        f"{n * 5:02d}:00.000 --> {n * 5:02d}:04.000\n<v Alice>{text.removeprefix('Alice: ')}</v>\n\n"
        f"{n * 5:02d}:04.000 --> {n * 5:02d}:06.000\n<v Bob>Right.</v>\n"
        for n, text in enumerate(blocks)
    )
    source = FakeFileSource(files={"/podcasts/t.vtt": vtt.encode()}, batches=[[pdf("/podcasts/t.vtt")]])
    process = partial(
        process_file,
        source=source,
        extractor=TranscriptExtractor(block_minutes=5),
        embedder=FakeEmbedder(),
        store=store,
        max_tokens=500,
        formats=frozenset({"vtt"}),
        metadata_reader=MetadataReader(),
        boilerplate=BoilerplateRules(),
    )

    assert sync_changes(SOURCE, source.changes, store, process) == []

    stored = store.documents["/podcasts/t.vtt"]
    assert stored.doc.metadata.doc_type == "transcript"
    assert len(stored.doc.pages) == 5
    texts = [c.chunk.text for c in stored.chunks]
    assert sum(t.count("Bob: Right.") for t in texts) == 5
    assert any("all rights reserved" in t for t in texts)
    [summary] = store.list_documents()
    reindex_document(summary, embedder=FakeEmbedder(), store=store, max_tokens=500, boilerplate=BoilerplateRules())
    assert [c.chunk.text for c in store.documents["/podcasts/t.vtt"].chunks] == texts
