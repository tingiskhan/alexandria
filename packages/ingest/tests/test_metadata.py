from datetime import UTC, datetime

import pytest

from alexandria_core.models import DocType, DocumentMetadata
from alexandria_ingest.crossref import CrossrefLookup
from alexandria_ingest.metadata import (
    FirstPage,
    MetadataReader,
    doc_type_for,
    find_doi,
    find_year,
    make_metadata_reader,
    read_first_page,
)
from alexandria_ingest.settings import MetadataSettings

from .fakes import FakeBibliographyLookup

PAPER = """\
<!-- image -->

## Deep Learning for Widgets

Ada Lovelace1, Charles Babbage2,* and Grace van Hopper1

1 University of Somewhere, 2 Analytical Engines Ltd.

Received 3 March 2019; accepted 12 January 2020

## Abstract

We study widgets. https://doi.org/10.1234/widgets.2020.001.
"""


def test_reads_title_authors_year_and_doi_from_a_paper() -> None:
    assert read_first_page(PAPER) == FirstPage(
        title="Deep Learning for Widgets",
        authors=("Ada Lovelace", "Charles Babbage", "Grace van Hopper"),
        year=2020,
        doi="10.1234/widgets.2020.001",
    )


def test_authors_come_from_the_paragraph_below_the_title() -> None:
    page = "# **Deep Learning for Widgets**\n\nAda Lovelace1, Charles Babbage2,* and Grace van Hopper1\n\n## Abstract"

    found = read_first_page(page)

    assert found.title == "Deep Learning for Widgets"
    assert found.authors == ("Ada Lovelace", "Charles Babbage", "Grace van Hopper")


@pytest.mark.parametrize(
    "line",
    [
        "This paper studies widgets in depth, and finds them wanting.",  # prose
        "Department of Physics, Stanford University",  # an affiliation
        "Jane Doe, jane@example.org",  # an email address
        "J. Doe (2020)",  # digits
        "Doe",  # a single word
    ],
)
def test_lines_that_are_not_names_are_not_authors(line: str) -> None:
    assert read_first_page(f"# A Title\n\n{line}\n").authors == ()


def test_section_names_and_short_headings_are_not_titles() -> None:
    page = "## Abstract\n\nText\n\n## 1. Introduction\n\n## 42\n\n## Contents:\n\n## Real Title\n\nJane Doe"

    assert read_first_page(page) == FirstPage(title="Real Title", authors=("Jane Doe",))


def test_a_page_without_headings_has_no_title() -> None:
    assert read_first_page("Just some text.\n\nMore text.") == FirstPage()


def test_authors_stop_at_the_next_heading() -> None:
    assert read_first_page("# Title here\n## Next\nJane Doe").authors == ()


def test_inline_maths_footnote_marks_are_dropped_from_names() -> None:
    page = r"# A Title" + "\n\n" + r"Jane Doe\(^{1}\), John Smith$^{2}$"

    assert read_first_page(page).authors == ("Jane Doe", "John Smith")


@pytest.mark.parametrize(
    ("text", "doi"),
    [
        ("doi:10.1000/xyz123.", "10.1000/xyz123"),
        ("(see https://doi.org/10.1000/abc)", "10.1000/abc"),
        ("10.1002/(SICI)1097-4636(199706)35:4<1::AID>", "10.1002/(SICI)1097-4636(199706)35:4"),
        ("[10.1000/a\\_b](https://doi.org/10.1000/a_b)", "10.1000/a_b"),
        ("no identifier here, 10.12/short", None),
    ],
)
def test_find_doi(text: str, doi: str | None) -> None:
    assert find_doi(text) == doi


@pytest.mark.parametrize(
    ("text", "title", "year"),
    [
        ("© 2018 Elsevier. Published 5 May 2021.", None, 2018),
        ("Copyright 2015", None, 2015),
        ("Received 3 March 2019; accepted Jan 2020; online 2020-02-01", None, 2020),
        ("Meeting of 5th June, 2011", None, 2011),
        ("Nothing dated here, page 1999 of many.", "Annual report 2023", 2023),
        ("Nothing dated here.", None, None),
        ("© 2999 Future Corp, March 2998", "Plan 2997", None),
    ],
)
def test_find_year(text: str, title: str | None, year: int | None) -> None:
    assert find_year(text, title) == year


