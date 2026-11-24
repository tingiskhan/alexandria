"""Split per-page Markdown into chunks for embedding and search.

Pure functions with no I/O. Token counts are estimated as characters / CHARS_PER_TOKEN, which is close
enough for sizing chunks and avoids loading a tokenizer.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from alexandria_core.models import Chunk, Page

CHARS_PER_TOKEN = 4  # rough average for English prose
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True)
class _Block:
    """One paragraph, table or heading, tagged with the page it came from."""

    page: int
    text: str


def chunk_pages(pages: Sequence[Page], max_tokens: int) -> list[Chunk]:
    """Split page Markdown into chunks: on headings first, then by packing paragraphs up to max_tokens.

    Tables are kept whole unless a single table exceeds the limit, in which case it is split by rows
    with the header repeated. Each chunk starts with its heading line (if any) and records the heading
    path and the pages it spans.
    """
    max_chars = max_tokens * CHARS_PER_TOKEN
    chunks: list[Chunk] = []
    headings: list[tuple[int, str]] = []  # (level, text) from the outermost heading in
    current: list[_Block] = []

    def flush() -> None:
        """Close the chunk being built, if any."""
        if current:
            chunks.append(
                Chunk(
                    page_start=current[0].page,
                    page_end=current[-1].page,
                    section=" > ".join(text for _, text in headings),
                    text="\n\n".join(b.text for b in current),
                )
            )
            current.clear()

    for page in pages:
        for raw in re.split(r"\n\s*\n", page.markdown):
            text = raw.strip()
            if not text:
                continue
            heading = _HEADING.match(text)
            if heading and "\n" not in text:
                flush()
                level = len(heading.group(1))
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, heading.group(2).strip()))
                # The heading stays as the chunk's first line, so a heading-only section is still searchable.
            for piece in _split_oversized(text, max_chars):
                if current and sum(len(b.text) + 2 for b in current) + len(piece) > max_chars:
                    flush()
                current.append(_Block(page=page.number, text=piece))
    flush()
    return chunks


def embedding_text(title: str, chunk: Chunk) -> str:
    """The text that gets embedded: chunk text prefixed with its context, so short chunks stay meaningful."""
    prefix = f"{title} > {chunk.section}" if chunk.section else title
    return f"{prefix}\n\n{chunk.text}"


def _split_oversized(text: str, max_chars: int) -> list[str]:
    """Split a block that is too big for one chunk: tables by rows (header repeated), anything else by words."""
    if len(text) <= max_chars:
        return [text]
    lines = text.splitlines()
    if all(line.lstrip().startswith("|") for line in lines):
        header, rows = lines[:2], lines[2:]
        return _pack(rows, max_chars - sum(len(h) + 1 for h in header), "\n", prefix="\n".join(header) + "\n")
    return _pack(text.split(), max_chars, " ")


def _pack(items: list[str], max_chars: int, separator: str, prefix: str = "") -> list[str]:
    """Greedily join items into parts of at most max_chars, each starting with prefix."""
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for item in items:
        if current and size + len(separator) + len(item) > max_chars:
            parts.append(prefix + separator.join(current))
            current, size = [], 0
        current.append(item)
        size += len(item) + len(separator)
    if current:
        parts.append(prefix + separator.join(current))
    return parts
