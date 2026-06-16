"""Academic search provider registry and shared types."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass
class AcademicSearchResult:
    """Normalized result from any academic search provider."""

    results: list[dict]
    next_cursor: str | None = None
    total_count: int = 0
    unresolved_filters: list[str] = field(default_factory=list)


class AcademicSearchProvider(Protocol):
    """Interface for academic database search providers."""

    name: str

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
    ) -> AcademicSearchResult: ...

    def get_by_doi(self, doi: str) -> list[dict]: ...


ACADEMIC_SEARCH_PROVIDERS: dict[str, type] = {}


def register_academic_search_provider(name: str, cls: type) -> None:
    ACADEMIC_SEARCH_PROVIDERS[name] = cls


def _ensure_providers_registered() -> None:
    """Lazily import and register all providers."""
    if ACADEMIC_SEARCH_PROVIDERS:
        return
    from . import openalex_provider  # noqa: F401
    from . import pubmed_provider  # noqa: F401


def create_academic_search_provider(name: str, **kwargs) -> AcademicSearchProvider:
    _ensure_providers_registered()
    try:
        cls = ACADEMIC_SEARCH_PROVIDERS[name]
    except KeyError:
        valid = ", ".join(sorted(ACADEMIC_SEARCH_PROVIDERS))
        raise ValueError(f"Unknown academic search provider {name!r}. Valid: {valid}")
    return cls(**kwargs)


def merge_results(
    provider_results: list[AcademicSearchResult],
    *,
    limit: int = 20,
) -> AcademicSearchResult:
    """Merge and deduplicate results from multiple providers by DOI."""
    seen_dois: set[str] = set()
    merged: list[dict] = []
    total_count = 0

    for pr in provider_results:
        total_count += pr.total_count
        for paper in pr.results:
            doi = (paper.get("doi") or "").lower().strip()
            if doi and doi in seen_dois:
                continue
            if doi:
                seen_dois.add(doi)
            merged.append(paper)

    merged.sort(key=lambda p: p.get("relevance_score") or 0, reverse=True)
    return AcademicSearchResult(
        results=merged[:limit],
        total_count=total_count,
    )
