"""CrossRef API client for fetching paper metadata by DOI."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

CROSSREF_API = "https://api.crossref.org/works"
_JATS_TAG_RE = re.compile(r"<[^>]+>")
_USER_AGENT = "ZotPilot/1.0 (mailto:zotpilot@example.com)"


@dataclass
class CrossRefWork:
    """Metadata for a paper from CrossRef."""
    doi: str
    title: str
    authors: list[dict]  # [{"creatorType": "author", "firstName": ..., "lastName": ...}]
    year: int | None
    item_type: str  # Zotero item type
    abstract: str | None
    journal: str | None
    volume: str | None
    issue: str | None
    pages: str | None
    publisher: str | None
    url: str | None
    oa_url: str | None
    raw: dict = field(default_factory=dict, repr=False)


def _crossref_type_to_zotero(cr_type: str) -> str:
    """Map CrossRef type string to Zotero item type."""
    mapping = {
        "journal-article": "journalArticle",
        "proceedings-article": "conferencePaper",
        "book": "book",
        "book-chapter": "bookSection",
        "dissertation": "thesis",
        "preprint": "preprint",
        "posted-content": "preprint",
        "report": "report",
        "dataset": "dataset",
        "monograph": "book",
        "reference-book": "book",
        "edited-book": "book",
    }
    return mapping.get(cr_type, "journalArticle")


def _extract_year(data: dict) -> int | None:
    """Extract publication year from CrossRef date fields."""
    for date_field in ("published-print", "published-online", "published", "issued"):
        parts = (data.get(date_field) or {}).get("date-parts", [[]])[0]
        if parts and parts[0]:
            return int(parts[0])
    return None


def _extract_authors(data: dict) -> list[dict]:
    """Extract authors as Zotero-compatible creator dicts."""
    authors = []
    for a in data.get("author", []):
        author = {
            "creatorType": "author",
            "firstName": a.get("given", ""),
            "lastName": a.get("family", ""),
        }
        if author["firstName"] or author["lastName"]:
            authors.append(author)
    return authors


class CrossRefClient:
    """Minimal CrossRef REST API client."""

    def __init__(self):
        self._last_request = 0.0
        self._rate_delay = 0.1  # polite: ~10 req/sec

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < self._rate_delay:
            time.sleep(self._rate_delay - elapsed)
        self._last_request = time.time()

    def get_by_doi(self, doi: str) -> CrossRefWork | None:
        """Fetch metadata for a DOI from CrossRef. Returns None if not found."""
        doi = doi.strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/"):
            if doi.lower().startswith(prefix):
                doi = doi[len(prefix):]
                break

        self._rate_limit()

        try:
            resp = httpx.get(
                f"{CROSSREF_API}/{doi}",
                headers={"User-Agent": _USER_AGENT},
                timeout=15.0,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json().get("message", {})
        except Exception as e:
            logger.warning(f"CrossRef lookup failed for {doi}: {e}")
            return None

        title_list = data.get("title") or []
        title = title_list[0] if title_list else doi

        # OA PDF URL from CrossRef links
        oa_url = None
        for lnk in (data.get("link") or []):
            if lnk.get("content-type") == "application/pdf":
                oa_url = lnk.get("URL")
                break

        container = data.get("container-title") or []
        journal = container[0] if container else None

        abstract = data.get("abstract")
        if abstract:
            abstract = _JATS_TAG_RE.sub("", abstract).strip()

        return CrossRefWork(
            doi=doi,
            title=title,
            authors=_extract_authors(data),
            year=_extract_year(data),
            item_type=_crossref_type_to_zotero(data.get("type", "")),
            abstract=abstract,
            journal=journal,
            volume=data.get("volume"),
            issue=data.get("issue"),
            pages=data.get("page"),
            publisher=data.get("publisher"),
            url=data.get("resource", {}).get("primary", {}).get("URL") or f"https://doi.org/{doi}",
            oa_url=oa_url,
            raw=data,
        )
