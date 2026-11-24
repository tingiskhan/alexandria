r"""Transcripts: timestamped WebVTT (.vtt) and SubRip (.srt) files to pages of fixed time blocks.

Transcription happens outside Alexandria, e.g. Whisper writing `.vtt` files into the source folder. Here the cues
are grouped by the block they start in (ALEXANDRIA_EXTRACT_TRANSCRIPT_BLOCK_MINUTES, 5 by default), and each block
becomes one page that starts with its time range as a heading:

    ## [00:15:00–00:20:00]

    Alice: What we found was that the effect only shows up in winter.

    Bob: And only in the north.

So `search` results name the time range as their section, and `get_pages` returns whole blocks. Consecutive cues
are merged into paragraphs, with a new one when the speaker changes, after a pause, or after a few sentences.

Page N is always block N, so a page number is a time: a block in which nothing was said is a page that says so,
rather than a missing page that would shift the pages after it (and the document's page count, which `reindex`
relies on). A transcript that names a title in its WebVTT header (`WEBVTT - Episode 12`) gets it as a `#` heading
above page 1's time range; MetadataReader reads it from there.

The parser is lenient: cue identifiers, settings after the timing, `NOTE`, `STYLE` and `REGION` blocks, voice
(`<v Speaker>`), class and timestamp tags, HTML entities, SubRip's `<font>` and `{\an8}` tags, a byte order mark,
CRLF line endings, timestamps without hours, and a comma or a dot before the milliseconds are all accepted.
"""

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from alexandria_core.models import Page

_TIMESTAMP = r"(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?"
_TIMING = re.compile(rf"^\s*{_TIMESTAMP}\s*-->\s*{_TIMESTAMP}")
_HEADER = re.compile(r"^WEBVTT(?:[ \t]+(.*))?$")
_SKIPPED_BLOCKS = re.compile(r"^(?:NOTE(?:[ \t].*)?|STYLE[ \t]*|REGION[ \t]*)$")

_VOICE = re.compile(r"<v(?:\.[^\s>]*)?[ \t]+([^>]*)>")
_RUBY_TEXT = re.compile(r"<rt>.*?</rt>")
_TAG = re.compile(r"</?[a-zA-Z][^>]*>|<\d[\d:.]*>")  # markup and karaoke timestamps, but not "a < b"
_SUBRIP_OVERRIDE = re.compile(r"\{\\[^}]*\}")  # {\an8}, {\i1}: ASS style overrides some SubRip files carry
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LOOKS_LIKE_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_TITLE = re.compile(r"^# (.+)$")

PAUSE_SECONDS = 2.0  # a silence at least this long between two cues starts a new paragraph
PARAGRAPH_CHARS = 600  # after this many characters, the next sentence starts a new paragraph
NOTHING_SAID = "(nothing transcribed in this block)"


@dataclass(frozen=True)
class Cue:
    """What one speaker says from `start` to `end` (seconds). A cue with several voices gives one Cue per voice."""

    start: float
    end: float
    text: str
    speaker: str | None = None


@dataclass(frozen=True)
class Transcript:
    """A parsed transcript: its cues in order of start time, and the title its header names, if any."""

    cues: tuple[Cue, ...]
    title: str | None = None


def parse_transcript(text: str) -> Transcript:
    """Parse WebVTT or SubRip text. Blocks without a timing line (headers, notes, styles, junk) are skipped."""
    lines = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title = None
    if lines and (header := _HEADER.match(lines[0].strip())):
        title = _clean_title(header.group(1) or "")
        lines = lines[1:]
    cues: list[Cue] = []
    for block in re.split(r"\n[ \t]*\n", "\n".join(lines)):
        cues.extend(_parse_block([line.strip() for line in block.strip("\n").split("\n")]))
    return Transcript(cues=tuple(sorted(cues, key=lambda c: c.start)), title=title)


