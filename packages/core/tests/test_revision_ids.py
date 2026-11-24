import pytest

from alexandria_core.migrate import FIRST_REVISION, MAX_REVISION_ID_LENGTH, detect_revision, next_revision_id

EXISTING = ["0003_duplicate_detection", "0002_document_metadata", "0001_initial_schema"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("add tags", "0004_add_tags"),
        ("Add Tags.", "0004_add_tags"),
        ("  drop sync_state's cursor!  ", "0004_drop_sync_state_s_cursor"),
        ("v2 index", "0004_v2_index"),
    ],
)
def test_next_revision_id_numbers_after_the_newest(message: str, expected: str) -> None:
    assert next_revision_id(message, EXISTING) == expected


def test_the_first_revision_is_0001() -> None:
    assert next_revision_id("initial schema", []) == "0001_initial_schema"


def test_ids_without_a_number_are_ignored() -> None:
    assert next_revision_id("x", ["abc123", "0009_nine", "0010_ten"]) == "0011_x"


def test_long_descriptions_are_cut_to_fit_alembic_version() -> None:
    revision = next_revision_id("add a very long column name to the documents table", EXISTING)

    assert len(revision) <= MAX_REVISION_ID_LENGTH
    assert revision == "0004_add_a_very_long_column_name"
    assert not next_revision_id("a" * 26 + " b", EXISTING).endswith("_")


def test_a_description_needs_letters_or_digits() -> None:
    with pytest.raises(ValueError, match="letters or digits"):
        next_revision_id(" -- ", EXISTING)


FIRST = {"id", "source_id", "path", "title", "content_hash", "page_count", "status", "error", "ingested_at"}
METADATA = {"authors", "year", "doi", "doc_type"}
DUPLICATES = {"content_digest", "duplicate_of"}


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        (FIRST, FIRST_REVISION),
        (FIRST | METADATA, "0002_document_metadata"),
        (FIRST | METADATA | DUPLICATES, "0003_duplicate_detection"),
    ],
)
def test_detect_revision_by_columns(columns: set[str], expected: str) -> None:
    assert detect_revision(columns) == expected


@pytest.mark.parametrize("columns", [FIRST | {"authors"}, FIRST | METADATA | {"content_digest"}, FIRST | DUPLICATES])
def test_detect_revision_refuses_a_table_no_release_made(columns: set[str]) -> None:
    with pytest.raises(RuntimeError, match="no release made such a table"):
        detect_revision(columns)
