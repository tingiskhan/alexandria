"""The `alexandria-ingest` command: one-off setup and checks. Syncing runs from alexandria-workflows."""

from collections import Counter
from typing import Annotated

import typer
from botocore.exceptions import BotoCoreError, ClientError
from dropbox.exceptions import DropboxException
from dropbox.oauth import DropboxOAuth2FlowNoRedirect
from pydantic import ValidationError

from alexandria_ingest.pipeline import extension
from alexandria_ingest.settings import ExtractSettings
from alexandria_ingest.sources import make_source

app = typer.Typer(name="alexandria-ingest", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Alexandria ingest tools."""


@app.command()
def auth(
    app_key: Annotated[str, typer.Option(help="The Dropbox app's key (ALEXANDRIA_DROPBOX_APP_KEY).")],
) -> None:
    """Run the Dropbox PKCE flow once and print an offline refresh token for ALEXANDRIA_DROPBOX_REFRESH_TOKEN."""
    flow = DropboxOAuth2FlowNoRedirect(app_key, use_pkce=True, token_access_type="offline")  # noqa: S106
    typer.echo(f"1. Open: {flow.start()}")
    typer.echo("2. Click 'Allow' (log in first if needed).")
    code = typer.prompt("3. Paste the authorization code here").strip()
    result = flow.finish(code)
    typer.echo(f"\nALEXANDRIA_DROPBOX_REFRESH_TOKEN={result.refresh_token}")


@app.command()
def check_source() -> None:
    """Check access to the configured source (ALEXANDRIA_SOURCE_KIND) and list the files a sync would ingest."""
    try:
        formats = ExtractSettings().formats
        configured = make_source()
    except ValidationError as error:
        typer.echo(f"Configuration is incomplete; see .env.example.\n{error}", err=True)
        raise typer.Exit(2) from error
    try:
        changes, _ = configured.source.changes(None)
    except (DropboxException, BotoCoreError, ClientError) as error:
        typer.echo(f"{configured.name}: access failed: {error}", err=True)
        raise typer.Exit(1) from error

    ingested = [c.path for c in changes if extension(c.path) in formats]
    typer.echo(f"{configured.name}: {len(changes)} files, {len(ingested)} to ingest")
    if ingested:
        by_format = Counter(extension(path) for path in ingested)
        typer.echo("  " + ", ".join(f"{count} {ext}" for ext, count in sorted(by_format.items())))
    for path in ingested[:10]:
        typer.echo(f"  {path}")
    if len(ingested) > 10:
        typer.echo(f"  … and {len(ingested) - 10} more")
