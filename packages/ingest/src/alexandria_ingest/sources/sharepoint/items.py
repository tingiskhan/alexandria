"""The id → item map SharePointSource keeps in its cursor, and the paths rebuilt from it.

In SharePoint and OneDrive for Business, Graph's delta feed has no item paths, and renaming or deleting a
folder doesn't report the items in it. Keeping every item's parent and name by id lets each run rebuild
the paths. Pure functions, no I/O.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Item:
    """What the cursor remembers about a drive item."""

    parent: str | None  # None for the drive root
    name: str
    folder: bool
    hash: str  # files only


def apply_entry(items: dict[str, Item], entry: dict[str, Any]) -> None:
    """Record one delta entry: deleted items go, the root and folders are kept for paths, files with their hash."""
    item_id = entry["id"]
    parent = None if "root" in entry else entry.get("parentReference", {}).get("id")
    if "deleted" in entry:
        items.pop(item_id, None)
    elif "root" in entry or "folder" in entry:
        items[item_id] = Item(parent, entry.get("name", ""), folder=True, hash="")
    elif "file" in entry:
        # quickXorHash is the content hash SharePoint and OneDrive for Business provide; cTag changes with content.
        content_hash = entry["file"].get("hashes", {}).get("quickXorHash") or entry.get("cTag", "")
        items[item_id] = Item(parent, entry["name"], folder=False, hash=content_hash)
    else:
        items.pop(item_id, None)  # packages, such as OneNote notebooks, aren't files


def paths(items: dict[str, Item]) -> dict[str, str | None]:
    """Every item's path from the drive root ("" for the root itself), or None when it can't reach the root."""
    resolved: dict[str, str | None] = {}

    def resolve(item_id: str) -> str | None:
        if item_id in resolved:
            return resolved[item_id]
        resolved[item_id] = None  # guards against a parent cycle
        item = items.get(item_id)
        if item is None:
            return None
        if item.parent is None:
            path: str | None = ""
        else:
            parent = resolve(item.parent)
            path = None if parent is None else f"{parent}/{item.name}"
        resolved[item_id] = path
        return path

    for item_id in items:
        resolve(item_id)
    return resolved


def file_paths(items: dict[str, Item], scope: str) -> dict[str, str]:
    """The path of every file that reaches the root and lies in the `scope` folder ("" for the whole drive)."""
    return {
        item_id: path
        for item_id, path in paths(items).items()
        if path is not None
        and not items[item_id].folder
        and (not scope or path.lower() == scope or path.lower().startswith(scope + "/"))
    }
