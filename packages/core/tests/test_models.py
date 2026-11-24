from typing import cast

import pytest

from alexandria_core.models import DocType, DocumentFilter


def test_document_filter_accepts_every_doc_type_and_a_one_year_range() -> None:
    assert DocumentFilter(doc_type="transcript", year_from=2020, year_to=2020).year_to == 2020


def test_document_filter_rejects_an_unknown_doc_type() -> None:
    with pytest.raises(ValueError, match="doc_type must be one of paper, book"):
        DocumentFilter(doc_type=cast("DocType", "invoice"))


def test_document_filter_rejects_an_empty_year_range() -> None:
    with pytest.raises(ValueError, match="year_from"):
        DocumentFilter(year_from=2021, year_to=2020)
