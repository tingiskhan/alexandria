"""Transcripts to pages of time blocks. The fixtures in data/ are synthetic."""

from pathlib import Path

import pytest

from alexandria_core.models import Page
from alexandria_ingest.transcript import (
    NOTHING_SAID,
    Cue,
    Transcript,
    TranscriptExtractor,
    format_time,
    parse_transcript,
    transcript_pages,
    transcript_title,
)

DATA = Path(__file__).parent / "data"


def test_webvtt_becomes_one_page_per_five_minutes_with_speakers() -> None:
    pages = TranscriptExtractor().extract((DATA / "talk.vtt").read_bytes(), "talk.vtt")

    assert pages == [
        Page(
            1,
            "# Widgets in Winter\n\n## [00:00:00–00:05:00]\n\n"
            "Alice: Welcome to the show. Today we talk about widgets in winter & spring.\n\n"
            "Bob Smith: Thanks for having me. The effect only shows up in the north.",
        ),
        Page(2, f"## [00:05:00–00:10:00]\n\n{NOTHING_SAID}"),  # kept, so page 3 is still minutes 10 to 15
        Page(3, "## [00:10:00–00:15:00]\n\nAlice: Welcome back after the break."),
    ]


def test_subrip_is_read_the_same_way() -> None:
    pages = TranscriptExtractor().extract((DATA / "talk.srt").read_bytes(), "talk.srt")

    assert pages == [
        Page(1, "## [00:00:00–00:05:00]\n\nWelcome to the show. Today we talk about widgets, and whether a < b."),
        Page(2, "## [00:05:00–00:10:00]\n\nBack after the break."),
    ]


def test_block_length_is_configurable() -> None:
    pages = TranscriptExtractor(block_minutes=1).extract((DATA / "talk.srt").read_bytes(), "talk.srt")

    assert len(pages) == 6
    assert pages[5].markdown == "## [00:05:00–00:06:00]\n\nBack after the break."


def test_byte_order_mark_and_crlf_are_accepted() -> None:
    content = "﻿WEBVTT\r\n\r\n1\r\n00:00:01.000 --> 00:00:02.000\r\nHello\r\nthere.\r\n".encode()

    assert TranscriptExtractor().extract(content, "a.vtt") == [Page(1, "## [00:00:00–00:05:00]\n\nHello there.")]


def test_old_carriage_returns_and_windows_1252_are_accepted() -> None:
    content = "1\r00:00:01,000 --> 00:00:02,000\rCaf\xe9 au lait\r".encode("cp1252")

    [page] = TranscriptExtractor().extract(content, "a.srt")

    assert page.markdown.endswith("Café au lait")


@pytest.mark.parametrize(
    ("timing", "start"),
    [
        ("01:02.5 --> 01:03.000", 62.5),  # no hours, short fraction
        ("1:02:03,250 --> 1:02:04,000", 3723.25),  # comma, one-digit hours
        ("100:00:00.000 --> 100:00:01.000", 360000.0),  # more than 99 hours
        ("00:00:07 --> 00:00:08", 7.0),  # no milliseconds
    ],
)
def test_timestamp_variants(timing: str, start: float) -> None:
    [cue] = parse_transcript(f"WEBVTT\n\n{timing}\ntext").cues

    assert cue.start == start


def test_several_voices_in_one_cue_and_voice_continuation() -> None:
    text = "WEBVTT\n\n00:01.000 --> 00:02.000\n<v Ann>Hi.\nHow are you?\n<v Ben>\nFine.</v>\n"

    cues = parse_transcript(text).cues

    assert [(c.speaker, c.text) for c in cues] == [("Ann", "Hi. How are you?"), ("Ben", "Fine.")]


def test_markup_is_removed() -> None:
    text = (
        "WEBVTT\n\n00:01.000 --> 00:02.000\n"
        "<ruby>漢<rt>kan</rt></ruby> <u>under</u> <lang en>x</lang> <00:00:01.500>karaoke &lt;tag&gt;\x07\n"
    )

    [cue] = parse_transcript(text).cues

    assert cue.text == "漢 under x karaoke <tag>"


def test_blocks_without_timing_are_skipped() -> None:
    text = (
        "WEBVTT\n\nNOTE\n00:01.000 --> 00:02.000 in a note\n\nREGION\nid:x\n\njunk line\n\n00:03.000 --> 00:04.000\nOK"
    )

    assert [c.text for c in parse_transcript(text).cues] == ["OK"]


def test_cues_are_sorted_by_start() -> None:
    text = "00:00:09,000 --> 00:00:10,000\nsecond\n\n00:00:01,000 --> 00:00:02,000\nfirst"

    assert [c.text for c in parse_transcript(text).cues] == ["first", "second"]


@pytest.mark.parametrize(
    ("header", "title"), [("WEBVTT", None), ("WEBVTT\tEpisode 1", "Episode 1"), ("WEBVTT - ", None)]
)
def test_title_comes_from_the_header(header: str, title: str | None) -> None:
    assert parse_transcript(f"{header}\n\n00:01.000 --> 00:02.000\nx").title == title


def test_a_file_without_cues_fails() -> None:
    with pytest.raises(ValueError, match=r"notes\.vtt: no timed cues"):
        TranscriptExtractor().extract(b"WEBVTT\n\nNOTE nothing here\n", "notes.vtt")


def test_block_length_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        TranscriptExtractor(block_minutes=0)
    with pytest.raises(ValueError, match="positive"):
        transcript_pages(Transcript(cues=()), 0)


def test_an_empty_transcript_is_one_empty_block() -> None:
    assert transcript_pages(Transcript(cues=()), 300) == [Page(1, f"## [00:00:00–00:05:00]\n\n{NOTHING_SAID}")]


def test_paragraphs_break_on_pauses_and_after_long_runs_of_sentences() -> None:
    sentence, other = "x" * 299 + ".", "y" * 299 + "."
    cues = (
        Cue(0, 1, "Before the pause."),
        Cue(3, 4, sentence),  # 2 s of silence before it
        Cue(4, 5, other),
        Cue(5, 6, "Long enough now, so this starts a new paragraph."),
        Cue(6, 7, "# not a heading"),
    )

    [page] = transcript_pages(Transcript(cues=cues), 300)

    assert page.markdown.split("\n\n")[1:] == [
        "Before the pause.",
        f"{sentence} {other}",
        "Long enough now, so this starts a new paragraph. # not a heading",
    ]


def test_text_that_looks_like_a_heading_is_escaped() -> None:
    [page] = transcript_pages(Transcript(cues=(Cue(0, 1, "# 1 hashtag"),)), 300)

    assert page.markdown.endswith("\n\n\\# 1 hashtag")


def test_a_cue_belongs_to_the_block_it_starts_in() -> None:
    pages = transcript_pages(Transcript(cues=(Cue(299.5, 302, "Across the boundary."),)), 300)

    assert pages == [Page(1, "## [00:00:00–00:05:00]\n\nAcross the boundary.")]


def test_format_time() -> None:
    assert format_time(0) == "00:00:00"
    assert format_time(3723) == "01:02:03"


def test_transcript_title_reads_page_one() -> None:
    assert transcript_title("# Episode 1\n\n## [00:00:00–00:05:00]\n\nHi") == "Episode 1"
    assert transcript_title("## [00:00:00–00:05:00]\n\n# Hi") is None
    assert transcript_title("") is None
