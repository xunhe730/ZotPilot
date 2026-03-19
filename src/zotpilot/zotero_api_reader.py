"""Read-only Zotero Web API client for data not reliably available via SQLite."""
from __future__ import annotations
from pyzotero import zotero


class ZoteroApiReader:
    """
    Read-only access to Zotero library via Web API v3 (Pyzotero).

    Used for data that is unstable across Zotero SQLite versions,
    such as annotations. Requires ZOTERO_API_KEY but NOT write access.
    """

    def __init__(self, api_key: str, user_id: str, library_type: str = "user"):
        self._zot = zotero.Zotero(user_id, library_type, api_key)

    def get_annotations(self, item_key: str | None = None, limit: int = 50) -> list[dict]:
        """Get annotations (highlights, comments) from the library.

        Args:
            item_key: If provided, get annotations for this specific item.
                      If None, get all annotations in the library.
            limit: Max annotations to return.

        Returns:
            List of annotation dicts with key, parent_key, type, text, comment, etc.
        """
        if item_key:
            # Get children of the item, filter to annotations
            children = self._zot.children(item_key)
            annotations = [
                c for c in children
                if c.get("data", {}).get("itemType") == "annotation"
            ]
        else:
            # Get all annotations in library
            annotations = self._zot.items(itemType="annotation", limit=limit)

        results = []
        for ann in annotations[:limit]:
            data = ann.get("data", {})
            results.append({
                "key": data.get("key", ""),
                "parent_key": data.get("parentItem", ""),
                "type": data.get("annotationType", ""),
                "text": data.get("annotationText", ""),
                "comment": data.get("annotationComment", ""),
                "color": data.get("annotationColor", ""),
                "page": data.get("annotationPageLabel", ""),
                "tags": [t.get("tag", "") for t in data.get("tags", [])],
            })
        return results
