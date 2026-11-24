from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from alexandria_core.models import DocumentFilter, DocumentMetadata, DocumentSummary, SearchHit
from alexandria_mcp.evaluation import (
    Level,
    Question,
    evaluate,
    first_relevant_rank,
    format_report,
    is_relevant,
    load_questions,
    score,
)
from alexandria_mcp.retrieval import Retrieval

from .fakes import AnsweringStore, FakeEmbedder, FakeReranker

REPORT = "/reports/annual-2025.pdf"
MANUAL = "/manuals/handbook.pdf"


def hit(path: str, page_start: int, page_end: int | None = None, text: str = "") -> SearchHit:
    return SearchHit(
        document_id=1,
        metadata=DocumentMetadata(title=path),
        path=path,
        page_start=page_start,
        page_end=page_start if page_end is None else page_end,
        section="",
        text=text,
        score=0.01,
    )


def question(text: str = "What was revenue?", path: str = REPORT, first: int = 41, last: int = 42) -> Question:
    return Question(question=text, path=path, first_page=first, last_page=last)


def summary(path: str) -> DocumentSummary:
    return DocumentSummary(
        id=1,
        source_id=path,
        content_hash="h",
        metadata=DocumentMetadata(title=path),
        path=path,
        page_count=100,
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("candidate", "document", "page"),
    [
        (hit(MANUAL, 41), False, False),  # wrong document
        (hit(REPORT, 7), True, False),  # right document, wrong pages
        (hit(REPORT, 41), True, True),
        (hit(REPORT, 42), True, True),
        (hit(REPORT, 40, 41), True, True),  # a chunk that runs into the range
        (hit(REPORT, 42, 44), True, True),  # a chunk that starts at the end of the range
        (hit(REPORT, 30, 50), True, True),  # a chunk that spans the whole range
        (hit(REPORT, 39, 40), True, False),  # ends just before
        (hit(REPORT, 43, 45), True, False),  # starts just after
    ],
)
def test_relevance_by_level(candidate: SearchHit, document: bool, page: bool) -> None:
    assert is_relevant(candidate, question(), Level.DOCUMENT) is document
    assert is_relevant(candidate, question(), Level.PAGE) is page


def test_first_relevant_rank_counts_from_one() -> None:
    hits = [hit(MANUAL, 1), hit(REPORT, 3), hit(REPORT, 41)]

    assert first_relevant_rank(hits, question(), Level.DOCUMENT) == 2
    assert first_relevant_rank(hits, question(), Level.PAGE) == 3
    assert first_relevant_rank(hits[:2], question(), Level.PAGE) is None
    assert first_relevant_rank([], question(), Level.DOCUMENT) is None


def test_score_recall_and_mrr() -> None:
    metrics = score([1, 2, 6, None], cutoffs=(1, 5, 10))

    assert metrics.recall == {1: 0.25, 5: 0.5, 10: 0.75}
    assert metrics.mrr == pytest.approx((1 + 1 / 2 + 1 / 6 + 0) / 4)


def test_score_of_no_questions_is_zero() -> None:
    metrics = score([])

    assert metrics.recall == {5: 0.0, 10: 0.0}
    assert metrics.mrr == 0.0


def test_evaluate_searches_like_the_mcp_tool_and_scores_both_levels() -> None:
    questions = [
        question("revenue", REPORT, 41, 42),
        question("vacation days", MANUAL, 3, 3),
        question("missing", "/gone.pdf", 1, 1),
    ]
    store = AnsweringStore(
        answers={
            "revenue": [hit(REPORT, 41)],  # page hit at rank 1
            # the right document at rank 2, the right page at rank 7
            "vacation days": [hit(REPORT, 1), hit(MANUAL, 9), *[hit(REPORT, 2)] * 4, hit(MANUAL, 3)],
        },
        documents=[summary(REPORT), summary(MANUAL)],
    )
    embedder = FakeEmbedder()

    report = evaluate(Retrieval(store, embedder), questions)

    assert [(q.text, q.embedding, q.model_name, q.limit) for q in store.queries] == [
        ("revenue", embedder.embed_query("revenue"), "fake-embedder", 10),
        ("vacation days", embedder.embed_query("vacation days"), "fake-embedder", 10),
        ("missing", embedder.embed_query("missing"), "fake-embedder", 10),
    ]
    assert [o.ranks for o in report.outcomes] == [
        {Level.DOCUMENT: 1, Level.PAGE: 1},
        {Level.DOCUMENT: 2, Level.PAGE: 7},
        {Level.DOCUMENT: None, Level.PAGE: None},
    ]
    assert report.metrics[Level.DOCUMENT].recall == {5: 2 / 3, 10: 2 / 3}
    assert report.metrics[Level.DOCUMENT].mrr == pytest.approx((1 + 1 / 2) / 3)
    assert report.metrics[Level.PAGE].recall == {5: 1 / 3, 10: 2 / 3}
    assert report.metrics[Level.PAGE].mrr == pytest.approx((1 + 1 / 7) / 3)
    assert report.depth == 10
    assert report.unknown_paths == ["/gone.pdf"]


