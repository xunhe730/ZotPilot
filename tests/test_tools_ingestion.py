"""Tests for ingestion MCP tools."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from zotpilot.tools.ingestion import (
    _batch_store,
    _run_save_worker,
    get_ingest_status,
    ingest_papers,
    save_from_url,
    search_academic_databases,
)


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
                "We": [0],
                "propose": [1],
                "the": [2],
                "Transformer": [3],
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


def _wait_for_batch(batch_id, timeout=5.0):
    """Wait for a batch to finalize."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        batch = _batch_store.get(batch_id)
        if batch is None or batch.is_final:
            return batch
        time.sleep(0.05)
    return _batch_store.get(batch_id)


class TestSearchAcademicDatabases:
    def test_returns_formatted_list(self):
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch("zotpilot.tools.ingestion.httpx.get", return_value=_mock_oa_response()),
        ):
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
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch(
                "zotpilot.tools.ingestion.httpx.get",
                side_effect=httpx.HTTPStatusError("service unavailable", request=MagicMock(), response=mock_resp),
            ),
        ):
            with pytest.raises(ToolError, match="http_503"):
                search_academic_databases("attention mechanism")

    def test_uses_openalex_email_as_mailto(self):
        with (
            patch(
                "zotpilot.tools.ingestion._get_config",
                return_value=_make_config(openalex_email="me@example.com"),
            ),
            patch("zotpilot.tools.ingestion.httpx.get", return_value=_mock_oa_response()) as mock_get,
        ):
            search_academic_databases("attention mechanism")

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["mailto"] == "me@example.com"


