"""Settings for the SharePoint source."""

from typing import Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings

from alexandria_core.settings import settings_config


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
