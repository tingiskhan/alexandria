"""A document's metadata: from its first page, from Crossref when that page has a DOI, and its type from its folder.

Reading the first page is a heuristic on the Markdown Docling writes: the first heading that isn't a section name
("Abstract", "Contents") is the title, a line of names right below it the authors, and a copyright notice or a
date the year. When it finds nothing, the title is the file stem, as before metadata existed. Transcripts skip all
of this: they are of type "transcript" and titled by their header or file stem.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath

from alexandria_core.models import DocType, DocumentMetadata
from alexandria_core.protocols import BibliographyLookup
from alexandria_ingest.crossref import CrossrefLookup, make_crossref_client
from alexandria_ingest.settings import TRANSCRIPT_FORMATS, MetadataSettings, extension
from alexandria_ingest.transcript import transcript_title

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>\[\]]+)")
_CLOSING = {")": "(", "]": "["}

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
_NUMBERING = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+")
_SECTION_NAMES = frozenset(
    {
        "abstract",
        "acknowledgements",
        "acknowledgments",
        "background",
        "contents",
        "executive summary",
        "foreword",
        "intro",
        "introduction",
        "keywords",
        "overview",
        "preface",
        "references",
        "summary",
        "table of contents",
    }
)
_MAX_TITLE = 300

_MATH = re.compile(r"\\\(.*?\\\)|\$[^$]*\$")
_MARK = r"[\d*†‡§¶#\u2217]"  # digits and the symbols papers use for affiliations, also U+2217 (asterisk operator)
_FOOTNOTE_MARKS = re.compile(rf"(?<=[^\W\d_]){_MARK}+(?:,{_MARK}+)*")
_NAME_SEPARATORS = re.compile(r"\s*(?:,|;|&|\band\b)\s*")
_PARTICLES = frozenset({"van", "von", "de", "der", "den", "da", "di", "du", "del", "della", "le", "la", "bin", "al"})
_NOT_NAMES = frozenset(
    {
        "agency",
        "bank",
        "center",
        "centre",
        "college",
        "corporation",
        "department",
        "inc",
        "institute",
        "journal",
        "laboratory",
        "ltd",
        "ministry",
        "report",
        "school",
        "university",
        "volume",
    }
)
_MAX_AUTHOR_LINE = 400

_YEAR = r"(1[89]\d\d|20\d\d)"
_MONTHS = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?"
    r"|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_COPYRIGHT = re.compile(rf"(?:©|\(c\)|copyright)\s*{_YEAR}\b", re.IGNORECASE)
_DATES = (
    re.compile(rf"\b{_MONTHS}\.?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?{_YEAR}\b"),  # March 2020, March 5, 2020
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}\.?,?\s+{_YEAR}\b"),  # 5 March 2020
    re.compile(rf"\b{_YEAR}-[01]\d-[0-3]\d\b"),  # 2020-03-05
)
_ANY_YEAR = re.compile(rf"\b{_YEAR}\b")


@dataclass(frozen=True)
class FirstPage:
    """What the first page says about its document. Anything not found is None or empty."""

    title: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    doi: str | None = None


def read_first_page(markdown: str) -> FirstPage:
    """Find the title, authors, year and DOI on a document's first page (Markdown, as extracted)."""
    lines = [line.strip() for line in markdown.replace("\\_", "_").splitlines()]
    title, after_title = _title(lines)
    authors = _authors(after_title)
    return FirstPage(title=title, authors=authors, year=find_year(markdown, title), doi=find_doi(markdown))


def find_doi(text: str) -> str | None:
    """The first DOI in `text`, without a doi.org prefix or the punctuation that follows it in prose."""
    match = _DOI.search(text.replace("\\_", "_"))
    if match is None:
        return None
    doi = match.group(1).rstrip(".,;:")
    # A closing bracket belongs to the DOI only if it closes one inside it: "(see doi:10.1/x)" vs "10.1/(x)".
    while doi[-1] in _CLOSING and doi.count(doi[-1]) > doi.count(_CLOSING[doi[-1]]):
        doi = doi[:-1].rstrip(".,;:")
    return doi


def find_year(markdown: str, title: str | None = None) -> int | None:
    """The publication year: from a copyright notice, else the latest date on the page, else a year in the title.

    Years after next year are ignored. The latest date wins because a paper's first page often lists when it was
    received, accepted and published, in that order.
    """
    latest = datetime.now(UTC).year + 1
    if (match := _COPYRIGHT.search(markdown)) and int(match.group(1)) <= latest:
        return int(match.group(1))
    dates = [int(m.group(1)) for pattern in _DATES for m in pattern.finditer(markdown)]
    if dates := [year for year in dates if year <= latest]:
        return max(dates)
    if title and (match := _ANY_YEAR.search(title)) and int(match.group(1)) <= latest:
        return int(match.group(1))
    return None