class TestIngestPapers:
    def setup_method(self):
        _batch_store.clear()

    def test_over_50_raises_tool_error(self):
        papers = [{"doi": f"10.1000/{i}", "landing_page_url": f"https://example.com/{i}"} for i in range(51)]
        with pytest.raises(ToolError, match="50"):
            ingest_papers(papers)

    def test_invalid_json_raises_tool_error(self):
        with pytest.raises(ToolError, match="JSON array"):
            ingest_papers("not valid json")

    def test_defaults_to_inbox_collection(self):
        papers = [{"landing_page_url": "https://example.com/paper"}]
        with (
            patch("zotpilot.tools.ingestion._ensure_inbox_collection", return_value="INBOX1"),
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch("zotpilot.tools.ingestion._get_writer"),
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "results": [
                        {
                            "success": True,
                            "url": "https://example.com/paper",
                            "item_key": "ITEM1",
                            "title": "Paper",
                            "routing_status": "routed_by_connector",
                        },
                    ],
                },
            ) as save_urls_mock,
            patch("zotpilot.tools.ingestion.ingestion_bridge._cleanup_publisher_tags"),
        ):
            result = ingest_papers(papers)
            # Wait inside with-block so mock is still active when background thread runs
            _wait_for_batch(result["batch_id"])

        assert save_urls_mock.call_args.kwargs["collection_key"] == "INBOX1"
        assert result["collection_used"] == "INBOX1"

    def test_explicit_collection_is_used(self):
        papers = [{"landing_page_url": "https://example.com/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch("zotpilot.tools.ingestion._get_writer"),
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "results": [
                        {
                            "success": True,
                            "url": "https://example.com/paper",
                            "item_key": "ITEM1",
                            "title": "Paper",
                            "routing_status": "routed_by_connector",
                        },
                    ],
                },
            ) as save_urls_mock,
            patch("zotpilot.tools.ingestion.ingestion_bridge._cleanup_publisher_tags"),
        ):
            result = ingest_papers(papers, collection_key="COL1")
            _wait_for_batch(result["batch_id"])
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
        papers = [
            {"doi": "10.1000/test", "arxiv_id": "2401.00001", "landing_page_url": "https://publisher.example/paper"}
        ]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch("zotpilot.tools.ingestion._get_writer"),
            patch("zotpilot.tools.ingestion._lookup_local_item_key_by_doi", return_value=None),
            patch("zotpilot.tools.ingestion._ensure_inbox_collection", return_value="INBOX1"),
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "results": [
                        {
                            "success": True,
                            "url": "https://arxiv.org/abs/2401.00001",
                            "item_key": "ITEM1",
                            "title": "Paper",
                            "routing_status": "routed_by_connector",
                        },
                    ],
                },
            ) as save_urls_mock,
            patch("zotpilot.tools.ingestion.ingestion_bridge._cleanup_publisher_tags"),
        ):
            result = ingest_papers(papers)
            _wait_for_batch(result["batch_id"])
            assert save_urls_mock.call_args.args[0] == ["https://arxiv.org/abs/2401.00001"]

    def test_landing_page_has_priority_over_doi(self):
        papers = [{"doi": "10.1000/test", "landing_page_url": "https://publisher.example/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch("zotpilot.tools.ingestion._get_writer"),
            patch("zotpilot.tools.ingestion._ensure_inbox_collection", return_value="INBOX1"),
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "results": [
                        {
                            "success": True,
                            "url": "https://publisher.example/paper",
                            "item_key": "ITEM1",
                            "title": "Paper",
                            "routing_status": "routed_by_connector",
                        },
                    ],
                },
            ) as save_urls_mock,
            patch("zotpilot.tools.ingestion.ingestion_bridge._cleanup_publisher_tags"),
        ):
            result = ingest_papers(papers)
            _wait_for_batch(result["batch_id"])
            assert save_urls_mock.call_args.args[0] == ["https://publisher.example/paper"]

    def test_local_doi_duplicate_skips_save(self):
        papers = [{"doi": "10.1000/existing", "landing_page_url": "https://publisher.example/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch("zotpilot.tools.ingestion._lookup_local_item_key_by_doi", return_value="EXISTING1"),
            patch("zotpilot.tools.ingestion.save_urls") as save_urls_mock,
        ):
            result = ingest_papers(papers)

        save_urls_mock.assert_not_called()
        assert result["duplicates"] == 1
        assert result["results"][0]["status"] == "duplicate"
        assert result["results"][0]["item_key"] == "EXISTING1"

    def test_local_doi_duplicate_with_collection_routes_to_collection(self):
        """Duplicate papers are routed into the specified collection (not orphaned)."""
        papers = [{"doi": "10.1000/existing", "landing_page_url": "https://publisher.example/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch("zotpilot.tools.ingestion._lookup_local_item_key_by_doi", return_value="EXISTING1"),
            patch("zotpilot.tools.ingestion._ensure_inbox_collection", return_value="COL1"),
            patch("zotpilot.tools.ingestion._get_writer") as get_writer_mock,
            patch("zotpilot.tools.ingestion.save_urls") as save_urls_mock,
        ):
            result = ingest_papers(papers, collection_key="COL1")

        save_urls_mock.assert_not_called()
        assert result["duplicates"] == 1
        writer_mock = get_writer_mock.return_value
        writer_mock.add_to_collection.assert_called_once_with("EXISTING1", "COL1")

    def test_preflight_uses_config_flag(self):
        papers = [{"landing_page_url": "https://publisher.example/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=True)),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.preflight_urls",
                return_value={
                    "checked": 1,
                    "accessible": [],
                    "blocked": [],
                    "skipped": [],
                    "errors": [],
                    "all_clear": True,
                },
            ) as preflight_mock,
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "results": [
                        {
                            "success": True,
                            "url": "https://publisher.example/paper",
                            "item_key": "ITEM1",
                            "title": "Paper",
                        },
                    ],
                },
            ),
        ):
            result = ingest_papers(papers)
            _wait_for_batch(result["batch_id"])

        preflight_mock.assert_called_once()

    def test_preflight_can_be_disabled_in_config(self):
        papers = [{"landing_page_url": "https://publisher.example/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=False)),
            patch("zotpilot.tools.ingestion.ingestion_bridge.preflight_urls") as preflight_mock,
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "results": [
                        {
                            "success": True,
                            "url": "https://publisher.example/paper",
                            "item_key": "ITEM1",
                            "title": "Paper",
                        },
                    ],
                },
            ),
        ):
            result = ingest_papers(papers)
            _wait_for_batch(result["batch_id"])

        preflight_mock.assert_not_called()

    def test_preflight_blocked_returns_failed_results_and_arrays(self):
        papers = [{"landing_page_url": "https://publisher.example/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=True)),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.preflight_urls",
                return_value={
                    "checked": 1,
                    "accessible": [],
                    "blocked": [{"url": "https://publisher.example/paper", "error": "robot check"}],
                    "skipped": [],
                    "errors": [],
                    "all_clear": False,
                },
            ),
            patch("zotpilot.tools.ingestion.save_urls") as save_urls_mock,
        ):
            result = ingest_papers(papers)

        save_urls_mock.assert_not_called()
        assert result["failed"] == 1
        assert result["results"][0]["status"] == "failed"
        assert "blocked" not in result  # no top-level blocked/errors arrays

    def test_bridge_top_level_failure_marks_urls_failed(self):
        papers = [{"landing_page_url": "https://publisher.example/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=False)),
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "success": False,
                    "error": "bridge down",
                    "results": [],
                },
            ),
        ):
            result = ingest_papers(papers)
            batch_id = result["batch_id"]
            _wait_for_batch(batch_id)

        status = get_ingest_status(batch_id)
        assert status["failed"] == 1
        assert status["results"][0]["error"] == "bridge save failed"

    def test_missing_batch_result_marks_unmatched_url_failed(self):
        papers = [
            {"landing_page_url": "https://publisher.example/1"},
            {"landing_page_url": "https://publisher.example/2"},
        ]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=False)),
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "results": [
                        {"success": True, "url": "https://publisher.example/1", "item_key": "ITEM1", "title": "One"},
                    ],
                },
            ),
        ):
            result = ingest_papers(papers)
            batch_id = result["batch_id"]
            _wait_for_batch(batch_id)

        status = get_ingest_status(batch_id)
        assert status["saved"] == 1
        assert status["failed"] == 1
        assert any(
            item["url"] == "https://publisher.example/2" and item["status"] == "failed" for item in status["results"]
        )

    def test_success_and_failure_are_mapped_to_simplified_statuses(self):
        papers = [
            {"landing_page_url": "https://publisher.example/1"},
            {"landing_page_url": "https://publisher.example/2"},
        ]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=False)),
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "results": [
                        {"success": True, "url": "https://publisher.example/1", "item_key": "ITEM1", "title": "One"},
                        {"success": False, "url": "https://publisher.example/2", "error": "translator failed"},
                    ],
                },
            ),
        ):
            result = ingest_papers(papers)
            batch_id = result["batch_id"]
            _wait_for_batch(batch_id)

        status = get_ingest_status(batch_id)
        assert status["saved"] == 1
        assert status["failed"] == 1
        assert {item["status"] for item in status["results"]} == {"saved", "failed"}


