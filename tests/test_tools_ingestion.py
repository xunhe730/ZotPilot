"""Tests for ingestion MCP tools."""
import pytest
from unittest.mock import MagicMock, patch, call

from fastmcp.exceptions import ToolError

from zotpilot.tools.ingestion import (
    add_paper_by_identifier,
    search_academic_databases,
    ingest_papers,
)
from zotpilot.identifier_resolver import PaperMetadata


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


class TestSearchAcademicDatabases:
    def _mock_s2_response(self, data=None):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = data or S2_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def test_returns_formatted_list(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = self._mock_s2_response()
            results = search_academic_databases("attention mechanism")

        assert len(results) == 1
        r = results[0]
        assert r["title"] == "Attention Is All You Need"
        assert r["doi"] == "10.9999/attention"
        assert r["arxiv_id"] == "1706.03762"
        assert r["cited_by_count"] == 50000
        assert r["year"] == 2017
        assert isinstance(r["authors"], list)
        assert len(r["authors"]) == 2

    def test_abstract_snippet_truncated_at_300(self):
        long_abstract = "x" * 500
        data = {"data": [{**S2_RESPONSE["data"][0], "abstract": long_abstract}]}
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = self._mock_s2_response(data)
            results = search_academic_databases("test")

        assert len(results[0]["abstract_snippet"]) == 300

    def test_with_api_key_sets_header(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key="MY_KEY")), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = self._mock_s2_response()
            search_academic_databases("test")

        _, kwargs = mock_get.call_args
        assert kwargs.get("headers", {}).get("x-api-key") == "MY_KEY"

    def test_without_api_key_no_header(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key=None)), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = self._mock_s2_response()
            search_academic_databases("test")

        _, kwargs = mock_get.call_args
        assert "x-api-key" not in kwargs.get("headers", {})

    def test_year_min_max_sets_param(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = self._mock_s2_response()
            search_academic_databases("test", year_min=2020, year_max=2023)

        _, kwargs = mock_get.call_args
        params = kwargs.get("params", {})
        assert "publicationDateOrYear" in params
        assert "2020" in params["publicationDateOrYear"]
        assert "2023" in params["publicationDateOrYear"]

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
        many_authors = [{"name": f"Author{i}"} for i in range(10)]
        data = {"data": [{**S2_RESPONSE["data"][0], "authors": many_authors}]}
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.httpx.get") as mock_get:
            mock_get.return_value = self._mock_s2_response(data)
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
