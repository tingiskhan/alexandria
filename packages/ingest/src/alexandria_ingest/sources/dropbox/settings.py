"""Settings for the Dropbox source."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings

from alexandria_core.settings import settings_config


class DropboxSettings(BaseSettings):
    """Dropbox app and folder (ALEXANDRIA_DROPBOX_*)."""

    model_config = settings_config("ALEXANDRIA_DROPBOX_")

    app_key: str
    refresh_token: SecretStr  # from `alexandria-ingest auth`; valid until the app is revoked
    path: str = ""  # folder to sync, starting with "/"; "" is the app folder root
