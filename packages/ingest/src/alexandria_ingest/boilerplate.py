"""Remove text that is not part of the document itself before chunking: running headers and front-matter notices.

Pure functions with no I/O. Only the chunker's input is stripped; stored pages keep everything the extractor
returned, so `get_pages` still shows the page as in the file, and a `reindex` applies changed rules to them.
Page numbers never change: a skipped front-matter page stays in the list with empty Markdown.
"""

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from alexandria_core.models import Page

# Matched case-insensitively against each paragraph of a document's first page. Kept to phrases that rarely
# occur in body text, since a page is only skipped when most of it matches.
DEFAULT_FRONT_MATTER_PATTERNS = (
    r"\bterms (and conditions|of use)\b",
    r"\ball rights reserved\b",
    r"\b(this content )?downloaded (from|by|on)\b",
    r"©|\bcopyright\b",
    r"\bfor personal use only\b",
    r"\bjstor\b",
    r"\bcreative commons\b",
    r"\b(may|must) not be (reproduced|copied|distributed|redistributed)\b",
    r"\bunauthori[sz]ed\b",
    r"\bpermissions?\b",
    r"\b(stable url|to cite this article)\b",
    r"https?://",
)

_LETTERS_IN_LINE = 3  # lines with fewer letters (rules, "$$", page numbers) never count as repeated
_PARAGRAPH = re.compile(r"\n\s*\n")


def compile_patterns(patterns: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    """Compile regexes case-insensitively. Raises re.error on an invalid one."""
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


@dataclass(frozen=True)
class BoilerplateRules:
    """What `strip_boilerplate` removes. See BoilerplateSettings for how each is configured.

    Attributes:
        patterns: Lines matching any of these (`re.search`) are dropped from every page.
        repeat_ratio: A line is a running header when it appears on at least this fraction of the pages.
        repeat_min_pages: Repeated lines are only looked for in documents with at least this many pages.
        front_matter_patterns: The first page is skipped when paragraphs matching these make up most of its text.
        front_matter_ratio: The share of the first page's text that must match for it to be skipped.
    """

    patterns: tuple[re.Pattern[str], ...] = ()
    repeat_ratio: float = 0.6
    repeat_min_pages: int = 4
    front_matter_patterns: tuple[re.Pattern[str], ...] = compile_patterns(DEFAULT_FRONT_MATTER_PATTERNS)
    front_matter_ratio: float = 0.5


def strip_boilerplate(pages: Sequence[Page], rules: BoilerplateRules) -> list[Page]:
    """Drop lines matching `rules.patterns`, lines repeated on most pages, and a front-matter first page.

    Returns one page per input page with the same numbers, so chunks still cite the right pages.
    """
    repeated = _repeated_lines(pages, rules)
    stripped = [
        Page(number=page.number, markdown=_strip_lines(page.markdown, rules.patterns, repeated)) for page in pages
    ]
    if len(pages) > 1 and _is_front_matter(pages[0].markdown, rules):
        stripped[0] = Page(number=pages[0].number, markdown="")
    return stripped


def _key(line: str) -> str | None:
    """How a line is compared across pages, or None if it can't be a running header.

    Heading markers, case, spacing and digits are ignored, so "Journal of X, Vol. 3, p. 12" on one page matches
    "journal of x, vol. 3, p. 13" on the next. Table rows and lines with hardly any letters are never keys:
    a table's `| --- |` separator or a `$$` math delimiter repeats on many pages but is not boilerplate.
    """
    text = line.strip().lstrip("#").strip()
    if text.startswith("|") or sum(c.isalpha() for c in text) < _LETTERS_IN_LINE:
        return None
    return re.sub(r"\s+", " ", re.sub(r"\d+", "0", text)).casefold()


def _repeated_lines(pages: Sequence[Page], rules: BoilerplateRules) -> frozenset[str]:
    """Keys of lines that appear on at least `repeat_ratio` of the non-empty pages."""
    texts = [page.markdown for page in pages if page.markdown.strip()]
    if len(texts) < max(rules.repeat_min_pages, 2):
        return frozenset()
    counts = Counter(key for text in texts for key in {_key(line) for line in text.splitlines()} if key is not None)
    threshold = max(2, math.ceil(rules.repeat_ratio * len(texts)))
    return frozenset(key for key, count in counts.items() if count >= threshold)


def _strip_lines(markdown: str, patterns: Sequence[re.Pattern[str]], repeated: frozenset[str]) -> str:
    """The page without lines that match a pattern or are repeated."""
    if not patterns and not repeated:
        return markdown
    return "\n".join(
        line
        for line in markdown.splitlines()
        if _key(line) not in repeated and not any(p.search(line) for p in patterns)
    )


def _is_front_matter(markdown: str, rules: BoilerplateRules) -> bool:
    """Whether paragraphs matching a front-matter pattern make up at least `front_matter_ratio` of the page's text."""
    paragraphs = [p.strip() for p in _PARAGRAPH.split(markdown) if p.strip()]
    total = sum(len(p) for p in paragraphs)
    if not total or not rules.front_matter_patterns:
        return False
    matched = sum(len(p) for p in paragraphs if any(r.search(p) for r in rules.front_matter_patterns))
    return matched / total >= rules.front_matter_ratio
