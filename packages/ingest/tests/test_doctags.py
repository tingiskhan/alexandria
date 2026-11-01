"""Literal `<` through docling-core's real DocTags parser, which test_extract_wrapper.py fakes."""

import logging
from dataclasses import dataclass

import pytest
from docling.datamodel.base_models import InputFormat
from docling_core.types.doc import DoclingDocument
from docling_core.types.doc.tokens import DocumentToken
from PIL import Image

from alexandria_core.models import FileChange, Page
from alexandria_ingest import extract
from alexandria_ingest.doctags import LiteralTextVlmPipeline, hide_literal_less_than, restore_literal_less_than
from alexandria_ingest.extract import DoclingExtractor
from alexandria_ingest.pipeline import process_file, warn_about_truncation

from .fakes import FakeEmbedder, FakeFileSource, InMemoryStore

LOC = "<loc_10><loc_10><loc_100><loc_100>"


def page(doctags: str) -> str:
    """A page of model output; `@` stands for an item's location tags."""
    return f"<doctag>{doctags.replace('@', LOC)}</doctag>"


def parse(doctags: str) -> DoclingDocument:
    """What VlmPipeline does with a page's model output, on the pipeline we install."""
    return LiteralTextVlmPipeline._doctags_page_document(page(doctags), Image.new("RGB", (100, 100)))


def markdown(doctags: str) -> str:
    return restore_literal_less_than(parse(doctags).export_to_markdown(escape_html=False))


@pytest.mark.parametrize(
    ("doctags", "expected"),
    [
        ("<text>@x$_{<t}$, z$_{<t}$ and more text after</text>", r"x$\_{<t}$, z$\_{<t}$ and more text after"),
        ("<text>@if a <b and c > d then more</text>", "if a <b and c > d then more"),
        ("<text>@the mean <x> of more</text>", "the mean <x> of more"),
        ("<text>@given a < t and more</text>", "given a < t and more"),
        ("<section_header_level_1>@On x_{<t} and more</section_header_level_1>", r"## On x\_{<t} and more"),
        ("<formula>@x_{<t} = y</formula>", "$$x_{<t} = y$$"),
        ("<unordered_list><list_item>@x$_{<t}$ and more</list_item></unordered_list>", r"- x$\_{<t}$ and more"),
        ("<code>@<_C_>#include <stdio.h>\nint i<n;</code>", "```\n#include <stdio.h>\nint i<n;\n```"),
    ],
)
def test_text_after_a_literal_less_than_survives(doctags: str, expected: str) -> None:
    assert markdown(doctags) == expected


def test_table_cells_keep_a_literal_less_than() -> None:
    table = markdown("<otsl>@<ched>name<ched>bound<nl><fcel>a<fcel>x<y and more<nl></otsl>")

    assert "x<y and more" in table


def test_real_tags_are_left_alone() -> None:
    tags = [*DocumentToken.get_special_tokens(), "<section_header_level_6>", "<key_1>", "</value_12>", "<link_3>"]

    assert hide_literal_less_than("".join(tags)) == "".join(tags)


@dataclass
class FakeResult:
    document: DoclingDocument


class ParsingConverter:
    """Docling's converter with the model replaced by canned DocTags, parsed for real."""

    def __init__(self, allowed_formats: list[InputFormat], format_options: dict) -> None:
        self.pipeline_cls = format_options[InputFormat.PDF].pipeline_cls

    def convert(self, _stream: object) -> FakeResult:
        document = self.pipeline_cls._doctags_page_document(
            page("<text>@x$_{<t}$, z$_{<t}$ and more text after</text>"),
            Image.new("RGB", (100, 100)),
        )
        return FakeResult(document)


def test_a_literal_less_than_survives_the_full_ingest_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract, "DocumentConverter", ParsingConverter)
    source = FakeFileSource(files={"id-1": b"%PDF-1.7"})
    store = InMemoryStore()

    outcome = process_file(
        FileChange(source_id="id-1", path="/papers/filtering.pdf", content_hash="h1"),
        source=source,
        extractor=DoclingExtractor({"pdf"}),
        embedder=FakeEmbedder(),
        store=store,
        max_tokens=500,
        formats={"pdf"},
    )

    stored = store.documents["id-1"]
    expected = r"x$\_{<t}$, z$\_{<t}$ and more text after"  # Docling's Markdown serializer escapes the underscore
    assert outcome == "ingested"
    assert [p.markdown for p in stored.doc.pages] == [expected]
    assert [c.chunk.text for c in stored.chunks] == [expected]


def test_truncated_text_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        warn_about_truncation("a.pdf", [Page(2, "Fine x$\\_{t}$.\n\nCut off at x$\\_{\n\nFine again.")])

    [record] = caplog.records
    assert "a.pdf p. 2" in record.getMessage()
    assert "Cut off at" in record.getMessage()


def test_complete_text_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        warn_about_truncation("a.pdf", [Page(1, "x$\\_{<t}$ and more.\n\n$$x_{t}$$")])

    assert not caplog.records
