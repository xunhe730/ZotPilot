"""Tests for ingestion MCP tools."""
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from zotpilot.identifier_resolver import PaperMetadata
from zotpilot.tools.ingestion import (
    add_paper_by_identifier,
    ingest_papers,
    search_academic_databases,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_metadata(**kwargs):
    defaults = dict(
        doi="10.1038/test",
        title="Test Paper",
        item_type="journalArticle",
        oa_url="https://example.com/paper.pdf",
        arxiv_id=None,
        authors=[{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
        year=2023,
    )
    defaults.update(kwargs)
    return PaperMetadata(**defaults)


def _make_writer(duplicate_key=None, create_key="NEWKEY1"):
    writer = MagicMock()
    writer.check_duplicate_by_doi.return_value = duplicate_key
    writer.create_item_from_metadata.return_value = {"success": {"0": create_key}}
    writer.try_attach_oa_pdf.return_value = "attached"
    return writer


def _make_resolver(metadata=None):
    resolver = MagicMock()
    resolver.resolve.return_value = metadata or _make_metadata()
    resolver.last_crossref_metadata = None
    return resolver


def _make_config(api_key=None):
    config = MagicMock()
    config.semantic_scholar_api_key = api_key
    return config


# ---------------------------------------------------------------------------
# add_paper_by_identifier
# ---------------------------------------------------------------------------

class TestAddPaperByIdentifier:
    def test_new_paper_creates_item(self):
        resolver = _make_resolver()
        writer = _make_writer(duplicate_key=None, create_key="ABC123")

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            result = add_paper_by_identifier("10.1038/test")

        assert result["success"] is True
        assert result["duplicate"] is False
        assert result["item_key"] == "ABC123"
        assert result["title"] == "Test Paper"
        writer.create_item_from_metadata.assert_called_once()

    def test_duplicate_doi_returns_existing_key(self):
        resolver = _make_resolver()
        writer = _make_writer(duplicate_key="EXISTING1")

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            result = add_paper_by_identifier("10.1038/test")

        assert result["duplicate"] is True
        assert result["existing_key"] == "EXISTING1"
        writer.create_item_from_metadata.assert_not_called()

    def test_attach_pdf_true_calls_try_attach(self):
        resolver = _make_resolver()
        writer = _make_writer()

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            result = add_paper_by_identifier("10.1038/test", attach_pdf=True)

        writer.try_attach_oa_pdf.assert_called_once()
        assert result["pdf"] == "attached"

    def test_attach_pdf_false_skips_try_attach(self):
        resolver = _make_resolver()
        writer = _make_writer()

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            result = add_paper_by_identifier("10.1038/test", attach_pdf=False)

        writer.try_attach_oa_pdf.assert_not_called()
        assert result["pdf"] == "skipped"

    def test_unknown_identifier_raises_tool_error(self):
        resolver = MagicMock()
        resolver.resolve.side_effect = ToolError("Unrecognized identifier format")
        writer = _make_writer()

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            with pytest.raises(ToolError, match="Unrecognized"):
                add_paper_by_identifier("not-a-real-id")

    def test_no_doi_skips_duplicate_check(self):
        metadata = _make_metadata(doi=None, arxiv_id="2301.00001")
        resolver = _make_resolver(metadata=metadata)
        writer = _make_writer()

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            result = add_paper_by_identifier("arxiv:2301.00001")

        writer.check_duplicate_by_doi.assert_not_called()
        assert result["duplicate"] is False

    def test_collection_key_passed_to_create(self):
        resolver = _make_resolver()
        writer = _make_writer()

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            add_paper_by_identifier("10.1038/test", collection_key="COL1")

        _, kwargs = writer.create_item_from_metadata.call_args
        assert kwargs.get("collection_keys") == ["COL1"]

    def test_tags_passed_to_create(self):
        resolver = _make_resolver()
        writer = _make_writer()

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            add_paper_by_identifier("10.1038/test", tags=["ml", "nlp"])

        _, kwargs = writer.create_item_from_metadata.call_args
        assert kwargs.get("tags") == ["ml", "nlp"]

    def test_create_failure_raises_tool_error(self):
        resolver = _make_resolver()
        writer = _make_writer()
        writer.create_item_from_metadata.return_value = {"failed": {"0": "error"}}

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            with pytest.raises(ToolError, match="Failed to create"):
                add_paper_by_identifier("10.1038/test")


# ---------------------------------------------------------------------------
# search_academic_databases
# ---------------------------------------------------------------------------

S2_RESPONSE = {
    "data": [
        {
            "paperId": "abc123",
            "title": "Attention Is All You Need",
            "authors": [{"name": "Vaswani"}, {"name": "Shazeer"}],
            "year": 2017,
            "externalIds": {"DOI": "10.9999/attention", "ArXiv": "1706.03762"},
            "citationCount": 50000,
            "abstract": "We propose the Transformer architecture based on attention mechanisms.",
        }
    ]
}

OA_RESPONSE = {
    "results": [
        {
            "id": "https://openalex.org/W1234567890",
            "doi": "https://doi.org/10.9999/attention",
            "display_name": "Attention Is All You Need",
            "authorships": [
                {"author": {"display_name": "Vaswani"}},
                {"author": {"display_name": "Shazeer"}},
            ],
            "publication_year": 2017,
            "cited_by_count": 50000,
            "open_access": {"is_oa": False, "oa_url": None},
            "abstract_inverted_index": {
                "We": [0], "propose": [1], "the": [2], "Transformer": [3],
            },
            "ids": {"doi": "https://doi.org/10.9999/attention"},
            "primary_location": {"landing_page_url": "https://example.com/paper"},
        }
    ]
}


class TestSearchAcademicDatabases:
    def _mock_oa_response(self, data=None):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = data or OA_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def _mock_s2_response(self, data=None):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = data or S2_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def test_returns_formatted_list(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = self._mock_oa_response()
            results = search_academic_databases("attention mechanism")

        assert len(results) == 1
        r = results[0]
        assert r["title"] == "Attention Is All You Need"
        assert r["doi"] == "10.9999/attention"
        assert r["cited_by_count"] == 50000
        assert r["year"] == 2017
        assert isinstance(r["authors"], list)
        assert len(r["authors"]) == 2

    def test_abstract_snippet_truncated_at_300(self):
        # Build an OA result with a long abstract via inverted index
        long_word = "x"
        long_inverted = {long_word: list(range(500))}
        oa_result = {**OA_RESPONSE["results"][0], "abstract_inverted_index": long_inverted}
        data = {"results": [oa_result]}
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = self._mock_oa_response(data)
            results = search_academic_databases("test")

        assert len(results[0]["abstract_snippet"]) == 300

    def test_with_api_key_sets_header(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key="MY_KEY")), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            # First call (OA) returns empty; second call (S2) returns S2_RESPONSE
            mock_get.side_effect = [self._mock_oa_response({"results": []}), self._mock_s2_response()]
            search_academic_databases("test")

        # Last call is S2 — check its header
        _, kwargs = mock_get.call_args
        assert kwargs.get("headers", {}).get("x-api-key") == "MY_KEY"

    def test_without_api_key_no_header(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key=None)), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = self._mock_oa_response()
            search_academic_databases("test")

        # Only one call (OA) — no x-api-key header
        _, kwargs = mock_get.call_args
        assert "x-api-key" not in kwargs.get("headers", {})

    def test_year_min_max_sets_param(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = self._mock_oa_response()
            search_academic_databases("test", year_min=2020, year_max=2023)

        # OA uses filter param with publication_year range (year_min-1 / year_max+1)
        _, kwargs = mock_get.call_args
        params = kwargs.get("params", {})
        assert "filter" in params
        assert "publication_year" in params["filter"]
        assert "2019" in params["filter"]  # year_min - 1
        assert "2024" in params["filter"]  # year_max + 1

    def test_timeout_raises_tool_error(self):
        import httpx as _httpx
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.side_effect = _httpx.TimeoutException("timeout")
            with pytest.raises(ToolError, match="timeout"):
                search_academic_databases("test")

    def test_http_error_raises_tool_error(self):
        import httpx as _httpx
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 429
            mock_get.side_effect = _httpx.HTTPStatusError(
                "Too many requests", request=MagicMock(), response=mock_resp
            )
            with pytest.raises(ToolError, match="429"):
                search_academic_databases("test")

    def test_authors_capped_at_five(self):
        many_authors = [{"author": {"display_name": f"Author{i}"}} for i in range(10)]
        oa_result = {**OA_RESPONSE["results"][0], "authorships": many_authors}
        data = {"results": [oa_result]}
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = self._mock_oa_response(data)
            results = search_academic_databases("test")

        assert len(results[0]["authors"]) == 5


# ---------------------------------------------------------------------------
# ingest_papers
# ---------------------------------------------------------------------------

class TestIngestPapers:
    def test_over_50_raises_tool_error(self):
        papers = [{"doi": f"10.9999/{i}"} for i in range(51)]
        with pytest.raises(ToolError, match="50"):
            ingest_papers(papers)

    def test_exactly_50_accepted(self):
        papers = [{"doi": f"10.9999/{i}"} for i in range(50)]
        resolver = _make_resolver()
        writer = _make_writer()
        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            result = ingest_papers(papers)
        assert result["total"] == 50

    def test_no_api_key_large_batch_has_warning(self):
        papers = [{"doi": f"10.9999/{i}"} for i in range(10)]
        resolver = _make_resolver()
        writer = _make_writer()
        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key=None)):
            result = ingest_papers(papers)
        assert result["warning"] is not None
        assert "S2_API_KEY" in result["warning"]

    def test_with_api_key_no_warning(self):
        papers = [{"doi": f"10.9999/{i}"} for i in range(10)]
        resolver = _make_resolver()
        writer = _make_writer()
        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key="KEY")):
            result = ingest_papers(papers)
        assert result["warning"] is None

    def test_identifier_priority_doi_over_arxiv(self):
        papers = [{"doi": "10.1038/test", "arxiv_id": "2301.00001", "s2_id": "abc" * 14}]
        resolver = _make_resolver()
        writer = _make_writer()
        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            ingest_papers(papers)

        called_id = resolver.resolve.call_args[0][0]
        assert called_id == "10.1038/test"

    def test_identifier_priority_arxiv_over_s2(self):
        papers = [{"arxiv_id": "2301.00001", "s2_id": "a" * 40}]
        resolver = _make_resolver(_make_metadata(doi=None, arxiv_id="2301.00001"))
        writer = _make_writer()
        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            ingest_papers(papers)

        called_id = resolver.resolve.call_args[0][0]
        assert called_id == "arxiv:2301.00001"

    def test_no_identifier_counted_as_failed(self):
        papers = [{"title": "Some paper with no ID"}]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            result = ingest_papers(papers)

        assert result["failed"] == 1
        assert result["ingested"] == 0
        assert "no usable identifier" in result["results"][0]["error"]

    def test_failure_does_not_abort_batch(self):
        papers = [
            {"doi": "10.1038/good"},
            {"doi": "10.1038/bad"},
            {"doi": "10.1038/good2"},
        ]
        resolver = MagicMock()
        resolver.resolve.side_effect = [
            _make_metadata(doi="10.1038/good"),
            ToolError("DOI not found"),
            _make_metadata(doi="10.1038/good2"),
        ]
        resolver.last_crossref_metadata = None
        writer = _make_writer()

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            result = ingest_papers(papers)

        assert result["total"] == 3
        assert result["ingested"] == 2
        assert result["failed"] == 1

    def test_skip_duplicates_counted(self):
        papers = [{"doi": "10.1038/existing"}]
        resolver = _make_resolver()
        writer = _make_writer(duplicate_key="EXISTINGKEY")

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            result = ingest_papers(papers, skip_duplicates=True)

        assert result["skipped_duplicates"] == 1
        assert result["ingested"] == 0

    def test_skip_duplicates_false_counts_as_ingested(self):
        papers = [{"doi": "10.1038/existing"}]
        resolver = _make_resolver()
        writer = _make_writer(duplicate_key="EXISTINGKEY")

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            result = ingest_papers(papers, skip_duplicates=False)

        # duplicate=True but skip_duplicates=False → counted as ingested
        assert result["ingested"] == 1
        assert result["skipped_duplicates"] == 0

    def test_results_list_has_entry_per_paper(self):
        papers = [{"doi": "10.1038/a"}, {"doi": "10.1038/b"}]
        resolver = _make_resolver()
        writer = _make_writer()

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            result = ingest_papers(papers)

        assert len(result["results"]) == 2

    def test_empty_batch_returns_zeros(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            result = ingest_papers([])

        assert result["total"] == 0
        assert result["ingested"] == 0
        assert result["failed"] == 0


# ---------------------------------------------------------------------------
# search_academic_databases v2 — OpenAlex-primary control flow
# ---------------------------------------------------------------------------

def _make_config_v2(s2_key=None, openalex_email=None):
    config = MagicMock()
    config.semantic_scholar_api_key = s2_key
    config.openalex_email = openalex_email
    return config


def _make_oa_http_response(data=None):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = data or OA_RESPONSE
    return mock_resp


def _make_s2_http_response(data=None):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = data or S2_RESPONSE
    return mock_resp


OA_PAPER = {
    "id": "https://openalex.org/W111",
    "doi": "https://doi.org/10.1234/abc",
    "display_name": "OA Paper",
    "authorships": [{"author": {"display_name": "Alice"}}],
    "publication_year": 2022,
    "cited_by_count": 10,
    "open_access": {"is_oa": True, "oa_url": "https://oa.example.com/paper.pdf"},
    "abstract_inverted_index": {"Hello": [0], "world": [1]},
    "ids": {"doi": "https://doi.org/10.1234/abc"},
    "primary_location": {"landing_page_url": "https://publisher.example.com/paper"},
}

S2_PAPER = {
    "paperId": "s2abc",
    "title": "OA Paper",
    "authors": [{"name": "Alice"}],
    "year": 2022,
    "externalIds": {"DOI": "10.1234/ABC"},
    "citationCount": 42,
    "abstract": "A great paper.",
}


class TestSearchAcademicDatabasesV2:
    def test_openalex_only_no_s2_key(self):
        """OA succeeds, no S2 key → returns OA results with OA fields."""
        config = _make_config_v2(s2_key=None)
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = _make_oa_http_response({"results": [OA_PAPER]})
            results = search_academic_databases("test")

        assert len(results) == 1
        r = results[0]
        assert r["is_oa"] is True
        assert r["oa_url"] == "https://oa.example.com/paper.pdf"
        assert r["landing_page_url"] == "https://publisher.example.com/paper"
        assert r["_source"] == "openalex"

    def test_openalex_with_s2_merge_same_doi(self):
        """OA and S2 both succeed with same DOI (different case) → single deduped result."""
        config = _make_config_v2(s2_key="KEY")
        oa_resp = _make_oa_http_response({"results": [OA_PAPER]})
        s2_resp = _make_s2_http_response({"data": [S2_PAPER]})
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.side_effect = [oa_resp, s2_resp]
            results = search_academic_databases("test")

        assert len(results) == 1
        r = results[0]
        assert r["s2_id"] == "s2abc"
        assert r["oa_url"] == "https://oa.example.com/paper.pdf"
        assert r["_source"] == "openalex"

    def test_openalex_with_s2_merge_doi_case_insensitive(self):
        """DOI matching is case-insensitive: '10.1234/TEST' and '10.1234/test' are same paper."""
        oa_paper = {**OA_PAPER, "doi": "https://doi.org/10.1234/TEST",
                    "ids": {"doi": "https://doi.org/10.1234/TEST"}}
        s2_paper = {**S2_PAPER, "externalIds": {"DOI": "10.1234/test"}}
        config = _make_config_v2(s2_key="KEY")
        oa_resp = _make_oa_http_response({"results": [oa_paper]})
        s2_resp = _make_s2_http_response({"data": [s2_paper]})
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.side_effect = [oa_resp, s2_resp]
            results = search_academic_databases("test")

        assert len(results) == 1
        assert results[0]["s2_id"] == "s2abc"

    def test_openalex_fails_s2_fallback_succeeds(self):
        """OA raises HTTPStatusError, S2 succeeds → S2 results with OA defaults."""
        import httpx as _httpx
        config = _make_config_v2(s2_key="KEY")
        mock_oa_resp = MagicMock()
        mock_oa_resp.status_code = 503
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.side_effect = [
                _httpx.HTTPStatusError("service unavailable", request=MagicMock(), response=mock_oa_resp),
                _make_s2_http_response(),
            ]
            results = search_academic_databases("test")

        assert len(results) == 1
        r = results[0]
        assert r["_source"] == "semantic_scholar"
        assert r["is_oa"] is False
        assert r["oa_url"] is None
        assert r["landing_page_url"] is None

    def test_both_fail_raises_tool_error(self):
        """Both OA and S2 fail → ToolError with both error messages."""
        import httpx as _httpx
        config = _make_config_v2(s2_key="KEY")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.side_effect = [
                _httpx.HTTPStatusError("OA error", request=MagicMock(), response=mock_resp),
                _httpx.HTTPStatusError("S2 error", request=MagicMock(), response=mock_resp),
            ]
            with pytest.raises(ToolError):
                search_academic_databases("test")

    def test_oa_fields_present_in_results(self):
        """OA result with open_access fields → is_oa, oa_url appear in returned dict."""
        config = _make_config_v2(s2_key=None)
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = _make_oa_http_response({"results": [OA_PAPER]})
            results = search_academic_databases("test")

        r = results[0]
        assert "is_oa" in r
        assert "oa_url" in r
        assert "landing_page_url" in r
        assert r["is_oa"] is True
        assert r["oa_url"] == "https://oa.example.com/paper.pdf"

    def test_openalex_email_config_used_as_mailto(self):
        """config.openalex_email is passed as mailto param in the OA request."""
        config = _make_config_v2(s2_key=None, openalex_email="myemail@institution.edu")
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = _make_oa_http_response({"results": []})
            search_academic_databases("test")

        _, kwargs = mock_get.call_args
        assert kwargs.get("params", {}).get("mailto") == "myemail@institution.edu"


# ---------------------------------------------------------------------------
# _enrich_oa_url
# ---------------------------------------------------------------------------

class TestEnrichOaUrl:
    def test_returns_oa_url_when_present(self):
        from zotpilot.tools.ingestion import _enrich_oa_url

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"open_access": {"is_oa": True, "oa_url": "https://oa.example.com/paper.pdf"}}

        with patch("zotpilot.tools.ingestion.httpx.get", return_value=mock_resp):
            result = _enrich_oa_url("10.1234/test")

        assert result == "https://oa.example.com/paper.pdf"

    def test_returns_none_on_404(self):
        from zotpilot.tools.ingestion import _enrich_oa_url
        import httpx as _httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("zotpilot.tools.ingestion.httpx.get", return_value=mock_resp):
            result = _enrich_oa_url("10.1234/nonexistent")

        assert result is None

    def test_returns_none_on_network_error(self):
        from zotpilot.tools.ingestion import _enrich_oa_url
        import httpx as _httpx

        with patch("zotpilot.tools.ingestion.httpx.get", side_effect=_httpx.TimeoutException("timeout")):
            result = _enrich_oa_url("10.1234/test")

        assert result is None

    def test_skipped_when_oa_url_already_set(self):
        """add_paper_by_identifier should NOT call _enrich_oa_url when metadata.oa_url is already set."""
        metadata = _make_metadata(oa_url="https://already.set/paper.pdf", arxiv_id=None)
        resolver = _make_resolver(metadata=metadata)
        writer = _make_writer()

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._enrich_oa_url") as mock_enrich:
            add_paper_by_identifier("10.1038/test", attach_pdf=True)

        mock_enrich.assert_not_called()

    def test_enrichment_called_when_oa_url_missing(self):
        """add_paper_by_identifier should call _enrich_oa_url when oa_url is None and no arxiv_id."""
        metadata = _make_metadata(oa_url=None, arxiv_id=None)
        resolver = _make_resolver(metadata=metadata)
        writer = _make_writer()

        with patch("zotpilot.tools.ingestion._get_resolver", return_value=resolver), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._enrich_oa_url", return_value="https://enriched.url/paper.pdf") as mock_enrich:
            add_paper_by_identifier("10.1038/test", attach_pdf=True)

        mock_enrich.assert_called_once_with("10.1038/test")
