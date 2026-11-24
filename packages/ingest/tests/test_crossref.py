from collections.abc import Callable
from typing import Any

import httpx
import pytest

from alexandria_core.models import DocumentMetadata
from alexandria_ingest.crossref import CROSSREF_URL, CrossrefLookup, make_crossref_client, parse_work

WORK: dict[str, Any] = {
    "DOI": "10.1000/XYZ",
    "type": "journal-article",
    "title": ["Widgets and <i>gadgets</i> &amp; more"],
    "subtitle": ["a survey"],
    "author": [{"given": "Ada", "family": "Lovelace"}, {"family": "Babbage"}, {"name": "The Widget Consortium"}, {}],
    "issued": {"date-parts": [[2021, 3, 1]]},
}


def lookup_with(handler: Callable[[httpx.Request], httpx.Response]) -> CrossrefLookup:
    return CrossrefLookup(httpx.Client(transport=httpx.MockTransport(handler)))


def test_lookup_reads_the_work() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "message": WORK})

    metadata = lookup_with(handler).lookup("10.1000/xyz")

    assert str(requests[0].url) == CROSSREF_URL + "10.1000/xyz"
    assert metadata == DocumentMetadata(
        title="Widgets and gadgets & more: a survey",
        authors=("Ada Lovelace", "Babbage", "The Widget Consortium"),
        year=2021,
        doi="10.1000/XYZ",
        doc_type="paper",
    )


@pytest.mark.parametrize(
    "respond",
    [
        lambda: httpx.Response(404, text="Resource not found."),
        lambda: httpx.Response(503),
        lambda: httpx.Response(200, text="not json"),
        lambda: httpx.Response(200, json={"status": "ok"}),
    ],
)
def test_lookup_failures_return_none(respond: Callable[[], httpx.Response]) -> None:
    assert lookup_with(lambda _request: respond()).lookup("10.1000/xyz") is None


def test_lookup_returns_none_when_crossref_cannot_be_reached() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    assert lookup_with(handler).lookup("10.1000/xyz") is None


def test_a_work_without_a_title_is_no_answer() -> None:
    assert parse_work({"title": []}, "10.1/x") is None


@pytest.mark.parametrize(
    ("work", "expected"),
    [
        ({"type": "book", "published": {"date-parts": [[1999]]}}, ("book", 1999)),
        ({"type": "dataset", "issued": {"date-parts": [[None]]}}, ("other", None)),
        ({"published-online": {"date-parts": [[2005, 1]]}}, ("other", 2005)),
    ],
)
def test_parse_work_type_and_year(work: dict[str, Any], expected: tuple[str, int | None]) -> None:
    metadata = parse_work({"title": ["T"], **work}, "10.1/x")

    assert metadata is not None
    assert (metadata.doc_type, metadata.year, metadata.doi, metadata.authors) == (*expected, "10.1/x", ())


def test_client_identifies_itself_and_the_contact() -> None:
    assert "mailto:ops@example.org" in make_crossref_client("ops@example.org").headers["User-Agent"]
    assert "mailto" not in make_crossref_client().headers["User-Agent"]
