"""OpenAlex academic search provider."""
from __future__ import annotations

import logging

from ...openalex_client import OpenAlexClient
from ...tools.ingestion.search import (
    fetch_openalex_by_doi,
    format_openalex_paper,
    reconstruct_abstract,
    search_openalex,
    _mark_top_venue_relative,
    _resolve_names_to_ids,
)
from . import AcademicSearchResult, register_academic_search_provider

logger = logging.getLogger(__name__)


class OpenAlexSearchProvider:
    """Search provider for OpenAlex."""

    name = "openalex"

    def __init__(self, email: str | None = None) -> None:
        self._client = OpenAlexClient(email=email)

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
        data = search_openalex(
            query,
            limit,
            year_min,
            year_max,
            sort_by,
            client=self._client,
            min_citations=min_citations,
            concept_ids=concept_ids,
            institution_ids=institution_ids,
            source_id=source_id,
            oa_only=oa_only,
            cursor=cursor,
        )
        return AcademicSearchResult(
            results=data.get("results", []),
            next_cursor=data.get("next_cursor"),
            total_count=data.get("total_count", 0),
        )

    def get_by_doi(self, doi: str) -> list[dict]:
        return fetch_openalex_by_doi(doi, client=self._client)


register_academic_search_provider("openalex", OpenAlexSearchProvider)
