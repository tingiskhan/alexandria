"""What the SharePoint source and its client depend on, so tests can pass fakes."""

from typing import Any, Protocol

from azure.core.credentials import AccessToken


class Credential(Protocol):
    """Where access tokens come from: azure-identity's credentials, or a fake in tests."""

    def get_token(self, *scopes: str) -> AccessToken:
        """An access token for `scopes`. azure-identity caches it and renews it shortly before it expires."""
        ...


class GraphClient(Protocol):
    """The Graph calls SharePointSource makes. Raises GraphError, or ResyncRequiredError for an expired delta link."""

    def delta_page(self, link: str | None) -> dict[str, Any]:
        """One page of the drive's delta feed: from the start when `link` is None, else from a next or delta link."""
        ...

    def download(self, path: str) -> bytes:
        """The content of the file at `path` (from the drive root, starting with "/")."""
        ...
