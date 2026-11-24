"""`CrossrefLookup`: a BibliographyLookup on Crossref's public REST API (https://api.crossref.org)."""

import html
import logging
import re
from importlib.metadata import version
from typing import Any
from urllib.parse import quote

import httpx

from alexandria_core.models import DocType, DocumentMetadata

log = logging.getLogger(__name__)

CROSSREF_URL = "https://api.crossref.org/works/"
TIMEOUT_SECONDS = 10.0

# Crossref's work types (https://api.crossref.org/types) that say what kind of document it is.
_DOC_TYPES: dict[str, DocType] = {
    "journal-article": "paper",
    "proceedings-article": "paper",
    "posted-content": "paper",  # preprints
    "book": "book",
    "edited-book": "book",
    "monograph": "book",
    "reference-book": "book",
}
_DATE_FIELDS = ("issued", "published", "published-print", "published-online")


class CrossrefLookup:
    """Looks DOIs up on Crossref. Failures are logged and return None, so they never stop an ingest."""

    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def lookup(self, doi: str) -> DocumentMetadata | None:
        """What Crossref has for `doi`, or None when it doesn't know it, can't be reached, or has no title for it."""
        try:
            response = self._http.get(CROSSREF_URL + quote(doi, safe="/()"))
        except httpx.HTTPError as error:
            log.warning("Crossref lookup of %s failed: %s", doi, error)
            return None
        if response.status_code == httpx.codes.NOT_FOUND:
            log.info("Crossref doesn't know %s", doi)
            return None
        if response.is_error:
            log.warning("Crossref lookup of %s failed: HTTP %d", doi, response.status_code)
            return None
        try:
            return parse_work(response.json()["message"], doi)
        except (ValueError, KeyError, TypeError, IndexError) as error:
            log.warning("Crossref sent an unexpected answer for %s: %r", doi, error)
            return None


def make_crossref_client(mailto: str | None = None) -> httpx.Client:
    """An HTTP client for Crossref. With `mailto`, requests go to its faster "polite" pool."""
    contact = f"; mailto:{mailto}" if mailto else ""
    user_agent = f"alexandria/{version('alexandria-ingest')} (https://github.com/tingiskhan/alexandria{contact})"
    return httpx.Client(timeout=TIMEOUT_SECONDS, headers={"User-Agent": user_agent}, follow_redirects=True)


def parse_work(work: dict[str, Any], doi: str) -> DocumentMetadata | None:
    """Turn a Crossref work (the `message` of /works/{doi}) into metadata; None if it has no title."""
    title = _text(work.get("title"))
    if not title:
        return None
    if subtitle := _text(work.get("subtitle")):
        title = f"{title}: {subtitle}"
    authors = tuple(
        name
        for author in work.get("author") or []
        if (
            name := " ".join(part for part in (author.get("given"), author.get("family")) if part) or author.get("name")
        )
    )
    return DocumentMetadata(
        title=title,
        authors=authors,
        year=_year(work),
        doi=work.get("DOI") or doi,
        doc_type=_DOC_TYPES.get(work.get("type", ""), "other"),
    )


def _text(values: object) -> str:
    """The first of Crossref's list of strings, without the JATS/HTML tags and entities it may contain."""
    if not isinstance(values, list) or not values:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", str(values[0]))).split())


def _year(work: dict[str, Any]) -> int | None:
    """The year from the first of Crossref's dates that has one."""
    for date_field in _DATE_FIELDS:
        parts = (work.get(date_field) or {}).get("date-parts") or [[]]
        if parts[0] and parts[0][0] is not None:
            return int(parts[0][0])
    return None
