"""Library write operations via Pyzotero Web API."""
import json
from typing import Annotated, Literal, TypedDict

from pydantic import Field

from ..state import ToolError, _get_writer, mcp
from .library import _invalidate_collection_cache


def _coerce_list(value) -> list:
    """Coerce a value to list, parsing JSON string if needed (Claude Code MCP client quirk)."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []


class TagItem(TypedDict):
    item_key: str
    tags: list[str]


_BATCH_MAX = 100


@mcp.tool()
def set_item_tags(
    item_key: Annotated[str, Field(description="Zotero item key")],
    tags: Annotated[list[str], Field(description="New tag list (replaces all existing)")],
) -> dict:
    """Replace ALL tags on an item (destructive). Use add_item_tags to append safely."""
    tags = _coerce_list(tags)
    _get_writer().set_item_tags(item_key, tags)
    return {"success": True, "item_key": item_key, "tags": tags}


@mcp.tool()
def add_item_tags(
    item_key: Annotated[str, Field(description="Zotero item key")],
    tags: Annotated[list[str], Field(description="Tags to add")],
) -> dict:
    """Add tags to an item without removing existing ones."""
    tags = _coerce_list(tags)
    _get_writer().add_item_tags(item_key, tags)
    return {"success": True, "item_key": item_key, "added": tags}


@mcp.tool()
def remove_item_tags(
    item_key: Annotated[str, Field(description="Zotero item key")],
    tags: Annotated[list[str], Field(description="Tags to remove")],
) -> dict:
    """Remove specific tags from an item. Missing tags silently ignored."""
    tags = _coerce_list(tags)
    _get_writer().remove_item_tags(item_key, tags)
    return {"success": True, "item_key": item_key, "removed": tags}


@mcp.tool()
def add_to_collection(
    item_key: Annotated[str, Field(description="Zotero item key")],
    collection_key: Annotated[str, Field(description="Target collection key from list_collections")],
) -> dict:
    """Add a paper to a collection. Non-destructive: stays in existing collections."""
    _get_writer().add_to_collection(item_key, collection_key)
    _invalidate_collection_cache()
    return {"success": True, "item_key": item_key, "collection_key": collection_key}


@mcp.tool()
def remove_from_collection(
    item_key: Annotated[str, Field(description="Zotero item key")],
    collection_key: Annotated[str, Field(description="Collection key to remove from")],
) -> dict:
    """Remove a paper from a collection. Stays in library and other collections."""
    _get_writer().remove_from_collection(item_key, collection_key)
    _invalidate_collection_cache()
    return {"success": True, "item_key": item_key, "collection_key": collection_key}


@mcp.tool()
def create_collection(
    name: Annotated[str, Field(description="Display name for the collection")],
    parent_key: Annotated[str | None, Field(description="Parent collection key for nesting, None for top-level")] = None,  # noqa: E501
) -> dict:
    """Create a new Zotero collection (folder)."""
    result = _get_writer().create_collection(name, parent_key)
    _invalidate_collection_cache()
    return result


@mcp.tool()
def create_note(
    item_key: Annotated[str, Field(description="Parent item key")],
    content: Annotated[str, Field(description="Note content (plain text or HTML)")],
    title: Annotated[str | None, Field(description="Note title (prepended as heading)")] = None,
    tags: Annotated[list[str] | None, Field(description="Tags for the note")] = None,
) -> dict:
    """Create a child note on a Zotero item. Requires ZOTERO_API_KEY."""
    if tags is not None:
        tags = _coerce_list(tags) or None
    return _get_writer().create_note(item_key, content, title=title, tags=tags)


def _extract_tag_item(item) -> tuple[str | None, list[str] | None]:
    """Extract item_key and tags from a TagItem (dict at runtime)."""
    item_key = item.get("item_key") if isinstance(item, dict) else getattr(item, "item_key", None)
    tags = item.get("tags") if isinstance(item, dict) else getattr(item, "tags", None)
    return item_key, tags


def _batch_tag_result(items: list, operation):
    """Run a per-item tag operation and collect results."""
    if len(items) > _BATCH_MAX:
        raise ToolError(f"Batch size {len(items)} exceeds limit of {_BATCH_MAX}")
    writer = _get_writer()
    results = []
    for item in items:
        item_key, tags = _extract_tag_item(item)
        if not item_key or tags is None:
            results.append({"item_key": item_key or "unknown", "success": False, "error": "Missing item_key or tags"})
            continue
        try:
            operation(writer, item_key, tags)
            results.append({"item_key": item_key, "success": True})
        except Exception as e:
            results.append({"item_key": item_key, "success": False, "error": str(e)})
    succeeded = sum(1 for r in results if r["success"])
    return {"total": len(items), "succeeded": succeeded, "failed": len(items) - succeeded, "results": results}


@mcp.tool()
def batch_tags(
    action: Annotated[Literal["add", "set", "remove"], Field(description="add=append, set=replace all (destructive), remove=delete specific tags")],  # noqa: E501
    items: Annotated[list[dict], Field(description="List of {item_key, tags} objects. Max 100.")],
) -> dict:
    """Batch tag operation on multiple items. Partial failures reported per-item."""
    items = _coerce_list(items)
    ops = {
        "add": lambda w, k, t: w.add_item_tags(k, t),
        "set": lambda w, k, t: w.set_item_tags(k, t),
        "remove": lambda w, k, t: w.remove_item_tags(k, t),
    }
    return _batch_tag_result(items, ops[action])


@mcp.tool()
def batch_collections(
    action: Annotated[Literal["add", "remove"], Field(description="add=add to collection, remove=remove from collection")],  # noqa: E501
    item_keys: Annotated[list[str], Field(description="Zotero item keys. Max 100.")],
    collection_key: Annotated[str, Field(description="Target collection key")],
) -> dict:
    """Batch collection operation on multiple items. Partial failures reported per-item."""
    item_keys = _coerce_list(item_keys)
    if len(item_keys) > _BATCH_MAX:
        raise ToolError(f"Batch size {len(item_keys)} exceeds limit of {_BATCH_MAX}")
    writer = _get_writer()
    op = writer.add_to_collection if action == "add" else writer.remove_from_collection
    results = []
    for key in item_keys:
        try:
            op(key, collection_key)
            results.append({"item_key": key, "success": True})
        except Exception as e:
            results.append({"item_key": key, "success": False, "error": str(e)})
    _invalidate_collection_cache()
    succeeded = sum(1 for r in results if r["success"])
    return {"total": len(item_keys), "succeeded": succeeded, "failed": len(item_keys) - succeeded, "results": results}
