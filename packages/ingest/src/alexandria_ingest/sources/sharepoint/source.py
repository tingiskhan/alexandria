"""SharePointSource: change detection with Graph's delta feed and the item map in the cursor."""

import json
import logging
from dataclasses import astuple
from typing import Any

from alexandria_core.models import FileChange
from alexandria_ingest.sources.sharepoint.errors import ResyncRequiredError
from alexandria_ingest.sources.sharepoint.items import Item, apply_entry, file_paths, paths
from alexandria_ingest.sources.sharepoint.protocols import GraphClient

log = logging.getLogger(__name__)


class SharePointSource:
    """FileSource over a drive. Files are identified by lower-cased path, like Dropbox's path_lower.

    The cursor is JSON, `{"delta": link, "items": {id: [parent, name, folder, hash]}}` (see `items`). Each run
    applies the delta to the items and diffs the paths and hashes before and after. A moved or renamed file
    comes out as a deletion of its old path and a change at the new one.
    """

    def __init__(self, client: GraphClient, path: str) -> None:
        self._client = client
        self._scope = path.rstrip("/").lower()

    def changes(self, cursor: str | None) -> tuple[list[FileChange], str]:
        """Files added, changed, moved or deleted since `cursor` (every file when it is None)."""
        state: dict[str, Any] = json.loads(cursor) if cursor else {"delta": None, "items": {}}
        previous = {item_id: Item(*fields) for item_id, fields in state["items"].items()}
        try:
            link, current = self._apply_delta(state["delta"], dict(previous))
        except ResyncRequiredError:
            # Graph expired the delta link: list the drive again. The diff below still finds what changed.
            log.warning("SharePoint delta link expired; listing the whole drive again")
            link, current = self._apply_delta(None, {})

        old_paths, new_paths = file_paths(previous, self._scope), file_paths(current, self._scope)
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
        reachable = paths(current)
        items = {item_id: list(astuple(item)) for item_id, item in current.items() if reachable[item_id] is not None}
        return [*deletions, *updates], json.dumps({"delta": link, "items": items}, sort_keys=True)

    def download(self, file_id: str) -> bytes:
        """The file's bytes; `file_id` is its lower-cased path, which Graph matches case-insensitively."""
        return self._client.download(file_id)

    def _apply_delta(self, link: str | None, items: dict[str, Item]) -> tuple[str, dict[str, Item]]:
        """Apply every delta page from `link` to `items`. Returns the delta link for next time, and `items`."""
        while True:
            page = self._client.delta_page(link)
            for entry in page.get("value", []):
                apply_entry(items, entry)
            if "@odata.nextLink" not in page:
                return page["@odata.deltaLink"], items
            link = page["@odata.nextLink"]
