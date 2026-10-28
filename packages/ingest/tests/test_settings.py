import pytest
from docling.datamodel.base_models import InputFormat
from pydantic import ValidationError

from alexandria_ingest.settings import DEFAULT_FORMATS, ExtractSettings, input_format


def test_formats_default_to_common_document_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALEXANDRIA_EXTRACT_FORMATS", raising=False)

    assert ExtractSettings().formats == DEFAULT_FORMATS
    assert {"pdf", "docx", "html", "md"} <= DEFAULT_FORMATS


def test_formats_are_comma_separated_and_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_EXTRACT_FORMATS", " pdf, .DOCX ,png,")

    assert ExtractSettings().formats == frozenset({"pdf", "docx", "png"})


@pytest.mark.parametrize(
    ("value", "message"), [("pdf,pfd", "cannot read 'pfd'"), ("xml", "'xml' is ambiguous"), (",", "at least one")]
)
def test_unusable_formats_fail_at_startup(monkeypatch: pytest.MonkeyPatch, value: str, message: str) -> None:
    monkeypatch.setenv("ALEXANDRIA_EXTRACT_FORMATS", value)

    with pytest.raises(ValidationError, match=message):
        ExtractSettings()


def test_text_files_are_read_as_markdown() -> None:
    assert input_format("txt") == InputFormat.MD
    assert input_format("jpeg") == InputFormat.IMAGE
