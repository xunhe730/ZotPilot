"""Zotero Web API write client (read is handled by ZoteroClient via SQLite)."""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone

import httpx
from pyzotero import zotero

from .zotero_client import _strip_html

logger = logging.getLogger(__name__)


class ZoteroQuotaExceeded(Exception):
    """Zotero Web file storage quota rejected the upload (HTTP 413)."""


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
        self._zot.url_params = None  # item_template may leave url_params dirty on cache hit
        template["parentItem"] = item_key
        template["note"] = html_content
        template["tags"] = [{"tag": t} for t in (tags or [])]

        result = self._zot.create_items([template])
        if result.get("success"):
            created_key = list(result["success"].values())[0]
            return {"key": created_key, "parent_key": item_key}
        raise RuntimeError(f"Failed to create note: {result}")

    def get_notes(self, item_key: str | None = None, query: str | None = None, limit: int = 20) -> list[dict]:
        """Read notes via Zotero Web API. Returns notes in the same format as ZoteroClient.get_notes."""
        try:
            if item_key:
                raw_items = self._zot.children(item_key, itemType="note")
            else:
                self._zot.url_params = {}
                raw_items = self._zot.items(itemType="note", limit=limit)
        except Exception as e:
            logger.warning("ZoteroWriter.get_notes failed: %s", e)
            return []
        finally:
            self._zot.url_params = {}

        results: list[dict] = []
        for item in raw_items:
            data = item.get("data") or {}
            if data.get("itemType") != "note":
                continue
            raw_content = data.get("note") or ""
            content = _strip_html(raw_content)
            if query and query.lower() not in content.lower():
                continue
            tag_list = data.get("tags") or []
            tags_str = "; ".join(t["tag"] for t in tag_list if t.get("tag"))
            results.append({
                "key": data.get("key") or item.get("key") or "",
                "parent_key": data.get("parentItem") or "",
                "parent_title": "",
                "tags": tags_str,
                "content": content,
                "date_added": data.get("dateAdded") or "",
            })
            if len(results) >= limit:
                break
        return results

    def get_item_tags(self, item_key: str) -> list[str]:
        """Return current tag strings for an item via the Web API."""
        try:
            item = self._zot.item(item_key)
            return sorted(
                tag["tag"]
                for tag in (item.get("data") or {}).get("tags", [])
                if tag.get("tag")
            )
        except Exception as e:
            logger.warning("get_item_tags(%s) failed: %s", item_key, e)
            return []

    def get_item_collection_keys(self, item_key: str) -> list[str]:
        """Return current collection keys for an item via the Web API."""
        try:
            item = self._zot.item(item_key)
            return sorted((item.get("data") or {}).get("collections", []) or [])
        except Exception as e:
            logger.warning("get_item_collection_keys(%s) failed: %s", item_key, e)
            return []

    # =========================================================
    # Ingestion helpers
    # =========================================================

    def find_items_by_url_and_title(
        self, url: str, title: str, limit: int = 20, window_s: int | None = None
    ) -> list[str]:
        """Find item keys matching title (and optionally URL) within a time window.

        Used for post-save item discovery when no item key is known.
        Returns list of item keys (usually 0 or 1; >1 is ambiguous, caller
        must not apply routing when ambiguous).

        window_s: if set, only items added within the last window_s seconds are
        considered. This prevents false positives from older duplicate-title items.
        """
        if not title:
            return []
        try:
            kwargs: dict = {"q": title, "qmode": "titleCreatorYear", "limit": limit,
                            "sort": "dateAdded", "direction": "desc"}
            items = self._zot.items(**kwargs)
        except Exception:
            return []
        finally:
            # pyzotero accumulates kwargs in url_params and does not reset them
            # after a call — clear so search params don't bleed into subsequent
            # item() / update_item() calls on the same singleton instance.
            self._zot.url_params = {}
        now = datetime.now(timezone.utc)
        results: list[str] = []
        for item in items:
            data = item.get("data") or {}
            # Time-window filter: skip items added before the window
            if window_s is not None:
                date_added_str = data.get("dateAdded", "")
                if date_added_str:
                    try:
                        date_added = datetime.fromisoformat(
                            date_added_str.replace("Z", "+00:00")
                        )
                        if (now - date_added).total_seconds() > window_s:
                            continue
                    except ValueError:
                        pass  # unparseable date — don't filter out
            # URL filter: accept items with no URL (journal articles rarely store it),
            # only reject items whose URL is present but doesn't match.
            item_url = data.get("url", "")
            if not item_url or self._urls_match(url, item_url):
                results.append(data["key"])
        return results

    def find_items_by_title(
        self, title: str, limit: int = 10, sort_by: str | None = None
    ) -> list[str]:
        """Find item keys matching title.

        Used as a fallback when URL-normalized match fails. When sort_by is
        "dateAdded", results are sorted descending (most recent first) to prefer
        newly saved items.
        """
        if not title:
            return []
        try:
            kwargs = {"q": title, "qmode": "titleCreatorYear", "limit": limit}
            if sort_by == "dateAdded":
                kwargs["sort"] = "dateAdded"
                kwargs["direction"] = "desc"
            items = self._zot.items(**kwargs)
        except Exception:
            return []
        finally:
            self._zot.url_params = {}
        return [item["data"]["key"] for item in items]

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
        try:
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
        finally:
            self._zot.url_params = {}

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
        self._zot.url_params = None  # item_template may leave url_params dirty on cache hit

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
        def _canonicalize(u: str) -> str:
            # arxiv.org/pdf/X and arxiv.org/pdf/X.pdf redirect to the same PDF.
            # Strip trailing .pdf and querystring so string-equality dedup works.
            base = u.split("?", 1)[0].rstrip("/")
            if base.endswith(".pdf"):
                base = base[:-4]
            return base

        urls: list[str] = []
        seen: set[str] = set()

        def _add(u: str | None) -> None:
            if not u:
                return
            key = _canonicalize(u)
            if key in seen:
                return
            seen.add(key)
            urls.append(u)

        _add(oa_url)
        if doi:
            _add(self._get_unpaywall_pdf_url(doi))
        if arxiv_id:
            _add(f"https://arxiv.org/pdf/{arxiv_id}")

        if not urls:
            return "not_found"

        had_attempt_error = False
        for url in urls:
            try:
                if self._download_and_attach_pdf(item_key, url):
                    return "attached"
            except ZoteroQuotaExceeded:
                # Trying another URL won't help — the quota is per-library.
                return "quota_exceeded"
            except Exception as e:
                logger.debug(f"PDF attach failed for {url}: {e}")
                had_attempt_error = True

        return "attach_failed" if had_attempt_error else "not_found"

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

    def get_item_type(self, item_key: str) -> str | None:
        """Return the Zotero itemType string for an item (e.g. 'journalArticle', 'webpage').
        Returns None if item not found or API call fails."""
        try:
            item = self._zot.item(item_key)
            return (item.get("data") or {}).get("itemType")
        except Exception as e:
            logger.warning("get_item_type(%s) failed: %s", item_key, e)
            return None

    def delete_item(self, item_key: str) -> bool:
        """Move an item to Zotero trash. Returns True on success."""
        try:
            item = self._zot.item(item_key)
            self._zot.delete_item(item)
            return True
        except Exception as e:
            logger.warning("delete_item(%s) failed: %s", item_key, e)
            return False

    def check_has_pdf(self, item_key: str) -> bool:
        """Return True if the item has at least one PDF attachment in Zotero."""
        try:
            children = self._zot.children(item_key)
            return any(
                (c.get("data") or {}).get("contentType") == "application/pdf"
                for c in children
            )
        except Exception as e:
            logger.debug("check_has_pdf(%s) failed: %s", item_key, e)
            return False

    def _download_and_attach_pdf(self, item_key: str, url: str) -> bool:
        """Download PDF from URL and attach to Zotero item. Returns True on success.

        Raises ZoteroQuotaExceeded when Zotero Web file storage quota is full
        (HTTP 413). Half-created attachment records are cleaned up so they do
        not leave broken children on the parent item.
        """
        resp = httpx.get(url, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "pdf" not in content_type.lower() and not resp.content[:4] == b"%PDF":
            return False

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(resp.content)
            tmp_path = f.name

        # Snapshot attachment-item keys before upload so we can delete any
        # orphan record pyzotero leaves behind on 413.
        before_keys = {
            (c.get("data") or {}).get("key")
            for c in self._zot.children(item_key) or []
        }

        try:
            self._zot.attachment_simple([tmp_path], item_key)
            return True
        except Exception as exc:
            msg = str(exc)
            if "413" in msg or "exceed quota" in msg.lower():
                # attachment item was created but file upload rejected — purge it
                for c in self._zot.children(item_key) or []:
                    k = (c.get("data") or {}).get("key")
                    if k and k not in before_keys:
                        try:
                            self._zot.delete_item(c)
                        except Exception:
                            pass
                raise ZoteroQuotaExceeded(
                    "Zotero Web storage quota exceeded (413). "
                    "Free the cloud quota or run Zotero Desktop's "
                    "'Find Available PDF' which saves locally."
                ) from exc
            raise
        finally:
            os.unlink(tmp_path)
