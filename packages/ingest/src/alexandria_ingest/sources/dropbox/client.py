"""The real Dropbox client."""

import dropbox

from alexandria_ingest.sources.dropbox.settings import DropboxSettings


def make_dropbox_client(settings: DropboxSettings) -> dropbox.Dropbox:
    """A client that refreshes its short-lived access token from the offline (PKCE) refresh token."""
    return dropbox.Dropbox(oauth2_refresh_token=settings.refresh_token.get_secret_value(), app_key=settings.app_key)
