"""FileSource over a SharePoint document library or a OneDrive for Business drive, via Microsoft Graph.

Signs in as an Entra ID app with a client secret (client credentials, through azure-identity), so syncs run
unattended. The README describes the app registration and how an admin grants it read access to a site.
"""

import json
import logging
import time
from dataclasses import astuple, dataclass
from typing import Any, Protocol, Self
from urllib.parse import quote

import httpx
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import ClientSecretCredential
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings

from alexandria_core.models import FileChange
from alexandria_core.settings import settings_config

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"  # the app's granted Graph permissions
_DELTA_SELECT = "id,name,parentReference,file,folder,root,deleted,package,cTag"
_RETRY_STATUSES = {429, 503, 504}  # throttled or briefly unavailable; Graph sends Retry-After
_MAX_ATTEMPTS = 5


class SharePointSettings(BaseSettings):
    """Entra ID app and document library (ALEXANDRIA_SHAREPOINT_*)."""

    model_config = settings_config("ALEXANDRIA_SHAREPOINT_")

    tenant_id: str
    client_id: str
    client_secret: SecretStr
    site: str | None = None  # e.g. contoso.sharepoint.com:/sites/Finance
    library: str = "Documents"  # the document library in `site` to sync
    drive_id: str | None = None  # instead of site and library, e.g. for a OneDrive for Business drive
    path: str = ""  # folder to sync, starting with "/"; "" is the whole library

    @model_validator(mode="after")
    def _drive_is_given(self) -> Self:
        """Either a site (with its library) or a drive id says which drive to sync."""
        if self.site is None and self.drive_id is None:
            raise ValueError("set ALEXANDRIA_SHAREPOINT_SITE or ALEXANDRIA_SHAREPOINT_DRIVE_ID")
        return self


class GraphError(Exception):
    """A Graph or sign-in request failed. The message carries Graph's error code and text."""


class ResyncRequiredError(GraphError):
    """The delta link has expired (HTTP 410), so the drive has to be listed again from the start."""


class Credential(Protocol):
    """Where access tokens come from: azure-identity's credentials, or a fake in tests."""

    def get_token(self, *scopes: str) -> AccessToken:
        """An access token for `scopes`. azure-identity caches it and renews it shortly before it expires."""
        ...


class GraphClient(Protocol):
    """The Graph calls this module uses, so tests can pass a fake."""

    def delta_page(self, link: str | None) -> dict[str, Any]:
        """One page of the drive's delta feed: from the start when `link` is None, else from a next or delta link."""
        ...

    def download(self, path: str) -> bytes:
        """The content of the file at `path` (from the drive root, starting with "/")."""
        ...


@dataclass(frozen=True)
class _Item:
    """What the cursor remembers about a drive item, to rebuild paths the delta feed doesn't carry."""

    parent: str | None  # None for the drive root
    name: str
    folder: bool
    hash: str  # files only