def doc_type_for(path: str, doc_types: Mapping[str, DocType]) -> DocType | None:
    """The type mapped to the deepest folder in `doc_types` ("/a/b", lower-case) that holds `path`, if any."""
    lowered = path.lower()
    folders = [folder for folder in doc_types if lowered.startswith(folder.rstrip("/") + "/")]
    return doc_types[max(folders, key=len)] if folders else None


@dataclass(frozen=True)
class MetadataReader:
    """Works out a document's metadata from its path and first page.

    `doc_types` maps folders ("/papers", lower-case) to the type of the documents below them. `lookup`, if given,
    is asked about a DOI found on the first page; what it knows is preferred over what the page says.
    """

    doc_types: Mapping[str, DocType] = field(default_factory=dict)
    lookup: BibliographyLookup | None = None

    def read(self, path: str, first_page: str) -> DocumentMetadata:
        """The metadata of the document at `path` whose first page is `first_page` (Markdown; "" if it has none).

        A transcript (.vtt, .srt) is always of type "transcript", whatever its folder, and is titled by its header
        or file name. Its first page is speech, so it is neither read for authors and a year nor looked up.
        """
        if extension(path) in TRANSCRIPT_FORMATS:
            return DocumentMetadata(
                title=transcript_title(first_page) or PurePosixPath(path).stem, doc_type="transcript"
            )
        found = read_first_page(first_page)
        registered = self.lookup.lookup(found.doi) if self.lookup is not None and found.doi else None
        if registered is None:
            registered = DocumentMetadata(title="")
        return DocumentMetadata(
            title=registered.title or found.title or PurePosixPath(path).stem,
            authors=registered.authors or found.authors,
            year=registered.year or found.year,
            doi=registered.doi or found.doi,
            doc_type=doc_type_for(path, self.doc_types) or registered.doc_type,
        )


def make_metadata_reader(settings: MetadataSettings) -> MetadataReader:
    """The reader `settings` asks for: with a Crossref lookup unless ALEXANDRIA_METADATA_CROSSREF is off."""
    lookup = CrossrefLookup(make_crossref_client(settings.crossref_mailto)) if settings.crossref else None
    return MetadataReader(doc_types=settings.doc_types, lookup=lookup)


def _clean(text: str) -> str:
    """Drop Markdown emphasis and extra whitespace."""
    return " ".join(re.sub(r"(\*\*|__|\*|_)(.+?)\1", r"\2", text).split())


def _title(lines: list[str]) -> tuple[str | None, list[str]]:
    """The first heading that reads like a title, and the lines after it."""
    for index, line in enumerate(lines):
        if not (match := _HEADING.match(line)):
            continue
        title = _clean(match.group(1))
        name = _NUMBERING.sub("", title).lower().rstrip(":")
        if name in _SECTION_NAMES or not 3 <= len(title) <= _MAX_TITLE or not re.search(r"[^\W\d_]{2}", title):
            continue
        return title, lines[index + 1 :]
    return None, []


def _authors(lines: list[str]) -> tuple[str, ...]:
    """The names in the first paragraph of `lines`, if all of it reads like a list of names."""
    paragraph: list[str] = []
    for line in lines:
        if line.startswith("#") or (not line and paragraph):
            break
        if line:
            paragraph.append(line)
    text = _clean(" ".join(paragraph))
    if not text or len(text) > _MAX_AUTHOR_LINE:
        return ()
    text = _FOOTNOTE_MARKS.sub("", _MATH.sub("", text))
    names = [name.strip(" .,") for name in _NAME_SEPARATORS.split(text)]
    names = [name for name in names if name]
    if not names or not all(_is_name(name) for name in names):
        return ()
    return tuple(names)


def _is_name(text: str) -> bool:
    """Two to five words, each capitalised (or an initial, or a particle like "van"), none an organisation word."""
    words = text.split()
    if not 2 <= len(words) <= 5 or any(char.isdigit() or char == "@" for char in text):
        return False
    for word in words:
        bare = word.strip(".-").lower()
        if bare in _NOT_NAMES:
            return False
        if not (word[0].isupper() or bare in _PARTICLES):
            return False
    return True
