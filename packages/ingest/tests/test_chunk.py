from alexandria_core.models import Chunk, Page
from alexandria_ingest.chunk import chunk_pages, embedding_text

TABLE = "| Year | Revenue |\n| --- | --- |\n| 2024 | 10 |\n| 2025 | 12 |"


def test_splits_on_headings_and_tracks_heading_path() -> None:
    markdown = "# Report\n\nIntro text.\n\n## Revenue\n\nRevenue rose 12%.\n\n## Costs\n\nCosts fell."

    chunks = chunk_pages([Page(number=1, markdown=markdown)], max_tokens=500)

    assert [(c.section, c.text) for c in chunks] == [
        ("Report", "# Report\n\nIntro text."),
        ("Report > Revenue", "## Revenue\n\nRevenue rose 12%."),
        ("Report > Costs", "## Costs\n\nCosts fell."),
    ]


def test_heading_levels_nest_and_pop() -> None:
    markdown = "## A\n\na\n\n### B\n\nb\n\n## C\n\nc\n\n# D\n\nd\n\n### E\n\ne"

    chunks = chunk_pages([Page(number=1, markdown=markdown)], max_tokens=500)

    assert [c.section for c in chunks] == ["A", "A > B", "C", "D", "D > E"]


def test_heading_only_section_is_kept() -> None:
    pages = [Page(number=1, markdown="## Scanned invoice number 4471"), Page(number=2, markdown="## Next\n\nBody")]

    chunks = chunk_pages(pages, max_tokens=500)

    assert [(c.page_start, c.section, c.text) for c in chunks] == [
        (1, "Scanned invoice number 4471", "## Scanned invoice number 4471"),
        (2, "Next", "## Next\n\nBody"),
    ]


def test_packs_paragraphs_up_to_limit() -> None:
    paragraph = "word " * 30  # ~150 chars
    markdown = "\n\n".join([paragraph] * 6)

    chunks = chunk_pages([Page(number=1, markdown=markdown)], max_tokens=100)  # 400 chars

    assert len(chunks) == 3
    assert all(len(c.text) <= 400 for c in chunks)


def test_table_kept_whole() -> None:
    markdown = f"{'x' * 380}\n\n{TABLE}"

    chunks = chunk_pages([Page(number=1, markdown=markdown)], max_tokens=100)

    assert [c.text for c in chunks] == ["x" * 380, TABLE]


def test_oversized_table_split_by_rows_with_header() -> None:
    rows = "\n".join(f"| {year} | {'9' * 40} |" for year in range(2000, 2030))
    table = f"| Year | Revenue |\n| --- | --- |\n{rows}"

    chunks = chunk_pages([Page(number=1, markdown=table)], max_tokens=100)

    assert len(chunks) > 1
    assert all(c.text.startswith("| Year | Revenue |\n| --- | --- |\n") for c in chunks)
    assert sum(c.text.count("| 20") for c in chunks) == 30


def test_oversized_paragraph_split_on_words() -> None:
    chunks = chunk_pages([Page(number=1, markdown="word " * 500)], max_tokens=100)

    assert len(chunks) > 1
    assert all(len(c.text) <= 400 for c in chunks)


def test_page_ranges_and_headings_carry_across_pages() -> None:
    pages = [Page(number=4, markdown="## Method\n\nFirst part."), Page(number=5, markdown="Second part.")]

    [chunk] = chunk_pages(pages, max_tokens=500)

    assert (chunk.page_start, chunk.page_end, chunk.section) == (4, 5, "Method")


def test_embedding_text_has_title_and_heading_prefix() -> None:
    chunk = Chunk(page_start=1, page_end=1, section="Results > Revenue", text="Revenue rose 12%")

    assert embedding_text("Annual report", chunk) == "Annual report > Results > Revenue\n\nRevenue rose 12%"
    assert embedding_text("Annual report", Chunk(1, 1, "", "x")) == "Annual report\n\nx"


def test_empty_pages_give_no_chunks() -> None:
    assert chunk_pages([Page(number=1, markdown="  \n\n ")], max_tokens=500) == []
