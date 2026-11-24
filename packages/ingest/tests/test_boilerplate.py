from alexandria_core.models import Page
from alexandria_ingest.boilerplate import BoilerplateRules, compile_patterns, strip_boilerplate
from alexandria_ingest.chunk import chunk_pages

RULES = BoilerplateRules()

NOTICE = """\
# Monetary Policy and the Yield Curve

Author(s): A. Writer

Stable URL: https://archive.example.org/stable/123456

Your use of the Example Archive indicates your acceptance of its Terms of Use, available at \
https://archive.example.org/terms. Articles may not be redistributed without permission.

This content downloaded from 10.0.0.1 on Mon, 01 Jan 2024 12:00:00 UTC. All use subject to the terms."""


TOPICS = ("bonds", "inflation", "expectations", "term premia", "central banks", "liquidity", "duration")


def body(n: int) -> str:
    return f"This section discusses {TOPICS[n]} and how they shape the term structure of interest rates."


def journal_pages(count: int) -> list[Page]:
    """Pages of a journal article, each with the publisher's running header carrying the page number."""
    return [
        Page(number=n, markdown=f"## Journal of Finance, Vol. 61, No. 2 (2006), pp. {100 + n}\n\n{body(n)}")
        for n in range(1, count + 1)
    ]


def texts(pages: list[Page]) -> list[str]:
    return [page.markdown for page in pages]


def test_running_header_is_dropped_from_every_page() -> None:
    stripped = strip_boilerplate(journal_pages(5), RULES)

    assert [p.number for p in stripped] == [1, 2, 3, 4, 5]
    assert all("Journal of Finance" not in text for text in texts(stripped))
    assert [text.strip() for text in texts(stripped)] == [body(n) for n in range(1, 6)]


def test_running_header_no_longer_reaches_chunks_or_sections() -> None:
    chunks = chunk_pages(strip_boilerplate(journal_pages(5), RULES), max_tokens=500)

    assert all("Journal of Finance" not in c.text and c.section == "" for c in chunks)
    assert (chunks[0].page_start, chunks[-1].page_end) == (1, 5)


def test_short_documents_keep_repeated_lines() -> None:
    pages = journal_pages(3)

    assert strip_boilerplate(pages, RULES) == pages


def test_a_line_on_fewer_pages_than_the_ratio_is_kept() -> None:
    pages = [
        Page(number=n, markdown=("A note that shows up now and then\n\n" if n <= 3 else "") + body(n))
        for n in range(1, 7)
    ]  # 3 of 6 pages, under 60%

    assert strip_boilerplate(pages, RULES) == pages


def test_empty_pages_do_not_count_towards_the_ratio() -> None:
    pages = [*journal_pages(4), Page(number=5, markdown=""), Page(number=6, markdown="  ")]

    assert all("Journal" not in text for text in texts(strip_boilerplate(pages, RULES)))


def test_table_rows_and_math_delimiters_are_never_treated_as_headers() -> None:
    table = "| Year | Rate |\n| --- | --- |\n| 2020 | 1.5 |"
    pages = [Page(number=n, markdown=f"{table}\n\n$$\nx^{n}\n$$\n\n{body(n)}") for n in range(1, 6)]

    assert strip_boilerplate(pages, RULES) == pages


def test_configured_patterns_drop_matching_lines() -> None:
    rules = BoilerplateRules(patterns=compile_patterns([r"^preprint\b"]))
    pages = [Page(number=1, markdown="PREPRINT – not peer reviewed\n\n# Results\n\nRevenue rose.")]

    assert texts(strip_boilerplate(pages, rules)) == ["\n# Results\n\nRevenue rose."]


def test_front_matter_page_is_skipped_but_keeps_its_number() -> None:
    pages = [Page(number=1, markdown=NOTICE), Page(number=2, markdown="# Introduction\n\nRates moved.")]

    stripped = strip_boilerplate(pages, RULES)

    assert stripped == [Page(number=1, markdown=""), pages[1]]
    [chunk] = chunk_pages(stripped, max_tokens=500)
    assert (chunk.page_start, chunk.text) == (2, "# Introduction\n\nRates moved.")


def test_first_page_with_mostly_content_is_kept() -> None:
    first = "# Introduction\n\n" + " ".join(body(n) for n in range(5)) + "\n\n© 2024 The Authors."
    pages = [Page(number=1, markdown=first), Page(number=2, markdown=body(2))]

    assert strip_boilerplate(pages, RULES) == pages


def test_single_page_documents_are_never_skipped() -> None:
    pages = [Page(number=1, markdown=NOTICE)]

    assert strip_boilerplate(pages, RULES) == pages


def test_front_matter_detection_can_be_turned_off() -> None:
    pages = [Page(number=1, markdown=NOTICE), Page(number=2, markdown="")]

    assert strip_boilerplate(pages, BoilerplateRules(front_matter_patterns=())) == pages
