from alexandria_ingest.extract import clean_text


def test_removes_nul_and_other_control_characters() -> None:
    assert clean_text("matrix P \x00 uk and f \x16uk\x7f") == "matrix P  uk and f uk"


def test_keeps_tabs_newlines_and_unicode() -> None:
    text = "## Title\n\n| a\t| θ ≥ 0.05 |\nñ ð Þ"
    assert clean_text(text) == text
