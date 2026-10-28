from pathlib import Path

import pytest

from alexandria_ingest.extract import DoclingExtractor

pytestmark = pytest.mark.gpu

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def extractor() -> DoclingExtractor:
    return DoclingExtractor()


def test_born_digital_pdf_gives_text_per_page(extractor: DoclingExtractor) -> None:
    pages = extractor.extract((DATA / "born_digital.pdf").read_bytes())

    assert [p.number for p in pages] == [1, 2]
    assert "revenue rose twelve percent" in pages[0].markdown.lower()
    assert "page two" in pages[1].markdown.lower()


def test_scanned_pdf_is_ocred(extractor: DoclingExtractor) -> None:
    pages = extractor.extract((DATA / "scanned.pdf").read_bytes())

    assert [p.number for p in pages] == [1]
    assert "4471" in pages[0].markdown
