"""SharePointSource against a fake Graph, and GraphHttpClient against httpx.MockTransport.

Delta entries follow Graph's documented driveItem shape for SharePoint and OneDrive for Business: no
`parentReference.path`, a `deleted` facet on removed items, and folder renames reported without their contents.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from pydantic import ValidationError

from alexandria_core.models import FileChange
from alexandria_ingest.sources.sharepoint import (
    GraphError,
    GraphHttpClient,
    ResyncRequiredError,
    SharePointSettings,
    SharePointSource,
)
from alexandria_ingest.sources.sharepoint import client as graph_client

ROOT = {"id": "root", "name": "root", "root": {}, "folder": {"childCount": 1}}


def folder(item_id: str, name: str, parent: str = "root") -> dict[str, Any]:
    return {"id": item_id, "name": name, "folder": {"childCount": 0}, "parentReference": {"id": parent}}


def file(item_id: str, name: str, parent: str = "root", content_hash: str = "q1") -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "file": {"mimeType": "application/pdf", "hashes": {"quickXorHash": content_hash}},
        "cTag": f"ctag-{content_hash}",
        "parentReference": {"id": parent},
    }


def deleted(item_id: str, parent: str = "root") -> dict[str, Any]:
    return {"id": item_id, "deleted": {"state": "deleted"}, "parentReference": {"id": parent}}


def change(path: str, content_hash: str = "q1") -> FileChange:
    return FileChange(source_id=path.lower(), path=path, content_hash=content_hash)


def deletion(path: str) -> FileChange:
    return FileChange(source_id=path.lower(), path=path, content_hash=None, deleted=True)


@dataclass
class FakeGraph:
    """Serves delta pages: `pages[link]` for a link, `pages[None]` for a new delta. Links in `expired` raise 410."""

    pages: dict[str | None, dict[str, Any]] = field(default_factory=dict)
    expired: set[str] = field(default_factory=set)
    links: list[str | None] = field(default_factory=list)

    def delta_page(self, link: str | None) -> dict[str, Any]:
        self.links.append(link)
        if link in self.expired:
            raise ResyncRequiredError("HTTP 410 resyncRequired: the delta token has expired")
        return self.pages[link]

    def download(self, path: str) -> bytes:
        return f"bytes of {path}".encode()


def page(*entries: dict[str, Any], next_link: str | None = None, delta: str = "d1") -> dict[str, Any]:
    links = {"@odata.nextLink": next_link} if next_link else {"@odata.deltaLink": delta}
    return {"value": list(entries), **links}


def first_run(graph: FakeGraph, path: str = "") -> tuple[SharePointSource, list[FileChange], str]:
    source = SharePointSource(graph, path)
    changes, cursor = source.changes(None)
    return source, changes, cursor


def test_first_run_resolves_nested_paths_whatever_the_order() -> None:
    # The file comes before its folders, and the folders before the root; paths are resolved after all pages.
    graph = FakeGraph(
        pages={
            None: page(file("f1", "Q3 Report.pdf", parent="sub"), next_link="p2"),
            "p2": page(folder("sub", "Reports", parent="top"), folder("top", "Finance"), ROOT, file("f2", "a.docx")),
        }
    )

    _, changes, cursor = first_run(graph)

    assert changes == [change("/Finance/Reports/Q3 Report.pdf"), change("/a.docx")]
    assert graph.links == [None, "p2"]
    assert json.loads(cursor)["delta"] == "d1"


def test_only_files_in_the_folder_are_reported() -> None:
    graph = FakeGraph(
        pages={
            None: page(
                ROOT,
                folder("fin", "Finance"),
                folder("hr", "HR"),
                file("f1", "a.pdf", parent="fin"),
                file("f2", "b.pdf", parent="hr"),
                file("f3", "c.pdf"),
            )
        }
    )

    _, changes, _ = first_run(graph, path="/finance/")

    assert changes == [change("/Finance/a.pdf")]


def test_packages_and_their_contents_are_skipped() -> None:
    notebook = {"id": "nb", "name": "Notes", "package": {"type": "oneNote"}, "parentReference": {"id": "root"}}
    graph = FakeGraph(pages={None: page(ROOT, notebook, file("s1", "Section.one", parent="nb"), file("f1", "a.pdf"))})

    _, changes, _ = first_run(graph)

    assert changes == [change("/a.pdf")]


def test_ctag_is_the_hash_when_there_is_no_quickxorhash() -> None:
    entry = file("f1", "a.pdf")
    del entry["file"]["hashes"]
    graph = FakeGraph(pages={None: page(ROOT, entry)})

    _, changes, _ = first_run(graph)

    assert changes == [change("/a.pdf", "ctag-q1")]


def incremental(*entries: dict[str, Any], initial: list[dict[str, Any]], path: str = "") -> list[FileChange]:
    """Run once over `initial`, then again with `entries` as the delta, and return the second run's changes."""
    graph = FakeGraph(pages={None: page(ROOT, *initial), "d1": page(*entries, delta="d2")})
    source, _, cursor = first_run(graph, path)
    changes, next_cursor = source.changes(cursor)
    assert graph.links[-1] == "d1"
    assert json.loads(next_cursor)["delta"] == "d2"
    return changes


