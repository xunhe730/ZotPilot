"""Tests for OpenAlex ingestion search helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from zotpilot.openalex_client import WORK_SEARCH_SELECT, OpenAlexClient
from zotpilot.tools.ingestion.search import (
    _is_fuzzy_nl_query,
    annotate_local_duplicate,
    fetch_openalex_by_doi,
    format_openalex_paper,
)


def _mock_response(payload: dict, *, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    return response


def _openalex_paper(
    *,
    primary_location: dict | None = None,
    best_oa_location: dict | None = None,
    open_access: dict | None = None,
    ids: dict | None = None,
    cited_by_count: int = 42,
) -> dict:
    return {
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1000/test",
        "display_name": "Test Paper",
        "publication_year": 2024,
        "cited_by_count": cited_by_count,
        "type": "article",
        "is_retracted": False,
        "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
        "primary_location": primary_location,
        "best_oa_location": best_oa_location,
        "open_access": open_access,
        "ids": ids,
        "abstract_inverted_index": {"Test": [0], "paper": [1]},
    }


def test_format_openalex_paper_extracts_venue_signals():
    paper = _openalex_paper(
        primary_location={
            "landing_page_url": "https://example.com/paper",
            "source": {
                "display_name": "Journal of Tests",
                "summary_stats": {"h_index": 120, "2yr_mean_citedness": 7.5},
            },
        },
        cited_by_count=55,
    )

    result = format_openalex_paper(paper)

    assert result["cited_by_count"] == 55
    assert result["venue"] == {
        "display_name": "Journal of Tests",
        "h_index": 120,
        "two_yr_mean_citedness": 7.5,
    }
    assert result["top_venue"] is True
    assert result["landing_page_url"] == "https://example.com/paper"


def test_format_openalex_paper_handles_null_summary_stats():
    no_primary = format_openalex_paper(_openalex_paper(primary_location=None, cited_by_count=12))
    no_stats = format_openalex_paper(
        _openalex_paper(
            primary_location={
                "landing_page_url": "https://example.com/paper",
                "source": {"display_name": "Journal of Tests", "summary_stats": None},
            },
            cited_by_count=12,
        )
    )

    for result in (no_primary, no_stats):
        assert result["venue"] == {
            "display_name": None if result is no_primary else "Journal of Tests",
            "h_index": None,
            "two_yr_mean_citedness": None,
        }
        assert result["top_venue"] is False


def test_format_openalex_paper_backfills_arxiv_and_oa_fields():
    result = format_openalex_paper(
        _openalex_paper(
            primary_location={
                "landing_page_url": "https://publisher.example/paper",
                "source": {"display_name": "Journal of Tests", "summary_stats": None},
            },
            open_access={"is_oa": False, "oa_url": None},
            best_oa_location={
                "landing_page_url": "https://arxiv.org/abs/2301.00001v2",
                "source": {"display_name": "arXiv"},
            },
        )
    )

    assert result["arxiv_id"] == "2301.00001"
    assert result["is_oa"] is True
    assert result["is_oa_published"] is False
    assert result["oa_url"] == "https://arxiv.org/abs/2301.00001v2"
    assert result["oa_host"] == "arXiv"


def test_search_works_uses_top_level_search_param(monkeypatch):
    """Regression guard: query goes to ?search= not ?filter=title_and_abstract.search:"""
    captured = {}

    def fake_get(url, params=None, **kw):
        captured["params"] = params
        return _mock_response({"results": []})

    monkeypatch.setattr("zotpilot.openalex_client.httpx.get", fake_get)
    OpenAlexClient(email="t@e.com").search_works("transformers", per_page=5)
    assert captured["params"].get("search") == "transformers"
    filter_value = captured["params"].get("filter") or ""
    assert "title_and_abstract.search" not in filter_value


def test_no_query_omits_search_param(monkeypatch):
    captured = {}

    def fake_get(url, params=None, **kw):
        captured["params"] = params
        return _mock_response({"results": []})

    monkeypatch.setattr("zotpilot.openalex_client.httpx.get", fake_get)
    OpenAlexClient(email="t@e.com").search_works(None, per_page=5)
    assert "search" not in captured["params"]


def test_doi_query_routes_to_direct_fetch():
    """DOI direct lookup is already supported — verify wrapper works."""
    from zotpilot.tools.ingestion.search import is_doi_query

    assert is_doi_query("10.1126/science.aaw4741") == "10.1126/science.aaw4741"
    assert is_doi_query("doi:10.1126/science.aaw4741") == "10.1126/science.aaw4741"
    assert is_doi_query("https://doi.org/10.1126/science.aaw4741") == "10.1126/science.aaw4741"
    assert is_doi_query("AI flow field reconstruction") is None


def test_fuzzy_nl_query_emits_missing_priming_warning():
    """Low-level search warns when a structured query plan is missing."""
    assert _is_fuzzy_nl_query("AI flow field reconstruction") is True
    assert _is_fuzzy_nl_query("10.1126/science.aaw4741") is False
    assert _is_fuzzy_nl_query("author:Raissi | flow") is False
    assert _is_fuzzy_nl_query('"flow field" AND (neural OR PINN)') is False
    assert _is_fuzzy_nl_query("CRISPR base editing") is True


def test_search_academic_databases_rejects_fuzzy_nl_without_filters():
    """Fuzzy NL queries without structured filters must raise — hard guardrail."""
    from zotpilot.tools.ingestion import search as ingestion_search

    class _Err(Exception):
        pass

    with pytest.raises(_Err) as excinfo:
        ingestion_search.search_academic_databases_impl(
            config=MagicMock(openalex_email="t@e.com"),
            query="AI flow field reconstruction",
            limit=10,
            year_min=None,
            year_max=None,
            sort_by="relevance",
            httpx_module=MagicMock(),
            tool_error_cls=_Err,
            logger=MagicMock(),
        )
    msg = str(excinfo.value)
    assert "Fuzzy bag-of-words query rejected" in msg
    assert "DOI direct" in msg and "Quoted phrase" in msg


def test_search_academic_databases_accepts_fuzzy_with_concept_filter(monkeypatch):
    """Concept filter narrows the search enough that fuzzy keywords are allowed."""
    from zotpilot.tools.ingestion import search as ingestion_search

    fake_payload = {
        "results": [ingestion_search.format_openalex_paper({"id": "W1", "doi": "10.x/a", "title": "test"})],
        "next_cursor": "abc",
        "total_count": 42,
    }
    monkeypatch.setattr(ingestion_search, "search_openalex", lambda *a, **kw: fake_payload)
    monkeypatch.setattr(
        ingestion_search.OpenAlexClient, "resolve_concept",
        lambda self, name: "https://openalex.org/C41008148",
    )
    out = ingestion_search.search_academic_databases_impl(
        config=MagicMock(openalex_email="t@e.com"),
        query="instruction tuning",  # fuzzy but concept-anchored
        limit=10,
        year_min=None,
        year_max=None,
        sort_by="relevance",
        httpx_module=MagicMock(),
        tool_error_cls=Exception,
        logger=MagicMock(),
        concepts=["Computer vision"],
    )
    assert out["results"], "expected results when concept filter is provided"
    assert out["total_count"] == 42
    assert out["next_cursor"] == "abc"
    assert out["unresolved_filters"] == []


def test_anchored_query_passes_through_without_error(monkeypatch):
    """Author-anchored queries bypass fuzzy rejection and return dict payload."""
    from zotpilot.tools.ingestion import search as ingestion_search

    fake_payload = {
        "results": [ingestion_search.format_openalex_paper({"id": "W1", "doi": "10.x/a", "title": "test"})],
        "next_cursor": None,
        "total_count": 1,
    }
    monkeypatch.setattr(ingestion_search, "search_openalex", lambda *a, **kw: fake_payload)
    out = ingestion_search.search_academic_databases_impl(
        config=MagicMock(openalex_email="t@e.com"),
        query="author:Raissi | flow field",
        limit=10,
        year_min=None,
        year_max=None,
        sort_by="relevance",
        httpx_module=MagicMock(),
        tool_error_cls=Exception,
        logger=MagicMock(),
    )
    assert "results" in out and "next_cursor" in out and "total_count" in out
    assert out["unresolved_filters"] == []


def test_search_academic_databases_impl_returns_duplicate_contract(monkeypatch):
    from zotpilot.tools.ingestion import search as ingestion_search

    fake_payload = {
        "results": [
            ingestion_search.format_openalex_paper(
                {"id": "W1", "doi": "10.1234/a", "display_name": "test"}
            )
        ],
        "next_cursor": None,
        "total_count": 1,
    }
    monkeypatch.setattr(ingestion_search, "search_openalex", lambda *a, **kw: fake_payload)

    out = ingestion_search.search_academic_databases_impl(
        config=MagicMock(openalex_email="t@e.com"),
        query='"test"',
        limit=10,
        year_min=None,
        year_max=None,
        sort_by="relevance",
        httpx_module=MagicMock(),
        tool_error_cls=Exception,
        logger=MagicMock(),
        lookup_by_doi=lambda doi: "ITEM1" if doi == "10.1234/a" else None,
        lookup_by_arxiv_extra=lambda arxiv_id: None,
    )

    assert out["results"][0]["local_duplicate"] is True
    assert out["results"][0]["existing_item_key"] == "ITEM1"


def test_annotate_local_duplicate_doi_hit():
    result = annotate_local_duplicate(
        {"doi": "10.1000/test", "arxiv_id": None},
        lookup_by_doi=lambda doi: "ITEM1" if doi == "10.1000/test" else None,
        lookup_by_arxiv_extra=lambda arxiv_id: None,
    )

    assert result["local_duplicate"] is True
    assert result["existing_item_key"] == "ITEM1"


def test_annotate_local_duplicate_arxiv_extra_hit():
    result = annotate_local_duplicate(
        {"doi": None, "arxiv_id": "2301.00001v2"},
        lookup_by_doi=lambda doi: None,
        lookup_by_arxiv_extra=lambda arxiv_id: "ITEM2" if arxiv_id == "2301.00001" else None,
    )

    assert result["local_duplicate"] is True
    assert result["existing_item_key"] == "ITEM2"


def test_annotate_local_duplicate_zotero_unavailable_silent():
    result = annotate_local_duplicate(
        {"doi": "10.1000/test", "arxiv_id": "2301.00001"},
        lookup_by_doi=lambda doi: (_ for _ in ()).throw(RuntimeError("locked")),
        lookup_by_arxiv_extra=lambda arxiv_id: (_ for _ in ()).throw(RuntimeError("locked")),
    )

    assert result["local_duplicate"] is False
    assert result["existing_item_key"] is None


def test_doi_fetch_returns_enriched_payload():
    client = OpenAlexClient(email="me@example.com")
    payload = _openalex_paper(
        primary_location={
            "landing_page_url": "https://example.com/paper",
            "source": {
                "display_name": "High Impact Journal",
                "summary_stats": {"h_index": 90, "2yr_mean_citedness": 4.2},
            },
        },
        cited_by_count=510,
    )

    with patch("zotpilot.openalex_client.httpx.get", return_value=_mock_response(payload)) as mock_get:
        results = fetch_openalex_by_doi("10.1000/test", client)

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["select"] == WORK_SEARCH_SELECT
    assert results[0]["venue"] == {
        "display_name": "High Impact Journal",
        "h_index": 90,
        "two_yr_mean_citedness": 4.2,
    }
    assert results[0]["top_venue"] is True
