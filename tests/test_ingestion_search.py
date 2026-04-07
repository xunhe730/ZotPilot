"""Tests for OpenAlex ingestion search helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from zotpilot.openalex_client import WORK_SEARCH_SELECT, OpenAlexClient
from zotpilot.tools.ingestion_search import (
    _is_fuzzy_nl_query,
    fetch_openalex_by_doi,
    format_openalex_paper,
)


def _mock_response(payload: dict, *, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    return response


def _openalex_paper(*, primary_location: dict | None = None, cited_by_count: int = 42) -> dict:
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
    from zotpilot.tools.ingestion_search import is_doi_query

    assert is_doi_query("10.1126/science.aaw4741") == "10.1126/science.aaw4741"
    assert is_doi_query("doi:10.1126/science.aaw4741") == "10.1126/science.aaw4741"
    assert is_doi_query("https://doi.org/10.1126/science.aaw4741") == "10.1126/science.aaw4741"
    assert is_doi_query("AI flow field reconstruction") is None


def test_fuzzy_nl_query_emits_missing_priming_warning():
    """Hybrid enforcement: server warns when SKILL SOP not followed."""
    assert _is_fuzzy_nl_query("AI flow field reconstruction") is True
    assert _is_fuzzy_nl_query("10.1126/science.aaw4741") is False
    assert _is_fuzzy_nl_query("author:Raissi | flow") is False
    assert _is_fuzzy_nl_query('"flow field" AND (neural OR PINN)') is False
    assert _is_fuzzy_nl_query("CRISPR base editing") is True


def test_search_academic_databases_injects_warning_for_fuzzy_nl(monkeypatch):
    """Calling the MCP tool with fuzzy NL query attaches _warnings to first result."""
    from zotpilot.tools import ingestion_search

    fake_results = [{"id": "W1", "doi": "10.x/a", "title": "test"}]
    monkeypatch.setattr(
        ingestion_search,
        "search_openalex",
        lambda *a, **kw: [ingestion_search.format_openalex_paper(p) for p in fake_results],
    )
    out = ingestion_search.search_academic_databases_impl(
        config=MagicMock(openalex_email="t@e.com"),
        query="AI flow field reconstruction",
        limit=10,
        year_min=None,
        year_max=None,
        sort_by="relevance",
        high_quality=True,
        httpx_module=MagicMock(),
        tool_error_cls=Exception,
        logger=MagicMock(),
    )
    assert out[0].get("_warnings"), "expected _warnings on first result for fuzzy NL"
    assert out[0]["_warnings"][0]["code"] == "missing_priming"


def test_anchored_query_does_not_inject_warning(monkeypatch):
    from zotpilot.tools import ingestion_search

    fake_results = [{"id": "W1", "doi": "10.x/a", "title": "test"}]
    monkeypatch.setattr(
        ingestion_search,
        "search_openalex",
        lambda *a, **kw: [ingestion_search.format_openalex_paper(p) for p in fake_results],
    )
    out = ingestion_search.search_academic_databases_impl(
        config=MagicMock(openalex_email="t@e.com"),
        query="author:Raissi | flow field",
        limit=10,
        year_min=None,
        year_max=None,
        sort_by="relevance",
        high_quality=True,
        httpx_module=MagicMock(),
        tool_error_cls=Exception,
        logger=MagicMock(),
    )
    assert not out[0].get("_warnings"), "anchored query must not warn"


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