def test_an_incremental_run_reports_only_what_changed() -> None:
    changes = incremental(
        file("f1", "a.pdf", content_hash="q2"),
        file("f2", "b.pdf"),  # reported again by Graph, but unchanged
        initial=[file("f1", "a.pdf"), file("f2", "b.pdf")],
    )

    assert changes == [change("/a.pdf", "q2")]


def test_a_renamed_file_is_deleted_at_its_old_path_and_ingested_at_the_new_one() -> None:
    changes = incremental(file("f1", "b.pdf"), initial=[file("f1", "a.pdf")])

    assert changes == [deletion("/a.pdf"), change("/b.pdf")]


def test_a_file_replaced_at_the_same_path_is_deleted_before_it_is_ingested() -> None:
    changes = incremental(deleted("f1"), file("f2", "a.pdf", content_hash="q2"), initial=[file("f1", "a.pdf")])

    assert changes == [deletion("/a.pdf"), change("/a.pdf", "q2")]


def test_renaming_a_folder_moves_the_files_in_it_although_graph_reports_only_the_folder() -> None:
    changes = incremental(
        folder("fin", "Money"),
        initial=[folder("fin", "Finance"), file("f1", "a.pdf", parent="fin"), file("f2", "b.pdf", parent="fin")],
    )

    assert changes == [
        deletion("/Finance/a.pdf"),
        deletion("/Finance/b.pdf"),
        change("/Money/a.pdf"),
        change("/Money/b.pdf"),
    ]


def test_deleting_a_folder_deletes_the_files_in_it_and_forgets_them() -> None:
    graph = FakeGraph(
        pages={
            None: page(
                ROOT, folder("fin", "Finance"), folder("q", "Q3", parent="fin"), file("f1", "a.pdf", parent="q")
            ),
            "d1": page(deleted("fin"), delta="d2"),
        }
    )
    source, _, cursor = first_run(graph)

    changes, next_cursor = source.changes(cursor)

    assert changes == [deletion("/Finance/Q3/a.pdf")]
    assert set(json.loads(next_cursor)["items"]) == {"root"}


def test_moving_a_file_out_of_the_folder_deletes_it() -> None:
    changes = incremental(
        file("f1", "a.pdf", parent="hr"),
        initial=[folder("fin", "Finance"), folder("hr", "HR"), file("f1", "a.pdf", parent="fin")],
        path="/Finance",
    )

    assert changes == [deletion("/Finance/a.pdf")]


