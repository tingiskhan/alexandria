"""The maths convention of stored Markdown, on its own and through Docling's real Markdown serializer."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from docling.datamodel.base_models import InputFormat
from docling_core.types.doc import DocItemLabel, DoclingDocument, TableCell, TableData
from typer.testing import CliRunner

from alexandria_core.models import DocumentSummary, FileChange, Page
from alexandria_ingest import cli, extract
from alexandria_ingest.extract import DoclingExtractor
from alexandria_ingest.katex import katex_error
from alexandria_ingest.latex import Formula, formulas, normalize_math
from alexandria_ingest.pipeline import process_file, warn_about_invalid_formulas

from .fakes import FakeEmbedder, FakeFileSource, InMemoryStore


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (r"Each \( x\_{n} \) and \( x = x\_{1:N} \).", r"Each \( x_{n} \) and \( x = x_{1:N} \)."),
        (r"\( \nabla\_{\theta}\log p \)", r"\( \nabla_{\theta}\log p \)"),
        (r"\( a \\_b \)", r"\( a \\_b \)"),  # a line break before a subscript, not an escape
        (r"$$\[\rho_ {k} = \eta \tag {10}\]$$", r"$$\rho_ {k} = \eta \tag {10}$$"),
        ("$$\\[\n a \\\\ b \n\\]$$", "$$\n a \\\\ b \n$$"),
        (r"x$\_{<t}$, z$\_{t}$ and more", r"x\(_{<t}\), z\(_{t}\) and more"),
        ("| KL (q &#124;&#124; p) | 1 |", r"| KL (q \|\| p) | 1 |"),
    ],
)
def test_normalizes_maths(stored: str, expected: str) -> None:
    assert normalize_math(stored) == expected


@pytest.mark.parametrize(
    "text",
    [
        r"written to output\_advi.csv, with \( x_{n} \)",  # prose escapes stay
        "It costs $5 and $10 or $5-$10, or US$ 3 and A$ 4.",
        r"Escaped \$x\_1\$ stays.",
        "```\nre.sub(r'\\(a\\_b\\)', '', s)\n```",
        r"`\(a\_b\)` in a code span",
        "An unclosed \\( x\\_1\n\nin one paragraph does not reach \\) the next.",
        "| a &#124; b",  # not a table row
    ],
)
def test_leaves_everything_else_alone(text: str) -> None:
    assert normalize_math(text) == text


def test_is_idempotent() -> None:
    once = normalize_math("$$\\[ a \\]$$ x$\\_{1}$ \\( y\\_{2} \\)\n\n| a &#124; b |")

    assert normalize_math(once) == once


def test_finds_the_formulas_of_a_page() -> None:
    markdown = "Each \\( x_{n} \\) costs $5.\n\n$$a \\tag {1}$$\n\n`\\(code\\)`"

    assert list(formulas(markdown)) == [Formula(" x_{n} ", display=False), Formula(r"a \tag {1}", display=True)]


@pytest.mark.parametrize(
    ("latex", "display", "valid"),
    [
        (r"x_{1:N}", False, True),
        (r"\rho_ {k} = \eta \tag {10}", True, True),  # \tag survives the check, in display maths only
        (r"\tag {10}", False, False),
        (r"\[ a \]", True, False),
        (r"\frac{a", False, False),
    ],
)
def test_katex_decides_what_is_valid(latex: str, display: bool, valid: bool) -> None:
    assert (katex_error(latex, display=display) is None) == valid


def test_katex_errors_are_logged_without_failing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        warn_about_invalid_formulas("a.pdf", [Page(3, r"Fine \( x_{1} \) and bad \( \frac{a \)")])

    [record] = caplog.records
    assert "a.pdf p. 3: invalid formula" in record.getMessage()


def mineru_page() -> DoclingDocument:
    """A page the way parse_mineru2 builds it: maths in plain text items, page numbers labelled as footers."""
    document = DoclingDocument(name="advi")
    document.add_text(label=DocItemLabel.TEXT, text=r"Each \( x_{1:N} \) in output_advi.csv with \( x_{<t} \) after")
    document.add_formula(text=r"\[\rho_ {k} = \eta \tag {10}\]")
    cells = [
        TableCell(text=text, start_row_offset_idx=row, end_row_offset_idx=row + 1, start_col_offset_idx=0,
                  end_col_offset_idx=1)
        for row, text in enumerate(["Divergence", "KL (q || p)"])
    ]  # fmt: skip
    document.add_table(data=TableData(num_rows=2, num_cols=1, table_cells=cells))
    document.add_text(label=DocItemLabel.CAPTION, text="Table 1: Divergences.")
    document.add_text(label=DocItemLabel.FOOTNOTE, text=r"\( ^{1} \) A footnote.")
    document.add_text(label=DocItemLabel.PAGE_FOOTER, text="4")
    return document


@dataclass
class FakeResult:
    document: DoclingDocument


class CannedConverter:
    def __init__(self, allowed_formats: list[InputFormat], format_options: dict) -> None:
        pass

    def convert(self, _stream: object) -> FakeResult:
        return FakeResult(mineru_page())


@pytest.fixture
def ingested(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(extract, "DocumentConverter", CannedConverter)
    store = InMemoryStore()
    process_file(
        FileChange(source_id="id-1", path="/papers/advi.pdf", content_hash="h1"),
        source=FakeFileSource(files={"id-1": b"%PDF-1.7"}),
        extractor=DoclingExtractor({"pdf"}),
        embedder=FakeEmbedder(),
        store=store,
        max_tokens=500,
        formats={"pdf"},
    )
    [page] = store.documents["id-1"].doc.pages
    return page.markdown


def test_inline_maths_is_stored_unescaped(ingested: str) -> None:
    assert r"\( x_{1:N} \)" in ingested


def test_prose_stays_escaped(ingested: str) -> None:
    assert r"output\_advi.csv" in ingested


def test_display_maths_has_one_pair_of_delimiters_and_keeps_its_tag(ingested: str) -> None:
    assert r"$$\rho_ {k} = \eta \tag {10}$$" in ingested


def test_a_literal_less_than_in_maths_is_stored_intact(ingested: str) -> None:
    assert r"\( x_{<t} \) after" in ingested


def test_table_pipes_stay_readable(ingested: str) -> None:
    assert r"KL (q \|\| p)" in ingested


def test_page_numbers_are_dropped(ingested: str) -> None:
    assert not ingested.rstrip().endswith("4")


def test_captions_and_footnotes_are_kept(ingested: str) -> None:
    assert "Table 1: Divergences." in ingested
    assert r"\( ^{1} \) A footnote." in ingested


def test_stored_formulas_render(ingested: str) -> None:
    assert [f for f in formulas(ingested) if katex_error(f.latex, display=f.display)] == []


class StoredPages:
    def __init__(self, *_args: object) -> None:
        pass

    def list_documents(self) -> list[DocumentSummary]:
        return [_summary()]

    def get_pages(self, _document_id: int, _first: int, _last: int) -> list[Page]:
        return [Page(1, r"\( x_{1} \)"), Page(2, r"\( \frac{a \)")]


def _summary() -> DocumentSummary:
    return DocumentSummary(
        id=7, source_id="s", content_hash="h", title="t", path="/a.pdf", page_count=2,
        ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
    )  # fmt: skip


def test_check_formulas_reports_per_document_and_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setattr(cli, "PgStore", StoredPages)

    result = CliRunner().invoke(cli.app, ["check-formulas"])

    assert result.exit_code == 1
    assert result.stdout.startswith("7\t2\t/a.pdf\tKaTeX parse error")
    assert "1 invalid formulas on 2 pages" in result.stderr
