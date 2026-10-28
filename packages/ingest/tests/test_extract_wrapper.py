"""DoclingExtractor's own logic, with Docling's converter replaced by a fake. test_extract.py covers real Docling."""

from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest
from docling.datamodel.base_models import InputFormat

from alexandria_core.models import Page
from alexandria_ingest import extract
from alexandria_ingest.extract import DoclingExtractor


@dataclass
class FakeDocument:
    pages: dict[int, str]
    whole: str = ""
    exports: list[tuple[int | None, bool]] = field(default_factory=list)

    def export_to_markdown(self, *, page_no: int | None = None, escape_html: bool) -> str:
        self.exports.append((page_no, escape_html))
        return self.whole if page_no is None else self.pages[page_no]


@dataclass
class FakeResult:
    document: FakeDocument


class FakeConverter:
    document = FakeDocument({2: "Second\x00 page", 1: "S&P\tfirst"})
    instances: ClassVar[list["FakeConverter"]] = []

    def __init__(self, allowed_formats: list[InputFormat], format_options: dict[InputFormat, Any]) -> None:
        self.allowed_formats = allowed_formats
        self.format_options = format_options
        self.converted: list[Any] = []
        FakeConverter.instances.append(self)

    def convert(self, stream: Any) -> FakeResult:
        self.converted.append(stream)
        return FakeResult(self.document)


@pytest.fixture(autouse=True)
def fake_converter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "DocumentConverter", FakeConverter)
    monkeypatch.setattr(FakeConverter, "document", FakeDocument({2: "Second\x00 page", 1: "S&P\tfirst"}))
    monkeypatch.setattr(FakeConverter, "instances", [])


def test_pages_come_out_in_order_with_control_characters_removed() -> None:
    pages = DoclingExtractor().extract(b"%PDF-1.7", "report.pdf")

    assert pages == [Page(1, "S&P\tfirst"), Page(2, "Second page")]
    assert FakeConverter.document.exports == [(1, False), (2, False)]  # HTML escaping off


def test_the_file_name_picks_the_format() -> None:
    DoclingExtractor().extract(b"<p>hi</p>", "Notes.HTML")

    [converter] = FakeConverter.instances
    assert [stream.name for stream in converter.converted] == ["Notes.HTML"]


def test_a_document_without_pages_is_one_page() -> None:
    FakeConverter.document = FakeDocument({}, whole="# Plan\n\nShip\x07 it")

    pages = DoclingExtractor().extract(b"PK...", "plan.docx")

    assert pages == [Page(1, "# Plan\n\nShip it")]
    assert FakeConverter.document.exports == [(None, False)]


def test_only_the_configured_formats_are_allowed() -> None:
    DoclingExtractor({"docx", "md", "txt", "png"})

    [converter] = FakeConverter.instances
    assert converter.allowed_formats == [InputFormat.DOCX, InputFormat.IMAGE, InputFormat.MD]
    assert set(converter.format_options) == {InputFormat.IMAGE}  # OCR options; PDF is not allowed


def test_pdfs_get_ocr_and_table_options() -> None:
    DoclingExtractor({"pdf"})

    [converter] = FakeConverter.instances
    options = converter.format_options[InputFormat.PDF].pipeline_options
    assert (options.do_ocr, options.do_table_structure) == (True, True)
