"""The `alexandria-ingest` command: one-off setup and checks. Syncing runs from alexandria-workflows."""

from collections import Counter
from typing import Annotated

import httpx
import typer
from botocore.exceptions import BotoCoreError, ClientError
from dropbox.exceptions import DropboxException
from dropbox.oauth import DropboxOAuth2FlowNoRedirect
from pydantic import ValidationError

from alexandria_core.db import make_engine
from alexandria_core.migrate import migrate
from alexandria_core.models import DocumentMetadata
from alexandria_core.settings import DatabaseSettings
from alexandria_core.store import PgStore
from alexandria_ingest.metadata import make_metadata_reader
from alexandria_ingest.pipeline import duplicate_groups, formula_errors, read_stored_metadata, stored_content_digest
from alexandria_ingest.settings import ExtractSettings, MetadataSettings, extension
from alexandria_ingest.sources import make_source
from alexandria_ingest.sources.sharepoint import GraphError, SharePointSettings, make_sharepoint_client

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
    except (DropboxException, BotoCoreError, ClientError, GraphError, httpx.HTTPError) as error:
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


@app.command()
def check_formulas() -> None:
    """Run every stored formula through KaTeX and list the failures per document and page. Exits 1 if there are any."""
    try:
        store = PgStore(make_engine(DatabaseSettings().url.get_secret_value()))
    except ValidationError as error:
        typer.echo(f"Configuration is incomplete; see .env.example.\n{error}", err=True)
        raise typer.Exit(2) from error
    failures = checked = 0
    for summary in store.list_documents():
        for page in store.get_pages(summary.id, 1, summary.page_count):
            checked += 1
            for formula, error in formula_errors(page):
                failures += 1
                typer.echo(f"{summary.id}\t{page.number}\t{summary.path}\t{error}\t{formula.latex}")
    typer.echo(f"{failures} invalid formulas on {checked} pages", err=True)
    if failures:
        raise typer.Exit(1)


@app.command()
def backfill_metadata(
    dry_run: Annotated[bool, typer.Option(help="Print what would change without storing it.")] = False,
) -> None:
    """Work out title, authors, year, DOI and type again for every stored document, from its stored first page.

    Neither downloads nor extracts anything, so it is quick. Migrates the database to the newest schema first.
    Search embeds each chunk with its document's title, so run the `reindex` flow afterwards
    for search to use the new titles too.
    """
    try:
        database, metadata = DatabaseSettings(), MetadataSettings()
    except ValidationError as error:
        typer.echo(f"Configuration is incomplete; see .env.example.\n{error}", err=True)
        raise typer.Exit(2) from error
    engine = make_engine(database.url.get_secret_value())
    migrate(engine)  # even for a dry run: the documents can't be listed without the columns
    store, reader = PgStore(engine), make_metadata_reader(metadata)
    changed = 0
    summaries = store.list_documents()
    for summary in summaries:
        found = read_stored_metadata(summary, store=store, metadata_reader=reader)
        if found == summary.metadata:
            continue
        changed += 1
        typer.echo(f"{summary.id}\t{summary.path}\t{describe(found)}")
        if not dry_run:
            store.update_metadata(summary.id, found)
    verb = "would change" if dry_run else "changed"
    typer.echo(f"{verb} the metadata of {changed} of {len(summaries)} documents", err=True)


def describe(metadata: DocumentMetadata) -> str:
    """One line with everything in `metadata`, for the backfill's output."""
    authors = "; ".join(metadata.authors) or "-"
    return f"{metadata.title}\t{authors}\t{metadata.year or '-'}\t{metadata.doi or '-'}\t{metadata.doc_type}"


@app.command()
def duplicates() -> None:
    """List stored documents with the same content as another, so the copies can be removed at the source.

    Prints one line per copy: its id and path, then the id and path of the document search uses instead, and
    whether the copy is still searched. Copies ingested before duplicates were detected are; they stop being
    searched when deleted, or when re-ingested (the `reindex` flow, or a change to the file).

    Documents ingested before duplicates were detected have no content digest yet. This computes it from their
    stored pages, without downloading or extracting anything, and stores it, so later copies are recognised.
    Migrates the database to the newest schema first.
    """
    try:
        database = DatabaseSettings()
    except ValidationError as error:
        typer.echo(f"Configuration is incomplete; see .env.example.\n{error}", err=True)
        raise typer.Exit(2) from error
    engine = make_engine(database.url.get_secret_value())
    migrate(engine)
    store = PgStore(engine)
    summaries = store.list_documents()
    groups = duplicate_groups(summaries, [stored_content_digest(s, store=store) for s in summaries])
    copies = searched = 0
    for original, *others in groups:
        for copy in others:
            copies += 1
            searched += copy.duplicate_of is None
            state = "searched" if copy.duplicate_of is None else "not searched"
            typer.echo(f"{copy.id}\t{copy.path}\t{original.id}\t{original.path}\t{state}")
    typer.echo(
        f"{copies} copies of {len(groups)} documents among {len(summaries)}; {searched} of the copies are searched",
        err=True,
    )


@app.command()
def sharepoint_libraries() -> None:
    """List the document libraries of ALEXANDRIA_SHAREPOINT_SITE with their drive ids."""
    try:
        settings = SharePointSettings()
    except ValidationError as error:
        typer.echo(f"Configuration is incomplete; see .env.example.\n{error}", err=True)
        raise typer.Exit(2) from error
    try:
        libraries = make_sharepoint_client(settings).libraries()
    except (GraphError, httpx.HTTPError) as error:
        typer.echo(f"{settings.site}: access failed: {error}", err=True)
        raise typer.Exit(1) from error
    for name, drive_id in libraries:
        typer.echo(f"{name}\t{drive_id}")
