"""PubMed academic search provider."""
from __future__ import annotations

import logging

from .pubmed_client import PubMedClient
from . import AcademicSearchResult, register_academic_search_provider

logger = logging.getLogger(__name__)


class PubMedSearchProvider:
    """Search provider for PubMed via NCBI E-utilities."""

    name = "pubmed"

    def __init__(
        self,
        api_key: str | None = None,
        email: str | None = None,
    ) -> None:
        self._client = PubMedClient(api_key=api_key, email=email)

    def search(
        self,
        query: str,
        limit: int,
        year_min: int | None,
        year_max: int | None,
        sort_by: str,
        *,
        min_citations: int | None = None,
        oa_only: bool = False,
        concept_ids: list[str] | None = None,
        institution_ids: list[str] | None = None,
        source_id: str | None = None,
        cursor: str | None = None,
    ) -> AcademicSearchResult:
        sort_map = {
            "relevance": "relevance",
            "publicationDate": "pub_date",
            "citationCount": "relevance",
        }
        sort_val = sort_map.get(sort_by, "relevance")

        mindate = f"{year_min}/01/01" if year_min else None
        maxdate = f"{year_max}/12/31" if year_max else None

        pmids, total = self._client.esearch(
            query,
            retmax=limit,
            sort=sort_val,
            mindate=mindate,
            maxdate=maxdate,
        )

        articles = self._client.efetch(pmids)
        results = [_format_pubmed_article(a) for a in articles]
        return AcademicSearchResult(
            results=results,
            total_count=total,
        )

    def get_by_doi(self, doi: str) -> list[dict]:
        pmids, _ = self._client.esearch(f"{doi}[doi]", retmax=1)
        if not pmids:
            return []
        articles = self._client.efetch(pmids)
        return [_format_pubmed_article(a) for a in articles]


def _format_pubmed_article(article: dict) -> dict:
    """Format a PubMed article dict into ZotPilot's normalized result format."""
    return {
        "title": article.get("title"),
        "authors": article.get("authors", []),
        "year": article.get("year"),
        "doi": article.get("doi"),
        "pmid": article.get("pmid"),
        "arxiv_id": None,
        "openalex_id": None,
        "cited_by_count": 0,
        "is_retracted": False,
        "type": "article",
        "venue": {
            "display_name": article.get("journal"),
            "h_index": None,
            "two_yr_mean_citedness": None,
        },
        "top_venue": False,
        "abstract_snippet": (article.get("abstract") or "")[:300],
        "is_oa": False,
        "is_oa_published": False,
        "oa_url": None,
        "oa_host": None,
        "landing_page_url": None,
        "journal": article.get("journal"),
        "publisher": None,
        "needs_manual_verification": False,
        "relevance_score": None,
        "_source": "pubmed",
    }


register_academic_search_provider("pubmed", PubMedSearchProvider)