class TestGetIngestStatus:
    def setup_method(self):
        _batch_store.clear()

    def test_not_found(self):
        result = get_ingest_status("ing_nonexistent")
        assert result["state"] == "not_found"
        assert result["is_final"] is True

    def test_returns_full_status(self):
        from zotpilot.tools.ingest_state import BatchState, IngestItemState

        items = [IngestItemState(index=0, url="https://example.com", title="T")]
        batch = BatchState(total=1, collection_used="INBOX1", pending_items=items)
        _batch_store.put(batch)
        result = get_ingest_status(batch.batch_id)
        assert result["batch_id"] == batch.batch_id
        assert result["state"] == "queued"
        assert result["is_final"] is False
        assert result["pending_count"] == 1

    def test_returns_final_with_item_keys(self):
        from zotpilot.tools.ingest_state import BatchState, IngestItemState

        items = [IngestItemState(index=0, url="https://example.com", title="T")]
        batch = BatchState(total=1, collection_used="INBOX1", pending_items=items)
        batch.update_item(0, status="saved", item_key="ITEM1", title="Real Title")
        batch.finalize()
        _batch_store.put(batch)
        result = get_ingest_status(batch.batch_id)
        assert result["is_final"] is True
        assert result["saved"] == 1
        assert result["results"][0]["item_key"] == "ITEM1"


class TestIngestPapersAsync:
    def setup_method(self):
        _batch_store.clear()

    def test_returns_batch_id_and_pending(self):
        papers = [{"landing_page_url": "https://publisher.example/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "results": [
                        {
                            "success": True,
                            "url": "https://publisher.example/paper",
                            "item_key": "ITEM1",
                            "title": "Paper",
                        },
                    ],
                },
            ),
        ):
            result = ingest_papers(papers)

        assert "batch_id" in result
        assert result["batch_id"].startswith("ing_")
        assert "pending_count" in result
        assert "_instruction" in result

    def test_duplicates_resolved_immediately(self):
        papers = [{"doi": "10.1000/existing", "landing_page_url": "https://publisher.example/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch("zotpilot.tools.ingestion._lookup_local_item_key_by_doi", return_value="EXISTING1"),
        ):
            result = ingest_papers(papers)

        assert result["is_final"] is True
        assert result["pending_count"] == 0
        assert result["duplicates"] == 1

    def test_no_save_candidates_returns_final(self):
        # Papers with no usable identifier → all fail immediately, is_final
        papers = [{"title": "No identifier here"}]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()):
            result = ingest_papers(papers)

        assert result["is_final"] is True
        assert result["pending_count"] == 0
        assert result["failed"] == 1

    def test_preflight_blocked_returns_final(self):
        papers = [{"landing_page_url": "https://publisher.example/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=True)),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.preflight_urls",
                return_value={
                    "checked": 1,
                    "accessible": [],
                    "blocked": [{"url": "https://publisher.example/paper", "error": "robot check"}],
                    "skipped": [],
                    "errors": [],
                    "all_clear": False,
                },
            ),
            patch("zotpilot.tools.ingestion.save_urls") as save_urls_mock,
        ):
            result = ingest_papers(papers)

        assert result["is_final"] is True
        assert result["pending_count"] == 0
        save_urls_mock.assert_not_called()