def test_an_expired_delta_link_lists_the_drive_again_and_diffs_against_the_cursor() -> None:
    graph = FakeGraph(pages={None: page(ROOT, file("f1", "a.pdf"), file("f2", "b.pdf"))})
    source, _, cursor = first_run(graph)
    graph.expired.add("d1")
    graph.pages[None] = page(ROOT, file("f1", "a.pdf"), file("f3", "c.pdf"), delta="d9")

    changes, next_cursor = source.changes(cursor)

    assert graph.links[-2:] == ["d1", None]
    assert changes == [deletion("/b.pdf"), change("/c.pdf")]  # a.pdf is unchanged, so not reported
    assert json.loads(next_cursor)["delta"] == "d9"


def test_download_passes_the_path() -> None:
    source = SharePointSource(FakeGraph(), "")

    assert source.download("/finance/q3 report.pdf") == b"bytes of /finance/q3 report.pdf"


# --- Settings -----------------------------------------------------------------------------------------------


def settings(**overrides: Any) -> SharePointSettings:
    values: dict[str, Any] = {
        "tenant_id": "tenant",
        "client_id": "app",
        "client_secret": "secret",
        "site": "contoso.sharepoint.com:/sites/Finance Team",
    }
    return SharePointSettings(**{**values, **overrides})


def test_a_site_or_a_drive_id_is_required() -> None:
    with pytest.raises(ValidationError, match="ALEXANDRIA_SHAREPOINT_SITE or ALEXANDRIA_SHAREPOINT_DRIVE_ID"):
        settings(site=None)

    assert settings(site=None, drive_id="b!abc").drive_id == "b!abc"


# --- GraphHttpClient ----------------------------------------------------------------------------------------


@dataclass
class FakeCredential:
    """Hands out numbered tokens; azure-identity does the caching, so every call here is a fresh token."""

    reject: bool = False
    scopes: list[tuple[str, ...]] = field(default_factory=list)

    def get_token(self, *scopes: str) -> AccessToken:
        if self.reject:
            raise ClientAuthenticationError(message="AADSTS7000215: Invalid client secret provided.")
        self.scopes.append(scopes)
        return AccessToken(f"token-{len(self.scopes)}", 0)


