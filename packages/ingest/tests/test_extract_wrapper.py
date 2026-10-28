"""DoclingExtractor's own logic, with Docling's converter replaced by a fake. test_extract.py covers real Docling."""

from dataclasses import dataclass, field
from typing import Any

import pytest

from alexandria_core.models import Page
from alexandria_ingest import extract
from alexandria_ingest.extract import DoclingExtractor


@dataclass
class FakeDocument:
    pages: dict[int, str]
    exports: list[tuple[int, bool]] = field(default_factory=list)

    def export_to_markdown(self, *, page_no: int, escape_html: bool) -> str:
        self.exports.append((page_no, escape_html))
        return self.pages[page_no]


@dataclass
class FakeResult:
    document: FakeDocument


class FakeConverter:
    document = FakeDocument({2: "Second\x00 page", 1: "S&P\tfirst"})

    def __init__(self, format_options: dict[Any, Any]) -> None:
        self.format_options = format_options
        self.converted: list[Any] = []

    def convert(self, stream: Any) -> FakeResult:
        self.converted.append(stream)
        return FakeResult(self.document)


@pytest.fixture(autouse=True)
def fake_converter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "DocumentConverter", FakeConverter)


def test_pages_come_out_in_order_with_control_characters_removed() -> None:
    pages = DoclingExtractor().extract(b"%PDF-1.7")

    assert pages == [Page(1, "S&P\tfirst"), Page(2, "Second page")]
    assert FakeConverter.document.exports == [(1, False), (2, False)]  # HTML escaping off
