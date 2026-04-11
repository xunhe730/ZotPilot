"""Resolve paper identifiers (DOI, arXiv, S2) to Zotero-compatible metadata."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx
from fastmcp.exceptions import ToolError

from .crossref_client import CrossRefClient

logger = logging.getLogger(__name__)

_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_ARXIV_OLD_RE = re.compile(r"^[a-z-]+/\d{7}(v\d+)?$")
_DOI_RE = re.compile(r"^10\.\d{4,}/\S+")
_S2_ID_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class PaperMetadata:
    """Normalized paper metadata ready for Zotero ingestion."""
    doi: str | None
    title: str
    item_type: str  # Zotero item type (e.g. "journalArticle", "preprint")
    oa_url: str | None
    arxiv_id: str | None
    authors: list[dict]  # [{"creatorType": "author", "firstName": ..., "lastName": ...}]
    year: int | None = None
    journal: str | None = None
    abstract: str | None = None
    pages: str | None = None
    volume: str | None = None
    issue: str | None = None
    publisher: str | None = None
    url: str | None = None


class IdentifierResolver:
    """Resolves paper identifiers to PaperMetadata.

    Supported formats:
    - DOI: 10.xxxx/... or https://doi.org/10.xxxx/...
    - arXiv: arxiv:NNNN.NNNNN or https://arxiv.org/abs/NNNN.NNNNN
    - Semantic Scholar paper ID: 40-char hex string
    """

    def __init__(self):
        self._crossref = CrossRefClient()
        self.last_crossref_metadata: dict | None = None

    def resolve(self, identifier: str) -> PaperMetadata:
        """Resolve an identifier to PaperMetadata.

        Raises ToolError for unrecognized formats or lookup failures.
        """
        identifier = identifier.strip()

        # doi.org URLs
        for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/"):
            if identifier.lower().startswith(prefix):
                doi = identifier[len(prefix):]
                return self._resolve_doi(doi)

        # arXiv URLs
        for pattern in ("arxiv.org/abs/", "arxiv.org/pdf/"):
            idx = identifier.lower().find(pattern)
            if idx != -1:
                arxiv_id = identifier[idx + len(pattern):].split("?")[0].rstrip(".pdf")
                return self._resolve_arxiv(arxiv_id)

        # Explicit arxiv: prefix
        if identifier.lower().startswith("arxiv:"):
            return self._resolve_arxiv(identifier[6:])

        # Bare DOI
        if _DOI_RE.match(identifier):
            return self._resolve_doi(identifier)

        # Bare arXiv ID (new format NNNN.NNNNN or old format cs/XXXXXXX)
        if _ARXIV_ID_RE.match(identifier) or _ARXIV_OLD_RE.match(identifier):
            return self._resolve_arxiv(identifier)

        # Semantic Scholar paper ID (40-char hex)
        if _S2_ID_RE.match(identifier):
            return self._resolve_s2(identifier)

        raise ToolError(
            f"Unrecognized identifier format: {identifier!r}. "
            "Supported: DOI (10.xxxx/...), arXiv ID (arxiv:NNNN.NNNNN), "
            "arXiv URL (arxiv.org/abs/...), doi.org URL, or Semantic Scholar paper ID (40-char hex)."
        )

    def _resolve_doi(self, doi: str) -> PaperMetadata:
        """Fetch metadata from CrossRef by DOI.

        arXiv DOIs (10.48550/arXiv.NNNN.NNNNN) are routed to the arXiv API
        since CrossRef does not index them.
        """
        # arXiv DOI fast-path: avoid CrossRef 404 for 10.48550/arXiv.*
        doi_lower = doi.strip().lower()
        arxiv_prefix = "10.48550/arxiv."
        if doi_lower.startswith(arxiv_prefix):
            arxiv_id = doi[len(arxiv_prefix):].strip()
            return self._resolve_arxiv(arxiv_id)

        work = self._crossref.get_by_doi(doi)
        if work is None:
            raise ToolError(f"DOI not found in CrossRef: {doi!r}")
        self.last_crossref_metadata = work.raw
        return PaperMetadata(
            doi=work.doi,
            title=work.title,
            item_type=work.item_type,
            oa_url=work.oa_url,
            arxiv_id=None,
            authors=work.authors,
            year=work.year,
            journal=work.journal,
            abstract=work.abstract,
            pages=work.pages,
            volume=work.volume,
            issue=work.issue,
            publisher=work.publisher,
            url=work.url,
        )

    def _resolve_arxiv(self, arxiv_id: str) -> PaperMetadata:
        """Fetch metadata from arXiv Atom API."""
        clean_id = re.sub(r"v\d+$", "", arxiv_id.strip())

        try:
            resp = httpx.get(
                "http://export.arxiv.org/api/query",
                params={"id_list": clean_id, "max_results": 1},
                timeout=15.0,
            )
            resp.raise_for_status()
        except httpx.TimeoutException:
            raise ToolError(f"arXiv API timed out for {arxiv_id!r}")
        except httpx.HTTPStatusError as e:
            raise ToolError(f"arXiv API error: {e.response.status_code}")

        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom",
        }
        try:
            root = ET.fromstring(resp.text)
            entry = root.find("atom:entry", ns)
            if entry is None:
                raise ToolError(f"arXiv ID not found: {arxiv_id!r}")

            title = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
            abstract = (entry.findtext("atom:summary", "", ns) or "").strip()
            published = entry.findtext("atom:published", "", ns) or ""
            year = int(published[:4]) if len(published) >= 4 else None

            doi_el = entry.find("arxiv:doi", ns)
            doi = doi_el.text.strip() if doi_el is not None and doi_el.text else None

            authors = []
            for author_el in entry.findall("atom:author", ns):
                name = (author_el.findtext("atom:name", "", ns) or "").strip()
                if name:
                    parts = name.rsplit(" ", 1)
                    authors.append({
                        "creatorType": "author",
                        "firstName": parts[0] if len(parts) > 1 else "",
                        "lastName": parts[-1],
                    })

            return PaperMetadata(
                doi=doi,
                title=title,
                item_type="preprint",
                oa_url=f"https://arxiv.org/pdf/{clean_id}",
                arxiv_id=clean_id,
                authors=authors,
                year=year,
                abstract=abstract,
                journal="arXiv",
                url=f"https://arxiv.org/abs/{clean_id}",
            )
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Failed to parse arXiv response: {e}")

    def _resolve_s2(self, s2_id: str) -> PaperMetadata:
        """Fetch metadata from Semantic Scholar graph API."""
        try:
            resp = httpx.get(
                f"https://api.semanticscholar.org/graph/v1/paper/{s2_id}",
                params={"fields": "title,authors,year,externalIds,abstract,openAccessPdf,journal"},
                timeout=15.0,
            )
            if resp.status_code == 404:
                raise ToolError(f"Paper not found on Semantic Scholar: {s2_id!r}")
            resp.raise_for_status()
            data = resp.json()
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"Semantic Scholar lookup failed: {e}")

        ext_ids = data.get("externalIds") or {}
        doi = ext_ids.get("DOI")
        arxiv_id = ext_ids.get("ArXiv")

        # Prefer richer CrossRef metadata when DOI is available
        if doi:
            try:
                return self._resolve_doi(doi)
            except ToolError:
                pass

        authors = [
            {
                "creatorType": "author",
                "firstName": "",
                "lastName": a.get("name") or "",
            }
            for a in (data.get("authors") or [])
        ]

        oa_pdf = data.get("openAccessPdf") or {}
        oa_url = oa_pdf.get("url")
        if not oa_url and arxiv_id:
            oa_url = f"https://arxiv.org/pdf/{arxiv_id}"

        journal_data = data.get("journal") or {}
        return PaperMetadata(
            doi=doi,
            title=data.get("title") or s2_id,
            item_type="preprint" if arxiv_id else "journalArticle",
            oa_url=oa_url,
            arxiv_id=arxiv_id,
            authors=authors,
            year=data.get("year"),
            abstract=data.get("abstract"),
            journal=journal_data.get("name"),
            volume=journal_data.get("volume"),
            pages=journal_data.get("pages"),
        )