class SharePointSource:
    """FileSource over a drive. Files are identified by lower-cased path, like Dropbox's path_lower.

    In SharePoint and OneDrive for Business, the delta feed has no item paths, and renaming or deleting a
    folder doesn't report the items in it. So the cursor keeps every item's parent and name by id, as JSON
    (`{"delta": link, "items": {id: [parent, name, folder, hash]}}`), and each run diffs the paths and hashes
    before and after applying the delta. A moved or renamed file comes out as a deletion of its old path
    and a change at the new one.
    """

    def __init__(self, client: GraphClient, path: str) -> None:
        self._client = client
        self._scope = path.rstrip("/").lower()

    def changes(self, cursor: str | None) -> tuple[list[FileChange], str]:
        """Files added, changed, moved or deleted since `cursor` (every file when it is None)."""
        state: dict[str, Any] = json.loads(cursor) if cursor else {"delta": None, "items": {}}
        previous = {item_id: _Item(*fields) for item_id, fields in state["items"].items()}
        try:
            link, current = self._apply_delta(state["delta"], dict(previous))
        except ResyncRequiredError:
            # Graph expired the delta link: list the drive again. The diff below still finds what changed.
            log.warning("SharePoint delta link expired; listing the whole drive again")
            link, current = self._apply_delta(None, {})

        old_paths, new_paths = _file_paths(previous, self._scope), _file_paths(current, self._scope)
        deletions = [
            FileChange(source_id=path.lower(), path=path, content_hash=None, deleted=True)
            for item_id, path in sorted(old_paths.items(), key=lambda entry: entry[1])
            if new_paths.get(item_id) != path
        ]
        # Deletions go first, so a file replaced at the same path is deleted before it is ingested again.
        updates = [
            FileChange(source_id=path.lower(), path=path, content_hash=current[item_id].hash)
            for item_id, path in sorted(new_paths.items(), key=lambda entry: entry[1])
            if old_paths.get(item_id) != path or previous[item_id].hash != current[item_id].hash
        ]
        # Items cut off from the root (inside a deleted folder) can never resolve again, so they are dropped.
        reachable = _paths(current)
        items = {item_id: list(astuple(item)) for item_id, item in current.items() if reachable[item_id] is not None}
        return [*deletions, *updates], json.dumps({"delta": link, "items": items}, sort_keys=True)

    def download(self, file_id: str) -> bytes:
        """The file's bytes; `file_id` is its lower-cased path, which Graph matches case-insensitively."""
        return self._client.download(file_id)

    def _apply_delta(self, link: str | None, items: dict[str, _Item]) -> tuple[str, dict[str, _Item]]:
        """Apply every delta page from `link` to `items`. Returns the delta link for next time, and `items`."""
        while True:
            page = self._client.delta_page(link)
            for entry in page.get("value", []):
                _apply(items, entry)
            if "@odata.nextLink" not in page:
                return page["@odata.deltaLink"], items
            link = page["@odata.nextLink"]


def _apply(items: dict[str, _Item], entry: dict[str, Any]) -> None:
    """Record one delta entry: deleted items go, the root and folders are kept for paths, files with their hash."""
    item_id = entry["id"]
    parent = None if "root" in entry else entry.get("parentReference", {}).get("id")
    if "deleted" in entry:
        items.pop(item_id, None)
    elif "root" in entry or "folder" in entry:
        items[item_id] = _Item(parent, entry.get("name", ""), folder=True, hash="")
    elif "file" in entry:
        # quickXorHash is the content hash SharePoint and OneDrive for Business provide; cTag changes with content.
        content_hash = entry["file"].get("hashes", {}).get("quickXorHash") or entry.get("cTag", "")
        items[item_id] = _Item(parent, entry["name"], folder=False, hash=content_hash)
    else:
        items.pop(item_id, None)  # packages, such as OneNote notebooks, aren't files


def _paths(items: dict[str, _Item]) -> dict[str, str | None]:
    """Every item's path from the drive root ("" for the root itself), or None when it can't reach the root."""
    resolved: dict[str, str | None] = {}

    def resolve(item_id: str) -> str | None:
        if item_id in resolved:
            return resolved[item_id]
        resolved[item_id] = None  # guards against a parent cycle
        item = items.get(item_id)
        if item is None:
            return None
        if item.parent is None:
            path: str | None = ""
        else:
            parent = resolve(item.parent)
            path = None if parent is None else f"{parent}/{item.name}"
        resolved[item_id] = path
        return path

    for item_id in items:
        resolve(item_id)
    return resolved


def _file_paths(items: dict[str, _Item], scope: str) -> dict[str, str]:
    """The path of every file that reaches the root and lies in the `scope` folder ("" for the whole drive)."""
    return {
        item_id: path
        for item_id, path in _paths(items).items()
        if path is not None
        and not items[item_id].folder
        and (not scope or path.lower() == scope or path.lower().startswith(scope + "/"))
    }


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
