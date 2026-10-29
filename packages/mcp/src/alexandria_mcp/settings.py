"""Settings for the MCP server's HTTP transport."""

from pydantic_settings import BaseSettings

from alexandria_core.settings import settings_config


class McpSettings(BaseSettings):
    """Where `--transport streamable-http` listens (ALEXANDRIA_MCP_*). stdio ignores these."""

    model_config = settings_config("ALEXANDRIA_MCP_")

    host: str = "127.0.0.1"  # localhost only; a container sets 0.0.0.0 and publishes the port on localhost
    port: int = 8000
