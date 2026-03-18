"""Library write operations via Pyzotero Web API."""
from ..state import mcp, _get_writer
from .library import _invalidate_collection_cache


@mcp.tool()
def set_item_tags(item_key: str, tags: list[str]) -> dict:
    """
    Replace ALL tags on a Zotero item with a new set.

    WARNING: This overwrites existing tags completely.
    Use add_item_tags to append without removing existing tags.

    Args:
        item_key: Zotero item key (e.g. "FRF9ACAJ")
        tags: New tag list (replaces everything)

    Returns:
        {"success": true, "item_key": ..., "tags": [...]}
    """
    _get_writer().set_item_tags(item_key, tags)
    return {"success": True, "item_key": item_key, "tags": tags}


@mcp.tool()
def add_item_tags(item_key: str, tags: list[str]) -> dict:
    """
    Add tags to a Zotero item WITHOUT removing existing tags.

    Safe to call multiple times -- existing tags are preserved.

    Args:
        item_key: Zotero item key (e.g. "FRF9ACAJ")
        tags: Tags to add

    Returns:
        {"success": true, "item_key": ..., "added": [...]}
    """
    _get_writer().add_item_tags(item_key, tags)
    return {"success": True, "item_key": item_key, "added": tags}


@mcp.tool()
def remove_item_tags(item_key: str, tags: list[str]) -> dict:
    """
    Remove specific tags from a Zotero item.

    Tags not present on the item are silently ignored.

    Args:
        item_key: Zotero item key (e.g. "FRF9ACAJ")
        tags: Tags to remove

    Returns:
        {"success": true, "item_key": ..., "removed": [...]}
    """
    _get_writer().remove_item_tags(item_key, tags)
    return {"success": True, "item_key": item_key, "removed": tags}


@mcp.tool()
def add_to_collection(item_key: str, collection_key: str) -> dict:
    """
    Add a paper to a Zotero collection (folder).

    Non-destructive: paper remains in any collections it's already in.
    Use list_collections() to find collection keys.

    Args:
        item_key: Zotero item key (e.g. "FRF9ACAJ")
        collection_key: Target collection key (from list_collections)

    Returns:
        {"success": true, "item_key": ..., "collection_key": ...}
    """
    _get_writer().add_to_collection(item_key, collection_key)
    _invalidate_collection_cache()
    return {"success": True, "item_key": item_key, "collection_key": collection_key}


@mcp.tool()
def remove_from_collection(item_key: str, collection_key: str) -> dict:
    """
    Remove a paper from a Zotero collection.

    The paper remains in the library and any other collections it belongs to.

    Args:
        item_key: Zotero item key (e.g. "FRF9ACAJ")
        collection_key: Collection key to remove from (from list_collections)

    Returns:
        {"success": true, "item_key": ..., "collection_key": ...}
    """
    _get_writer().remove_from_collection(item_key, collection_key)
    _invalidate_collection_cache()
    return {"success": True, "item_key": item_key, "collection_key": collection_key}


@mcp.tool()
def create_collection(name: str, parent_key: str | None = None) -> dict:
    """
    Create a new Zotero collection (folder).

    Args:
        name: Display name for the new collection
        parent_key: Key of parent collection for nested folders (None = top-level)

    Returns:
        {"key": ..., "name": ..., "parent_key": ...}
    """
    result = _get_writer().create_collection(name, parent_key)
    _invalidate_collection_cache()
    return result