def test_evaluate_retrieves_as_deep_as_the_largest_cutoff() -> None:
    store = AnsweringStore(answers={"q": [hit(REPORT, 41)] * 30})

    report = evaluate(Retrieval(store, FakeEmbedder()), [question("q")], cutoffs=(3, 20))

    assert store.queries[0].limit == 20
    assert report.depth == 20
    assert report.metrics[Level.PAGE].recall == {3: 1.0, 20: 1.0}


def test_evaluate_reranks_like_the_mcp_tool() -> None:
    candidates = [hit(MANUAL, 1, text="handbook"), hit(REPORT, 41, text="revenue revenue")]
    store = AnsweringStore(answers={"revenue": candidates}, documents=[summary(REPORT), summary(MANUAL)])
    reranker = FakeReranker()

    report = evaluate(Retrieval(store, FakeEmbedder(), reranker, rerank_candidates=30), [question("revenue")])

    [query] = store.queries
    assert (query.limit, query.filters) == (30, DocumentFilter())  # more candidates than the depth of 10
    assert reranker.calls == [["handbook", "revenue revenue"]]
    assert report.outcomes[0].ranks == {Level.DOCUMENT: 1, Level.PAGE: 1}  # rank 2 without the reranker


def test_format_report_prints_a_table() -> None:
    store = AnsweringStore(answers={"revenue": [hit(MANUAL, 1), hit(REPORT, 41)]}, documents=[summary(REPORT)])
    report = evaluate(Retrieval(store, FakeEmbedder()), [question("revenue"), question("profit")])

    assert format_report(report) == (
        "2 questions, top 10 hits each\n"
        "\n"
        "level        recall@5  recall@10     MRR\n"
        "document        0.500      0.500   0.250\n"
        "page            0.500      0.500   0.250"
    )


def test_format_report_details_and_warnings() -> None:
    store = AnsweringStore(answers={"revenue": [hit(REPORT, 1), hit(REPORT, 41)]})
    report = evaluate(Retrieval(store, FakeEmbedder()), [question("revenue"), question("profit")])

    lines = format_report(report, details=True).splitlines()

    assert lines[:4] == [
        f"warning: not in the library: {REPORT}",
        "document=1  page=2  revenue",
        "document=-  page=-  profit",
        "",
    ]
    assert lines[4] == "2 questions, top 10 hits each"


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "questions.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_questions(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
questions:
  - question: What was revenue in 2025?
    path: /reports/annual-2025.pdf
    first_page: 41
    last_page: 42
  - question: How many vacation days?
    path: /manuals/handbook.pdf
    first_page: 3
    last_page: 3
""",
    )

    assert load_questions(path) == [
        question("What was revenue in 2025?", REPORT, 41, 42),
        question("How many vacation days?", MANUAL, 3, 3),
    ]


@pytest.mark.parametrize(
    "content",
    [
        "questions: []",
        "questions:\n  - {question: q, path: /a.pdf, first_page: 5, last_page: 4}",  # range ends before it starts
        "questions:\n  - {question: q, path: /a.pdf, first_page: 0, last_page: 1}",  # pages count from 1
        "questions:\n  - {question: '', path: /a.pdf, first_page: 1, last_page: 1}",
        "questions:\n  - {question: q, path: /a.pdf, first_page: 1}",
        "questions:\n  - {question: q, path: /a.pdf, first_page: 1, last_page: 1, page: 3}",  # unknown key
        "- just a list",
    ],
)
def test_load_questions_rejects_bad_files(tmp_path: Path, content: str) -> None:
    with pytest.raises(ValidationError):
        load_questions(write(tmp_path, content))


def test_the_example_questions_file_is_valid() -> None:
    root = next(p for p in Path(__file__).resolve().parents if (p / "eval" / "questions.example.yaml").is_file())

    questions = load_questions(root / "eval" / "questions.example.yaml")

    assert questions
    assert all(q.path.startswith("/") and "example" in q.path for q in questions)
