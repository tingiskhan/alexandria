"""GraphClient over httpx, signed in as an Entra ID app through azure-identity.

The Graph SDK isn't used: it is async-only and very large, and the source makes three kinds of request.
"""

import logging
import time
from typing import Any
from urllib.parse import quote

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import ClientSecretCredential

from alexandria_ingest.sources.sharepoint.errors import GraphError, ResyncRequiredError
from alexandria_ingest.sources.sharepoint.protocols import Credential
from alexandria_ingest.sources.sharepoint.settings import SharePointSettings

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"  # the app's granted Graph permissions
_DELTA_SELECT = "id,name,parentReference,file,folder,root,deleted,package,cTag"
_RETRY_STATUSES = {429, 503, 504}  # throttled or briefly unavailable; Graph sends Retry-After
_MAX_ATTEMPTS = 5


class GraphHttpClient:
    """GraphClient over httpx, with tokens from `credential`. The drive is looked up on first use."""

    def __init__(self, http: httpx.Client, credential: Credential, settings: SharePointSettings) -> None:
        self._http = http
        self._credential = credential
        self._settings = settings
        self._drive_id = settings.drive_id

    def delta_page(self, link: str | None) -> dict[str, Any]:
        """One page of the drive's delta feed: from the start when `link` is None, else from a next or delta link."""
        url = link or f"{GRAPH_URL}/drives/{self.drive_id()}/root/delta?$select={_DELTA_SELECT}"
        page: dict[str, Any] = self._get(url).json()
        return page

    def download(self, path: str) -> bytes:
        """The content of the file at `path`. Graph redirects to a pre-authenticated download URL."""
        return self._get(f"{GRAPH_URL}/drives/{self.drive_id()}/root:{quote(path)}:/content").content

    def drive_id(self) -> str:
        """The configured drive id, or that of the library named `library` in `site`."""
        if self._drive_id is None:
            libraries = self.libraries()
            wanted = self._settings.library.casefold()
            matches = [drive_id for name, drive_id in libraries if name.casefold() == wanted]
            if not matches:
                names = ", ".join(name for name, _ in libraries) or "none"
                raise GraphError(f"No library {self._settings.library!r} in {self._settings.site}; it has: {names}")
            self._drive_id = matches[0]
        return self._drive_id

    def libraries(self) -> list[tuple[str, str]]:
        """The document libraries of `site`, as (name, drive id), sorted by name."""
        if self._settings.site is None:
            raise GraphError("set ALEXANDRIA_SHAREPOINT_SITE to list its libraries")
        site = self._get(f"{GRAPH_URL}/sites/{quote(self._settings.site, safe=':/')}").json()
        drives = self._get(f"{GRAPH_URL}/sites/{site['id']}/drives?$select=id,name").json()
        return sorted((drive["name"], drive["id"]) for drive in drives["value"])

    def _get(self, url: str) -> httpx.Response:
        """GET with the access token, retrying throttled requests after the delay Graph asks for."""
        attempt = 1
        while True:
            response = self._http.get(url, headers={"Authorization": f"Bearer {self._access_token()}"})
            if response.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                delay = _retry_after(response, default=2.0**attempt)
                log.warning("Graph answered %d; retrying in %.0f s", response.status_code, delay)
                time.sleep(delay)
                attempt += 1
                continue
            if response.status_code == httpx.codes.GONE:
                raise ResyncRequiredError(_error_message(response))
            if response.is_error:
                raise GraphError(_error_message(response))
            return response

    def _access_token(self) -> str:
        """An app-only access token for Graph. A rejected sign-in becomes a GraphError, like a failed request."""
        try:
            return self._credential.get_token(GRAPH_SCOPE).token
        except ClientAuthenticationError as error:
            raise GraphError(f"Sign-in failed: {error.message}") from error


def _retry_after(response: httpx.Response, *, default: float) -> float:
    """The delay in Retry-After (Graph sends seconds), or `default` when it's missing or not a number."""
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return default


def _error_message(response: httpx.Response) -> str:
    """The status with Graph's error code and message (`{"error": {"code", "message"}}`) when it sent them."""
    status = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return status
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"{status} {error.get('code')}: {error.get('message')}"
    return status


def make_sharepoint_client(settings: SharePointSettings) -> GraphHttpClient:
    """A Graph client for the configured app and drive. Nothing is requested until it is first used."""
    credential = ClientSecretCredential(
        settings.tenant_id, settings.client_id, settings.client_secret.get_secret_value()
    )
    return GraphHttpClient(httpx.Client(timeout=60, follow_redirects=True), credential, settings)
