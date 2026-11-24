import pytest
from docling.datamodel.base_models import InputFormat
from pydantic import ValidationError

from alexandria_ingest.boilerplate import DEFAULT_FRONT_MATTER_PATTERNS
from alexandria_ingest.settings import (
    DEFAULT_DOC_TYPES,
    DEFAULT_FORMATS,
    TRANSCRIPT_FORMATS,
    BoilerplateSettings,
    ExtractSettings,
    MetadataSettings,
    extension,
    input_format,
)


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


def test_transcripts_are_ingested_by_default_and_not_checked_against_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_EXTRACT_FORMATS", "srt,VTT")

    settings = ExtractSettings()

    assert TRANSCRIPT_FORMATS <= DEFAULT_FORMATS
    assert settings.formats == frozenset({"srt", "vtt"})  # Docling can't read srt
    assert settings.transcript_block_minutes == 5


def test_transcript_blocks_are_whole_positive_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_EXTRACT_TRANSCRIPT_BLOCK_MINUTES", "10")
    assert ExtractSettings().transcript_block_minutes == 10

    monkeypatch.setenv("ALEXANDRIA_EXTRACT_TRANSCRIPT_BLOCK_MINUTES", "0")
    with pytest.raises(ValidationError, match="greater than 0"):
        ExtractSettings()


def test_extension_is_lower_case_without_the_dot() -> None:
    assert extension("/a/Talk.VTT") == "vtt"
    assert extension("/a/README") == ""


def test_text_files_are_read_as_markdown() -> None:
    assert input_format("txt") == InputFormat.MD
    assert input_format("jpeg") == InputFormat.IMAGE


def test_metadata_defaults_look_up_dois_and_map_common_folders() -> None:
    settings = MetadataSettings()

    assert settings.crossref is True
    assert settings.crossref_mailto is None
    assert settings.doc_types == DEFAULT_DOC_TYPES


def test_doc_types_are_folder_type_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_METADATA_DOC_TYPES", " Papers/=Paper, reports/policy=policy,,")
    monkeypatch.setenv("ALEXANDRIA_METADATA_CROSSREF", "false")

    settings = MetadataSettings()

    assert settings.doc_types == {"/papers": "paper", "/reports/policy": "policy"}
    assert settings.crossref is False


@pytest.mark.parametrize(
    ("value", "message"),
    [("papers", "folder=type pairs"), ("papers=journal", "'paper'"), ("/=paper", "a folder is required")],
)
def test_unusable_doc_types_fail_at_startup(monkeypatch: pytest.MonkeyPatch, value: str, message: str) -> None:
    monkeypatch.setenv("ALEXANDRIA_METADATA_DOC_TYPES", value)

    with pytest.raises(ValidationError, match=message):
        MetadataSettings()


def test_boilerplate_defaults_detect_headers_and_front_matter(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PATTERNS", "REPEAT_RATIO", "REPEAT_MIN_PAGES", "FRONT_MATTER_PATTERNS"):
        monkeypatch.delenv(f"ALEXANDRIA_BOILERPLATE_{name}", raising=False)

    rules = BoilerplateSettings().rules()

    assert rules.patterns == ()
    assert (rules.repeat_ratio, rules.repeat_min_pages) == (0.6, 4)
    assert len(rules.front_matter_patterns) == len(DEFAULT_FRONT_MATTER_PATTERNS)


def test_boilerplate_patterns_are_json_lists_matched_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALEXANDRIA_BOILERPLATE_PATTERNS", r'["^downloaded from \\S+"]')
    monkeypatch.setenv("ALEXANDRIA_BOILERPLATE_FRONT_MATTER_PATTERNS", "[]")

    rules = BoilerplateSettings().rules()

    [pattern] = rules.patterns
    assert pattern.search("Downloaded from example.org on 1 May")
    assert rules.front_matter_patterns == ()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("PATTERNS", '["(unclosed"]', "invalid regex"),
        ("FRONT_MATTER_PATTERNS", '["[a-"]', "invalid regex"),
        ("REPEAT_RATIO", "0", "greater than 0"),
        ("REPEAT_MIN_PAGES", "1", "greater than or equal to 2"),
    ],
)
def test_bad_boilerplate_settings_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    monkeypatch.setenv(f"ALEXANDRIA_BOILERPLATE_{name}", value)

    with pytest.raises(ValidationError, match=message):
        BoilerplateSettings()