def transcript_pages(transcript: Transcript, block_seconds: int) -> list[Page]:
    """One page per `block_seconds` long block, up to the block of the last cue's start; page N is block N."""
    if block_seconds <= 0:
        raise ValueError("block_seconds must be positive")
    count = 1 + max((int(cue.start // block_seconds) for cue in transcript.cues), default=0)
    blocks: list[list[Cue]] = [[] for _ in range(count)]
    for cue in transcript.cues:
        blocks[int(cue.start // block_seconds)].append(cue)
    pages = []
    for index, cues in enumerate(blocks):
        start = index * block_seconds
        heading = f"## [{format_time(start)}–{format_time(start + block_seconds)}]"
        body = "\n\n".join(_paragraphs(cues)) or NOTHING_SAID
        markdown = f"{heading}\n\n{body}"
        if index == 0 and transcript.title:
            markdown = f"# {transcript.title}\n\n{markdown}"
        pages.append(Page(number=index + 1, markdown=markdown))
    return pages


def format_time(seconds: int) -> str:
    """`seconds` as HH:MM:SS, e.g. 900 → 00:15:00."""
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def transcript_title(first_page: str) -> str | None:
    """The title `transcript_pages` put above page 1's time range, if the transcript named one."""
    match = _TITLE.match(first_page.split("\n", 1)[0])
    return match.group(1).strip() if match else None


class TranscriptExtractor:
    """An Extractor for timestamped transcripts (.vtt, .srt): one page per `block_minutes` block of time.

    Reads UTF-8 (with or without a byte order mark), and falls back to Windows-1252, which older SubRip files use.
    Raises ValueError for a file without a single timed cue, so it is recorded as failed rather than stored empty.
    """

    def __init__(self, block_minutes: int = 5) -> None:
        if block_minutes <= 0:
            raise ValueError("block_minutes must be positive")
        self._block_seconds = block_minutes * 60

    def extract(self, content: bytes, name: str) -> list[Page]:
        """Parse the transcript and cut it into pages of fixed time blocks."""
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1252", errors="replace")
        transcript = parse_transcript(text)
        if not transcript.cues:
            raise ValueError(f"{name}: no timed cues found; is it a WebVTT or SubRip transcript?")
        return transcript_pages(transcript, self._block_seconds)


def _parse_block(lines: list[str]) -> list[Cue]:
    """The cues of one blank-line separated block: an optional identifier, the timing line, then the text."""
    if not lines or _SKIPPED_BLOCKS.match(lines[0]):
        return []
    for index, line in enumerate(lines):
        if timing := _TIMING.match(line):
            start, end = _seconds(*timing.groups()[:4]), _seconds(*timing.groups()[4:])
            return _cues(start, end, lines[index + 1 :])
    return []


def _seconds(hours: str | None, minutes: str, seconds: str, fraction: str | None) -> float:
    """A matched timestamp in seconds; the fraction is milliseconds, padded on the right ("5" is 500 ms)."""
    millis = int((fraction or "0").ljust(3, "0"))
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + millis / 1000


def _cues(start: float, end: float, lines: list[str]) -> list[Cue]:
    """A cue's text lines as Cues, one per voice; a line without a voice tag continues the voice before it."""
    cues: list[Cue] = []
    speaker: str | None = None
    for line in lines:
        if voice := _VOICE.search(line):
            speaker = " ".join(voice.group(1).split()) or None
        if not (text := _plain(line)):
            continue
        if cues and cues[-1].speaker == speaker:
            cues[-1] = replace(cues[-1], text=f"{cues[-1].text} {text}")
        else:
            cues.append(Cue(start=start, end=end, text=text, speaker=speaker))
    return cues


def _plain(line: str) -> str:
    """A line of cue text without markup, entities or extra whitespace."""
    text = _SUBRIP_OVERRIDE.sub("", _TAG.sub("", _RUBY_TEXT.sub("", line)))
    return " ".join(_CONTROL.sub("", html.unescape(text)).split())


def _clean_title(text: str) -> str | None:
    """The text after "WEBVTT" in a header, without the separator people put before it ("WEBVTT - Title")."""
    title = " ".join(text.split()).strip("-–—:| ")
    return title or None


def _paragraphs(cues: Sequence[Cue]) -> list[str]:
    """Merge consecutive cues into paragraphs, each prefixed with its speaker when the transcript names one."""
    paragraphs: list[tuple[str | None, list[str]]] = []
    previous: Cue | None = None
    for cue in cues:
        if previous is None or _starts_paragraph(cue, previous, paragraphs[-1]):
            paragraphs.append((cue.speaker, [cue.text]))
        elif (cue.speaker, cue.text) != (previous.speaker, previous.text):  # rolling captions repeat the line before
            paragraphs[-1][1].append(cue.text)
        previous = cue
    return [_render(speaker, " ".join(texts)) for speaker, texts in paragraphs]


def _starts_paragraph(cue: Cue, previous: Cue, current: tuple[str | None, list[str]]) -> bool:
    """A new speaker, a pause, or a sentence end in a paragraph that is long enough already."""
    speaker, texts = current
    if cue.speaker != speaker or cue.start - previous.end >= PAUSE_SECONDS:
        return True
    return sum(len(t) + 1 for t in texts) >= PARAGRAPH_CHARS and texts[-1].endswith((".", "?", "!", "…"))


def _render(speaker: str | None, text: str) -> str:
    """`Speaker: text`, or the text alone, escaped so it can't read as a Markdown heading."""
    if speaker:
        return f"{speaker}: {text}"
    return "\\" + text if _LOOKS_LIKE_HEADING.match(text) else text