def test_find_year_accepts_next_year() -> None:
    next_year = datetime.now(UTC).year + 1

    assert find_year(f"To appear in December {next_year}") == next_year


def test_doc_type_comes_from_the_deepest_matching_folder() -> None:
    doc_types: dict[str, DocType] = {"/papers": "paper", "/papers/books": "book", "/policy": "policy"}

    assert doc_type_for("/Papers/a.pdf", doc_types) == "paper"
    assert doc_type_for("/papers/books/b.pdf", doc_types) == "book"
    assert doc_type_for("/policy-drafts/c.pdf", doc_types) is None
    assert doc_type_for("/c.pdf", doc_types) is None


def test_reader_falls_back_to_the_file_stem_and_other() -> None:
    assert MetadataReader().read("/misc/Q3 notes.pdf", "") == DocumentMetadata(title="Q3 notes", doc_type="other")


def test_reader_uses_the_first_page_and_the_folder() -> None:
    reader = MetadataReader(doc_types={"/papers": "paper"})

    metadata = reader.read("/papers/w.pdf", "# Widgets Revisited\n\nJane Doe\n\n© 2020\n\ndoi:10.1000/w")

    assert metadata == DocumentMetadata(
        title="Widgets Revisited", authors=("Jane Doe",), year=2020, doi="10.1000/w", doc_type="paper"
    )


def test_reader_prefers_what_the_lookup_knows_and_fills_gaps_from_the_page() -> None:
    registered = DocumentMetadata(title="Widgets: Revisited", authors=(), year=2021, doi="10.1000/W", doc_type="paper")
    lookup = FakeBibliographyLookup(known={"10.1000/w": registered})
    reader = MetadataReader(lookup=lookup)

    metadata = reader.read("/w.pdf", "# Widgets Revisited\n\nJane Doe\n\n© 2020\n\ndoi:10.1000/w")

    assert lookup.asked == ["10.1000/w"]
    assert metadata == DocumentMetadata(
        title="Widgets: Revisited", authors=("Jane Doe",), year=2021, doi="10.1000/W", doc_type="paper"
    )


def test_the_folder_type_wins_over_the_lookups() -> None:
    registered = DocumentMetadata(title="Widgets", doc_type="paper")
    reader = MetadataReader(doc_types={"/policies": "policy"}, lookup=FakeBibliographyLookup({"10.1000/w": registered}))

    assert reader.read("/policies/w.pdf", "doi:10.1000/w").doc_type == "policy"


def test_reader_without_a_doi_or_an_answer_uses_the_page() -> None:
    lookup = FakeBibliographyLookup()
    reader = MetadataReader(lookup=lookup)

    assert reader.read("/a.pdf", "# Plain Title").title == "Plain Title"
    assert reader.read("/b.pdf", "# Other Title\n\n10.1000/unknown").title == "Other Title"
    assert lookup.asked == ["10.1000/unknown"]


def test_make_metadata_reader_follows_the_settings() -> None:
    with_crossref = make_metadata_reader(MetadataSettings())
    without = make_metadata_reader(MetadataSettings(crossref=False, doc_types={"/x": "book"}))

    assert isinstance(with_crossref.lookup, CrossrefLookup)
    assert with_crossref.doc_types["/papers"] == "paper"
    assert without.lookup is None
    assert without.doc_types == {"/x": "book"}


def test_transcripts_are_transcripts_titled_by_their_header_without_a_lookup() -> None:
    lookup = FakeBibliographyLookup({"10.1000/w": DocumentMetadata(title="Widgets", doc_type="paper")})
    reader = MetadataReader(doc_types={"/papers": "paper"}, lookup=lookup)
    page = "# Widgets in Winter\n\n## [00:00:00–00:05:00]\n\nJane Doe: In March 2020 we cited doi:10.1000/w."

    assert reader.read("/papers/Episode 12.VTT", page) == DocumentMetadata(
        title="Widgets in Winter", doc_type="transcript"
    )
    assert reader.read("/talks/Episode 13.srt", "## [00:00:00–00:05:00]\n\nJane Doe: Hi.") == DocumentMetadata(
        title="Episode 13", doc_type="transcript"
    )
    assert lookup.asked == []
