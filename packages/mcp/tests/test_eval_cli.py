from pathlib import Path

import pytest

from alexandria_core.models import DocumentMetadata, SearchHit
from alexandria_mcp import eval_cli as module
from alexandria_mcp.eval_cli import run
from alexandria_mcp.retrieval import Retrieval

from .fakes import AnsweringStore, FakeEmbedder

QUESTIONS = """
questions:
  - question: What was revenue in 2025?
    path: /reports/annual-2025.pdf
    first_page: 41
    last_page: 42
"""

HIT = SearchHit(
    document_id=1,
    metadata=DocumentMetadata(title="annual-2025"),
    path="/reports/annual-2025.pdf",
    page_start=41,
    page_end=41,
    section="",
    text="Revenue rose 12%.",
    score=0.03,
)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> AnsweringStore:
    store = AnsweringStore(answers={"What was revenue in 2025?": [HIT]})
    monkeypatch.setattr(module, "build_retrieval", lambda: Retrieval(store, FakeEmbedder()))
    return store


def invoke(monkeypatch: pytest.MonkeyPatch, *args: str) -> str | int | None:
    monkeypatch.setattr("sys.argv", ["alexandria-eval", *args])
    with pytest.raises(SystemExit) as exit_info:
        run()
    return exit_info.value.code


def test_prints_the_metrics_for_the_default_questions_file(
    store: AnsweringStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "questions.yaml").write_text(QUESTIONS, encoding="utf-8")  # tmp_path is the working dir

    assert invoke(monkeypatch) == 0

    out = capsys.readouterr().out
    assert "1 questions, top 10 hits each" in out
    assert "page            1.000      1.000   1.000" in out
    assert [q.text for q in store.queries] == ["What was revenue in 2025?"]


def test_details_lists_each_question(
    store: AnsweringStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "mine.yaml"
    path.write_text(QUESTIONS, encoding="utf-8")

    assert invoke(monkeypatch, str(path), "--details") == 0

    assert "document=1  page=1  What was revenue in 2025?" in capsys.readouterr().out


def test_refuses_an_invalid_questions_file_before_loading_the_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def build() -> None:
        raise AssertionError("must not build the store, embedder and reranker")

    monkeypatch.setattr(module, "build_retrieval", build)
    path = tmp_path / "bad.yaml"
    path.write_text("questions: []", encoding="utf-8")

    with pytest.raises(SystemExit, match=r"bad\.yaml"):
        module.main(path)


def test_refuses_a_missing_questions_file(monkeypatch: pytest.MonkeyPatch) -> None:
    assert invoke(monkeypatch, "nowhere.yaml") == 2  # typer's usage error
