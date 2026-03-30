"""Tests for ingestion MCP tools."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from zotpilot.tools.ingestion import ingest_papers, save_from_url, search_academic_databases


def _make_config(*, preflight_enabled=False, openalex_email=None, zotero_api_key=None):
    config = MagicMock()
    config.preflight_enabled = preflight_enabled
    config.openalex_email = openalex_email
    config.zotero_api_key = zotero_api_key
    return config


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


def _mock_oa_response(data=None, status_code=200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = data or OA_RESPONSE
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


class TestSearchAcademicDatabases:
    def test_returns_formatted_list(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.httpx.get", return_value=_mock_oa_response()):
            results = search_academic_databases("attention mechanism")

        assert len(results) == 1
        result = results[0]
        assert result["title"] == "Attention Is All You Need"
        assert result["doi"] == "10.9999/attention"
        assert result["landing_page_url"] == "https://example.com/paper"

    def test_openalex_error_raises_tool_error(self):
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 503
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch(
                 "zotpilot.tools.ingestion.httpx.get",
                 side_effect=httpx.HTTPStatusError("service unavailable", request=MagicMock(), response=mock_resp),
             ):
            with pytest.raises(ToolError, match="http_503"):
                search_academic_databases("attention mechanism")

    def test_uses_openalex_email_as_mailto(self):
        with patch(
            "zotpilot.tools.ingestion._get_config",
            return_value=_make_config(openalex_email="me@example.com"),
        ), \
             patch("zotpilot.tools.ingestion.httpx.get", return_value=_mock_oa_response()) as mock_get:
            search_academic_databases("attention mechanism")

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["mailto"] == "me@example.com"


class TestIngestPapers:
    def test_over_50_raises_tool_error(self):
        papers = [{"doi": f"10.1000/{i}", "landing_page_url": f"https://example.com/{i}"} for i in range(51)]
        with pytest.raises(ToolError, match="50"):
            ingest_papers(papers)

    def test_invalid_json_raises_tool_error(self):
        with pytest.raises(ToolError, match="JSON array"):
            ingest_papers("not valid json")

    def test_defaults_to_inbox_collection(self):
        papers = [{"landing_page_url": "https://example.com/paper"}]
        with patch("zotpilot.tools.ingestion._ensure_inbox_collection", return_value="INBOX1"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.save_urls", return_value={
                 "results": [
                     {"success": True, "url": "https://example.com/paper", "item_key": "ITEM1", "title": "Paper"},
                 ],
             }) as save_urls_mock:
            result = ingest_papers(papers)

        assert save_urls_mock.call_args.kwargs["collection_key"] == "INBOX1"
        assert result["collection_used"] == "INBOX1"

    def test_explicit_collection_is_used(self):
        papers = [{"landing_page_url": "https://example.com/paper"}]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.save_urls", return_value={
                 "results": [
                     {"success": True, "url": "https://example.com/paper", "item_key": "ITEM1", "title": "Paper"},
                 ],
             }) as save_urls_mock:
            result = ingest_papers(papers, collection_key="COL1")

        assert save_urls_mock.call_args.kwargs["collection_key"] == "COL1"
        assert result["collection_used"] == "COL1"

    def test_no_identifier_marks_failed(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            result = ingest_papers([{"title": "Missing IDs"}])

        assert result["failed"] == 1
        assert result["results"][0]["status"] == "failed"
        assert "no usable identifier" in result["results"][0]["error"]

    def test_doi_only_marks_failed(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            result = ingest_papers([{"doi": "10.1000/test"}])

        assert result["failed"] == 1
        assert "DOI-only papers cannot be ingested" in result["results"][0]["error"]

    def test_arxiv_id_has_priority(self):
        papers = [{"doi": "10.1000/test", "arxiv_id": "2401.00001", "landing_page_url": "https://publisher.example/paper"}]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.save_urls", return_value={
                 "results": [
                     {
                         "success": True,
                         "url": "https://arxiv.org/abs/2401.00001",
                         "item_key": "ITEM1",
                         "title": "Paper",
                     },
                 ],
             }) as save_urls_mock:
            ingest_papers(papers)

        assert save_urls_mock.call_args.args[0] == ["https://arxiv.org/abs/2401.00001"]

    def test_landing_page_has_priority_over_doi(self):
        papers = [{"doi": "10.1000/test", "landing_page_url": "https://publisher.example/paper"}]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion.save_urls", return_value={
                 "results": [
                     {"success": True, "url": "https://publisher.example/paper", "item_key": "ITEM1", "title": "Paper"},
                 ],
             }) as save_urls_mock:
            ingest_papers(papers)

        assert save_urls_mock.call_args.args[0] == ["https://publisher.example/paper"]

    def test_local_doi_duplicate_skips_save(self):
        papers = [{"doi": "10.1000/existing", "landing_page_url": "https://publisher.example/paper"}]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()), \
             patch("zotpilot.tools.ingestion._lookup_local_item_key_by_doi", return_value="EXISTING1"), \
             patch("zotpilot.tools.ingestion.save_urls") as save_urls_mock:
            result = ingest_papers(papers)

        save_urls_mock.assert_not_called()
        assert result["duplicates"] == 1
        assert result["results"][0]["status"] == "duplicate"
        assert result["results"][0]["item_key"] == "EXISTING1"

    def test_preflight_uses_config_flag(self):
        papers = [{"landing_page_url": "https://publisher.example/paper"}]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=True)), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.preflight_urls", return_value={
                 "checked": 1,
                 "accessible": [],
                 "blocked": [],
                 "skipped": [],
                 "errors": [],
                 "all_clear": True,
             }) as preflight_mock, \
             patch("zotpilot.tools.ingestion.save_urls", return_value={
                 "results": [
                     {"success": True, "url": "https://publisher.example/paper", "item_key": "ITEM1", "title": "Paper"},
                 ],
             }):
            ingest_papers(papers)

        preflight_mock.assert_called_once()

    def test_preflight_can_be_disabled_in_config(self):
        papers = [{"landing_page_url": "https://publisher.example/paper"}]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=False)), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.preflight_urls") as preflight_mock, \
             patch("zotpilot.tools.ingestion.save_urls", return_value={
                 "results": [
                     {"success": True, "url": "https://publisher.example/paper", "item_key": "ITEM1", "title": "Paper"},
                 ],
             }):
            ingest_papers(papers)

        preflight_mock.assert_not_called()

    def test_preflight_blocked_returns_failed_results_and_arrays(self):
        papers = [{"landing_page_url": "https://publisher.example/paper"}]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=True)), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.preflight_urls", return_value={
                 "checked": 1,
                 "accessible": [],
                 "blocked": [{"url": "https://publisher.example/paper", "error": "robot check"}],
                 "skipped": [],
                 "errors": [],
                 "all_clear": False,
             }), \
             patch("zotpilot.tools.ingestion.save_urls") as save_urls_mock:
            result = ingest_papers(papers)

        save_urls_mock.assert_not_called()
        assert result["failed"] == 1
        assert result["results"][0]["status"] == "failed"
        assert "blocked" not in result  # no top-level blocked/errors arrays

    def test_bridge_top_level_failure_marks_urls_failed(self):
        papers = [{"landing_page_url": "https://publisher.example/paper"}]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=False)), \
             patch("zotpilot.tools.ingestion.save_urls", return_value={
                 "success": False,
                 "error": "bridge down",
                 "results": [],
             }):
            result = ingest_papers(papers)

        assert result["failed"] == 1
        assert result["results"][0]["error"] == "bridge save failed"

    def test_missing_batch_result_marks_unmatched_url_failed(self):
        papers = [
            {"landing_page_url": "https://publisher.example/1"},
            {"landing_page_url": "https://publisher.example/2"},
        ]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=False)), \
             patch("zotpilot.tools.ingestion.save_urls", return_value={
                 "results": [
                     {"success": True, "url": "https://publisher.example/1", "item_key": "ITEM1", "title": "One"},
                 ],
             }):
            result = ingest_papers(papers)

        assert result["saved"] == 1
        assert result["failed"] == 1
        assert any(
            item["url"] == "https://publisher.example/2" and item["status"] == "failed"
            for item in result["results"]
        )

    def test_success_and_failure_are_mapped_to_simplified_statuses(self):
        papers = [
            {"landing_page_url": "https://publisher.example/1"},
            {"landing_page_url": "https://publisher.example/2"},
        ]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=False)), \
             patch("zotpilot.tools.ingestion.save_urls", return_value={
                 "results": [
                     {"success": True, "url": "https://publisher.example/1", "item_key": "ITEM1", "title": "One"},
                     {"success": False, "url": "https://publisher.example/2", "error": "translator failed"},
                 ],
             }):
            result = ingest_papers(papers)

        assert result["saved"] == 1
        assert result["failed"] == 1
        assert {item["status"] for item in result["results"]} == {"saved", "failed"}


class TestSaveFromUrl:
    def test_aliases_save_urls_and_preserves_collection_used(self):
        with patch("zotpilot.tools.ingestion.save_urls", return_value={
            "results": [{"success": True, "url": "https://example.com", "item_key": "ITEM1"}],
            "collection_used": "INBOX1",
        }) as save_urls_mock:
            result = save_from_url("https://example.com")

        save_urls_mock.assert_called_once_with(["https://example.com"], collection_key=None, tags=None)
        assert result["collection_used"] == "INBOX1"
