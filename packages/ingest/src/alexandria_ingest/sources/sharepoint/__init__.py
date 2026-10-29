"""FileSource over a SharePoint document library or a OneDrive for Business drive, via Microsoft Graph.

Signs in as an Entra ID app with a client secret (client credentials, through azure-identity), so syncs run
unattended. The README describes the app registration and how an admin grants it read access to a site.
"""

from alexandria_ingest.sources.sharepoint.client import GraphHttpClient, make_sharepoint_client
from alexandria_ingest.sources.sharepoint.errors import GraphError, ResyncRequiredError
from alexandria_ingest.sources.sharepoint.protocols import Credential, GraphClient
from alexandria_ingest.sources.sharepoint.settings import SharePointSettings
from alexandria_ingest.sources.sharepoint.source import SharePointSource

__all__ = [
    "Credential",
    "GraphClient",
    "GraphError",
    "GraphHttpClient",
    "ResyncRequiredError",
    "SharePointSettings",
    "SharePointSource",
    "make_sharepoint_client",
]
