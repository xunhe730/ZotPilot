"""Zotero Web API write client (read is handled by ZoteroClient via SQLite)."""
from __future__ import annotations

import logging
import os
import tempfile

import httpx
from pyzotero import zotero

logger = logging.getLogger(__name__)


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

    def create_note(self, item_key: str, content: str, title: str | None = None, tags: list[str] | None = None) -> dict:
        """Create a child note on a Zotero item.

        Args:
            item_key: Parent item key
            content: Note content (plain text or HTML)
            title: Optional title (prepended as heading)
            tags: Optional tags for the note

        Returns:
            Dict with key of created note
        """
        import html as html_mod

        # Convert plain text to HTML if it doesn't look like HTML
        if not content.strip().startswith("<"):
            # Plain text → HTML: paragraphs and line breaks
            paragraphs = content.split("\n\n")
            html_parts = []
            for p in paragraphs:
                escaped = html_mod.escape(p).replace("\n", "<br/>")
                html_parts.append(f"<p>{escaped}</p>")
            html_content = "".join(html_parts)
        else:
            html_content = content

        # Prepend title as heading
        if title:
            html_content = f"<h1>{html_mod.escape(title)}</h1>{html_content}"

        # Build note template
        template = self._zot.item_template("note")
        template["parentItem"] = item_key
        template["note"] = html_content
        template["tags"] = [{"tag": t} for t in (tags or [])]

        result = self._zot.create_items([template])
        if result.get("success"):
            created_key = list(result["success"].values())[0]
            return {"key": created_key, "parent_key": item_key}
        raise RuntimeError(f"Failed to create note: {result}")

    # =========================================================
    # Ingestion helpers
    # =========================================================

    def find_items_by_url_and_title(
        self, url: str, title: str, limit: int = 20
    ) -> list[str]:
        """Find item keys matching BOTH URL and title.

        Used for post-save item discovery when no item key is known.
        Returns list of item keys (usually 0 or 1; >1 is ambiguous, caller
        must not apply routing when ambiguous).
        """
        if not title:
            return []
        try:
            items = self._zot.items(q=title, qmode="titleCreatorYear", limit=limit)
        except Exception:
            return []
        finally:
            # pyzotero accumulates kwargs in url_params and does not reset them
            # after a call — clear so search params don't bleed into subsequent
            # item() / update_item() calls on the same singleton instance.
            self._zot.url_params = {}
        results: list[str] = []
        for item in items:
            item_url = (item.get("data") or {}).get("url", "")
            if item_url and self._urls_match(url, item_url):
                results.append(item["data"]["key"])
        return results

    @staticmethod
    def _urls_match(a: str, b: str) -> bool:
        """Loose URL comparison: ignore trailing slash and volatile query params."""
        import urllib.parse

        def normalize(u: str) -> str:
            try:
                parsed = urllib.parse.urlparse(u)
                # Keep scheme + netloc + path; drop query + fragment
                return (
                    parsed.scheme.lower()
                    + "://"
                    + parsed.netloc.lower()
                    + parsed.path.rstrip("/")
                )
            except Exception:
                return u.rstrip("/").lower()

        return normalize(a) == normalize(b)

    def check_duplicate_by_doi(self, doi: str) -> str | None:
        """Return item key if DOI already exists in library, else None."""
        doi = doi.strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/"):
            if doi.lower().startswith(prefix):
                doi = doi[len(prefix):]
                break

        results = self._zot.items(q=doi, qmode="everything", limit=5)
        for item in results:
            item_doi = (item.get("data") or {}).get("DOI", "").strip()
            for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/"):
                if item_doi.lower().startswith(prefix):
                    item_doi = item_doi[len(prefix):]
                    break
            if item_doi.lower() == doi.lower():
                return item["data"]["key"]
        return None

    def create_item_from_metadata(
        self,
        metadata,
        collection_keys: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Create a Zotero item from PaperMetadata. Returns raw pyzotero result dict."""
        try:
            template = self._zot.item_template(metadata.item_type)
        except Exception:
            template = self._zot.item_template("journalArticle")

        template["title"] = metadata.title
        template["creators"] = metadata.authors or []

        if metadata.doi:
            template["DOI"] = metadata.doi
        if metadata.url:
            template["url"] = metadata.url
        if metadata.abstract:
            template["abstractNote"] = metadata.abstract
        if metadata.year:
            template["date"] = str(metadata.year)
        if metadata.journal:
            if "publicationTitle" in template:
                template["publicationTitle"] = metadata.journal
            elif "proceedingsTitle" in template:
                template["proceedingsTitle"] = metadata.journal
        if metadata.volume and "volume" in template:
            template["volume"] = metadata.volume
        if metadata.issue and "issue" in template:
            template["issue"] = metadata.issue
        if metadata.pages and "pages" in template:
            template["pages"] = metadata.pages
        if metadata.publisher and "publisher" in template:
            template["publisher"] = metadata.publisher
        if collection_keys:
            template["collections"] = collection_keys
        if tags:
            template["tags"] = [{"tag": t} for t in tags]

        return self._zot.create_items([template])

    def try_attach_oa_pdf(
        self,
        item_key: str,
        doi: str | None = None,
        oa_url: str | None = None,
        crossref_raw: dict | None = None,
        arxiv_id: str | None = None,
    ) -> str:
        """Attempt to find and attach an open-access PDF. Returns status string."""
        urls: list[str] = []

        if oa_url:
            urls.append(oa_url)

        if doi:
            unpaywall_url = self._get_unpaywall_pdf_url(doi)
            if unpaywall_url and unpaywall_url not in urls:
                urls.append(unpaywall_url)

        if arxiv_id:
            arxiv_url = f"https://arxiv.org/pdf/{arxiv_id}"
            if arxiv_url not in urls:
                urls.append(arxiv_url)

        if not urls:
            return "not_found"

        for url in urls:
            try:
                if self._download_and_attach_pdf(item_key, url):
                    return "attached"
            except Exception as e:
                logger.debug(f"PDF attach failed for {url}: {e}")

        return "not_found"

    def _get_unpaywall_pdf_url(self, doi: str) -> str | None:
        """Query Unpaywall for an OA PDF URL."""
        try:
            resp = httpx.get(
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": "zotpilot@example.com"},
                timeout=10.0,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            best_oa = resp.json().get("best_oa_location") or {}
            return best_oa.get("url_for_pdf")
        except Exception as e:
            logger.debug(f"Unpaywall lookup failed for {doi}: {e}")
            return None

    def _download_and_attach_pdf(self, item_key: str, url: str) -> bool:
        """Download PDF from URL and attach to Zotero item. Returns True on success."""
        resp = httpx.get(url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "pdf" not in content_type.lower() and not resp.content[:4] == b"%PDF":
            return False

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(resp.content)
            tmp_path = f.name

        try:
            self._zot.attachment_simple([tmp_path], item_key)
            return True
        finally:
            os.unlink(tmp_path)
