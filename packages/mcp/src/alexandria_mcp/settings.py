"""Settings for the MCP server's HTTP transport."""

from typing import Annotated

from mcp.server.transport_security import TransportSecuritySettings
from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode

from alexandria_core.settings import settings_config

LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


class McpSettings(BaseSettings):
    """Where `--transport streamable-http` listens (ALEXANDRIA_MCP_*). stdio ignores these."""

    model_config = settings_config("ALEXANDRIA_MCP_")

    host: str = "127.0.0.1"  # localhost only; a container sets 0.0.0.0 and publishes the port on localhost
    port: int = 8000
    # Other names clients reach it by, comma-separated in the environment, e.g. an authenticating proxy's.
    allowed_hosts: Annotated[tuple[str, ...], NoDecode] = ()

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        """Split the comma-separated environment value."""
        return tuple(host.strip() for host in value.split(",") if host.strip()) if isinstance(value, str) else value

    def transport_security(self) -> TransportSecuritySettings:
        """Reject requests for any other Host or Origin, so a web page can't reach the server by DNS rebinding.

        The SDK does this by itself only when `host` is a localhost address, which it isn't in a container.
        """
        hosts = (*LOCAL_HOSTS, *self.allowed_hosts)
        return TransportSecuritySettings(
            allowed_hosts=[pattern for host in hosts for pattern in (host, f"{host}:*")],
            allowed_origins=[
                pattern
                for host in hosts
                for scheme in ("http", "https")
                for pattern in (f"{scheme}://{host}", f"{scheme}://{host}:*")
            ],
        )