@dataclass
class FakeGraphServer:
    """Answers Graph requests from `routes`: a path (with its query) mapped to responses, served in order."""

    routes: dict[str, list[httpx.Response]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.routes[request.url.raw_path.decode().removeprefix("/v1.0")].pop(0)


def client(server: FakeGraphServer, credential: FakeCredential | None = None, **overrides: Any) -> GraphHttpClient:
    http = httpx.Client(transport=httpx.MockTransport(server.handle))
    return GraphHttpClient(http, credential or FakeCredential(), settings(**overrides))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(graph_client.time, "sleep", slept.append)
    return slept


SITE = "/sites/contoso.sharepoint.com:/sites/Finance%20Team"
DRIVES = "/sites/site-1/drives?$select=id,name"
DELTA = "/drives/drive-1/root/delta?$select=id,name,parentReference,file,folder,root,deleted,package,cTag"


def site_routes() -> dict[str, list[httpx.Response]]:
    drives = {"value": [{"id": "drive-2", "name": "Site Assets"}, {"id": "drive-1", "name": "Documents"}]}
    return {SITE: [httpx.Response(200, json={"id": "site-1"})], DRIVES: [httpx.Response(200, json=drives)]}


def test_libraries_are_listed_by_name() -> None:
    assert client(FakeGraphServer(routes=site_routes())).libraries() == [
        ("Documents", "drive-1"),
        ("Site Assets", "drive-2"),
    ]


def test_the_library_is_found_by_name_once_and_used_for_the_delta() -> None:
    server = FakeGraphServer(routes={**site_routes(), DELTA: [httpx.Response(200, json=page(ROOT))] * 2})
    graph = client(server, library="documents")

    assert graph.delta_page(None) == page(ROOT)
    assert graph.delta_page(None) == page(ROOT)
    assert len(server.requests) == 4  # site and drives looked up once


def test_an_unknown_library_names_the_ones_there_are() -> None:
    with pytest.raises(GraphError, match=r"No library 'Docs' in .*; it has: Documents, Site Assets"):
        client(FakeGraphServer(routes=site_routes()), library="Docs").drive_id()


def test_libraries_need_a_site() -> None:
    with pytest.raises(GraphError, match="set ALEXANDRIA_SHAREPOINT_SITE"):
        client(FakeGraphServer(), site=None, drive_id="drive-1").libraries()


def test_each_request_carries_a_token_for_graph() -> None:
    credential = FakeCredential()
    server = FakeGraphServer(routes={DELTA: [httpx.Response(200, json=page(ROOT))] * 2})
    graph = client(server, credential, drive_id="drive-1")

    graph.delta_page(None)
    graph.delta_page(None)

    assert credential.scopes == [("https://graph.microsoft.com/.default",)] * 2
    assert [r.headers["Authorization"] for r in server.requests] == ["Bearer token-1", "Bearer token-2"]


def test_a_rejected_sign_in_is_a_graph_error() -> None:
    with pytest.raises(GraphError, match="Sign-in failed: AADSTS7000215: Invalid client secret"):
        client(FakeGraphServer(), FakeCredential(reject=True), drive_id="drive-1").delta_page(None)


def test_throttled_requests_are_retried_after_the_delay_graph_asks_for(no_sleep: list[float]) -> None:
    throttled = httpx.Response(429, headers={"Retry-After": "7"})
    unavailable = httpx.Response(503)  # no Retry-After: back off exponentially
    server = FakeGraphServer(routes={DELTA: [throttled, unavailable, httpx.Response(200, json=page(ROOT))]})

    assert client(server, drive_id="drive-1").delta_page(None) == page(ROOT)
    assert no_sleep == [7.0, 4.0]


def test_throttling_gives_up_after_a_few_attempts(no_sleep: list[float]) -> None:
    error = {"error": {"code": "activityLimitReached", "message": "Slow down."}}
    server = FakeGraphServer(routes={DELTA: [httpx.Response(429, json=error) for _ in range(5)]})

    with pytest.raises(GraphError, match=r"HTTP 429 activityLimitReached: Slow down\."):
        client(server, drive_id="drive-1").delta_page(None)
    assert len(no_sleep) == 4


def test_an_expired_delta_link_raises_resync_required() -> None:
    error = {"error": {"code": "resyncRequired", "message": "Resync required."}}
    server = FakeGraphServer(routes={"/old": [httpx.Response(410, json=error)]})

    with pytest.raises(ResyncRequiredError, match="resyncRequired"):
        client(server, drive_id="drive-1").delta_page("https://graph.microsoft.com/v1.0/old")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(403, json={"error": {"code": "accessDenied", "message": "No."}}), "HTTP 403 accessDenied: No."),
        (httpx.Response(500, text="<html>oops</html>"), "HTTP 500"),
        (httpx.Response(404, json=["unexpected"]), "HTTP 404"),
    ],
)
def test_errors_carry_graphs_code_and_message(response: httpx.Response, message: str) -> None:
    with pytest.raises(GraphError, match=f"^{message}$"):
        client(FakeGraphServer(routes={DELTA: [response]}), drive_id="drive-1").delta_page(None)


def test_download_quotes_the_path_and_follows_the_redirect_without_the_token() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "download.example":
            assert "Authorization" not in request.headers  # httpx drops it on the cross-origin redirect
            return httpx.Response(200, content=b"%PDF")
        assert request.url.raw_path == b"/v1.0/drives/drive-1/root:/q3%20report%20%2350%25.pdf:/content"
        return httpx.Response(302, headers={"Location": "https://download.example/abc"})

    http = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)
    graph = GraphHttpClient(http, FakeCredential(), settings(drive_id="drive-1"))

    assert graph.download("/q3 report #50%.pdf") == b"%PDF"


def test_the_real_client_signs_in_with_the_app_secret_and_follows_redirects() -> None:
    graph = graph_client.make_sharepoint_client(settings())

    assert isinstance(graph._credential, graph_client.ClientSecretCredential)
    assert graph._http.follow_redirects
