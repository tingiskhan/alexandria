r"""The one maths convention of stored Markdown: inline `\(...\)`, display `$$...$$`, no Markdown escapes inside.

Models write maths inside plain text, so Docling escapes it as prose (`\( x\_{n} \)`), and they delimit display
formulas themselves, inside the `$$` Docling adds (`$$\[ x \]$$`). `\(` is kept for inline maths because `$` also
occurs in prose. All of this works on exported Markdown; prose and code are left alone.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

_NOT_A_NEW_PARAGRAPH = r"(?:(?!\n\s*\n).)"
_MATHS = re.compile(
    r"```.*?```|`[^`\n]*`"  # code: matched first, so that nothing inside it counts as maths
    rf"|\$\$(?P<display>{_NOT_A_NEW_PARAGRAPH}+?)\$\$"
    rf"|\\\((?P<inline>{_NOT_A_NEW_PARAGRAPH}+?)\\\)"
    # Pandoc's rule, which tells maths from prices: no space inside the delimiters, no digit after the closing one.
    r"|(?<!\\)\$(?=\S)(?P<dollar>[^$\n]+?)(?<=\S)\$(?!\d)",
    re.DOTALL,
)
_LATEX_MARKER = re.compile(r"[\\_^{]")  # what `$...$` needs to count as maths: "$10, or US$ 3" fits Pandoc's rule too
_ESCAPED_UNDERSCORE = re.compile(r"(?<!\\)\\_")
_BRACKETS = re.compile(r"\s*\\\[(.*)\\\]\s*", re.DOTALL)
_TABLE_ROW = re.compile(r"^\|.*\|[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Formula:
    """One maths span of a page, without its delimiters."""

    latex: str
    display: bool


def _normalize_span(match: re.Match[str]) -> str:
    if (display := match["display"]) is not None:
        brackets = _BRACKETS.fullmatch(display)
        return f"$${brackets[1] if brackets else display}$$"
    inline = match["inline"] if match["inline"] is not None else match["dollar"]
    if inline is None or (match["dollar"] is not None and not _LATEX_MARKER.search(inline)):
        return match[0]
    return rf"\({_ESCAPED_UNDERSCORE.sub('_', inline)}\)"


def _readable_pipes(row: re.Match[str]) -> str:
    return row[0].replace("&#124;", r"\|")  # Docling's escape for a pipe in a cell; `\|` is the Markdown one


def normalize_math(markdown: str) -> str:
    """Bring Markdown to the convention above. Normalized text comes back unchanged."""
    return _TABLE_ROW.sub(_readable_pipes, _MATHS.sub(_normalize_span, markdown))


def formulas(markdown: str) -> Iterator[Formula]:
    """The maths spans of normalized Markdown."""
    for match in _MATHS.finditer(markdown):
        if match["display"] is not None:
            yield Formula(match["display"], display=True)
        elif match["inline"] is not None:
            yield Formula(match["inline"], display=False)
