"""Zotero Web API write client (read is handled by ZoteroClient via SQLite)."""
from __future__ import annotations
from pyzotero import zotero


class ZoteroWriter:
    """
    Write access to Zotero library via official Web API v3 (Pyzotero).

    Reads are NOT done here — use ZoteroClient for reads.
    This class only handles mutations: tags, collections, etc.
    """

    def __init__(self, api_key: str, user_id: str, library_type: str = "user"):
        self._zot = zotero.Zotero(user_id, library_type, api_key)

    # =========================================================
    # Tag operations
    # =========================================================

    def set_item_tags(self, item_key: str, tags: list[str]) -> dict:
        """Replace all tags on an item with the given list."""
        item = self._zot.item(item_key)
        item["data"]["tags"] = [{"tag": t} for t in tags]
        return self._zot.update_item(item)

    def add_item_tags(self, item_key: str, tags: list[str]) -> dict:
        """Add tags to an item without removing existing ones."""
        item = self._zot.item(item_key)
        existing = {t["tag"] for t in item["data"].get("tags", [])}
        new_tags = existing | set(tags)
        item["data"]["tags"] = [{"tag": t} for t in sorted(new_tags)]
        return self._zot.update_item(item)

    def remove_item_tags(self, item_key: str, tags: list[str]) -> dict:
        """Remove specific tags from an item."""
        item = self._zot.item(item_key)
        remove_set = set(tags)
        item["data"]["tags"] = [
            t for t in item["data"].get("tags", [])
            if t["tag"] not in remove_set
        ]
        return self._zot.update_item(item)

    # =========================================================
    # Collection operations
    # =========================================================

    def add_to_collection(self, item_key: str, collection_key: str) -> bool:
        """Add an item to a collection (non-destructive, keeps existing collections)."""
        item = self._zot.item(item_key)
        existing = set(item["data"].get("collections", []))
        existing.add(collection_key)
        item["data"]["collections"] = list(existing)
        self._zot.update_item(item)
        return True

    def remove_from_collection(self, item_key: str, collection_key: str) -> bool:
        """Remove an item from a collection."""
        item = self._zot.item(item_key)
        existing = set(item["data"].get("collections", []))
        existing.discard(collection_key)
        item["data"]["collections"] = list(existing)
        self._zot.update_item(item)
        return True

    def create_collection(self, name: str, parent_key: str | None = None) -> dict:
        """Create a new collection. Returns the created collection's data."""
        payload = [{"name": name, "parentCollection": parent_key or False}]
        result = self._zot.create_collections(payload)
        # Pyzotero returns {"success": {"0": key}, "unchanged": {}, "failed": {}}
        if result.get("success"):
            created_key = list(result["success"].values())[0]
            return {"key": created_key, "name": name, "parent_key": parent_key}
        raise RuntimeError(f"Failed to create collection: {result}")