class TestSaveFromUrl:
    def test_aliases_save_urls_and_preserves_collection_used(self):
        with patch(
            "zotpilot.tools.ingestion.save_urls",
            return_value={
                "results": [{"success": True, "url": "https://example.com", "item_key": "ITEM1"}],
                "collection_used": "INBOX1",
            },
        ) as save_urls_mock:
            result = save_from_url("https://example.com")

        save_urls_mock.assert_called_once_with(["https://example.com"], collection_key=None, tags=None)
        assert result["collection_used"] == "INBOX1"


class TestRunSaveWorkerReconciliation:
    def setup_method(self):
        _batch_store.clear()

    def test_updates_batch_item_routing_status_from_save_results(self):
        from zotpilot.tools.ingest_state import BatchState, IngestItemState

        batch = BatchState(
            total=1,
            collection_used="COL1",
            pending_items=[IngestItemState(index=0, url="https://example.com/paper", title="Paper")],
        )
        with patch(
            "zotpilot.tools.ingestion.save_urls",
            return_value={
                "results": [
                    {
                        "success": True,
                        "url": "https://example.com/paper",
                        "item_key": "ITEM1",
                        "title": "Paper",
                        "routing_status": "routed_by_backend",
                    }
                ],
            },
        ):
            _run_save_worker(
                batch,
                [{"url": "https://example.com/paper", "_index": 0, "paper": {"title": "Paper"}}],
                "COL1",
            )

        assert batch.is_final is True
        assert batch.pending_items[0].routing_status == "routed_by_backend"

    def test_reconciliation_prefers_local_api_then_web_api(self):
        from zotpilot.tools.ingest_state import BatchState, IngestItemState

        batch = BatchState(
            total=1,
            collection_used="COL1",
            pending_items=[IngestItemState(index=0, url="https://example.com/paper", title="Paper")],
        )
        with (
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "results": [
                        {
                            "success": True,
                            "url": "https://example.com/paper",
                            "item_key": None,
                            "title": "Paper",
                            "routing_status": "routing_deferred",
                            "warning": "deferred",
                        }
                    ],
                },
            ),
            patch("zotpilot.tools.ingestion.time.sleep"),
            patch("zotpilot.tools.ingestion._discover_via_local_api", return_value="ITEM1"),
            patch("zotpilot.tools.ingestion._route_via_local_api", return_value=True) as route_local_mock,
            patch("zotpilot.tools.ingestion._get_writer") as get_writer_mock,
            patch("zotpilot.tools.ingestion.ingestion_bridge._cleanup_publisher_tags"),
        ):
            _run_save_worker(
                batch,
                [{"url": "https://example.com/paper", "_index": 0, "paper": {"title": "Paper"}}],
                "COL1",
            )

        route_local_mock.assert_called_once_with("ITEM1", "COL1")
        assert batch.pending_items[0].item_key == "ITEM1"
        assert batch.pending_items[0].routing_status == "routed_by_reconciliation_local"
        assert batch.pending_items[0].warning is None

    def test_reconciliation_falls_back_to_web_api_routing(self):
        from zotpilot.tools.ingest_state import BatchState, IngestItemState

        batch = BatchState(
            total=1,
            collection_used="COL1",
            pending_items=[IngestItemState(index=0, url="https://example.com/paper", title="Paper")],
        )
        writer = MagicMock()
        with (
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "results": [
                        {
                            "success": True,
                            "url": "https://example.com/paper",
                            "item_key": "ITEM1",
                            "title": "Paper",
                            "warning": "not routed",
                        }
                    ],
                },
            ),
            patch("zotpilot.tools.ingestion.time.sleep"),
            patch("zotpilot.tools.ingestion._route_via_local_api", return_value=False),
            patch("zotpilot.tools.ingestion._get_writer", return_value=writer),
        ):
            _run_save_worker(
                batch,
                [{"url": "https://example.com/paper", "_index": 0, "paper": {"title": "Paper"}}],
                "COL1",
            )

        writer.add_to_collection.assert_called_once_with("ITEM1", "COL1")
        assert batch.pending_items[0].routing_status == "routed_by_reconciliation_web"
