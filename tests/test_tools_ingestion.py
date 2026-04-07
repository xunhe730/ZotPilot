"""Tests for ingestion MCP tools."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from zotpilot.tools.ingestion import (
    _batch_store,
    _is_pdf_or_doi_url,
    _resolve_dois_concurrent,
    _run_save_worker,
    classify_ingest_candidate,
    get_ingest_status,
    ingest_papers,
    resolve_doi_to_landing_url,
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


def _wait_for_batch(batch_id, timeout=20.0):
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
            patch("zotpilot.openalex_client.httpx.get", return_value=_mock_oa_response()),
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
                "zotpilot.openalex_client.httpx.get",
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
            patch("zotpilot.openalex_client.httpx.get", return_value=_mock_oa_response()) as mock_get,
        ):
            search_academic_databases("attention mechanism")

        _, kwargs = mock_get.call_args
        assert kwargs["params"]["mailto"] == "me@example.com"


class TestIngestPapers:
    def setup_method(self):
        _batch_store.clear()
        self._submit_patcher = patch("zotpilot.tools.ingestion._executor.submit", side_effect=self._submit_inline)
        self._submit_patcher.start()

    def teardown_method(self):
        self._submit_patcher.stop()

    @staticmethod
    def _submit_inline(fn, *args, **kwargs):
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:  # pragma: no cover - mirrors executor behavior
            future.set_exception(exc)
        return future

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
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.wait_for_extension",
                return_value={"extension_connected": True},
            ),
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
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.wait_for_extension",
                return_value={"extension_connected": True},
            ),
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

    def test_doi_only_routes_to_connector_when_resolution_finds_landing_page(self):
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch(
                "zotpilot.tools.ingestion._resolve_dois_concurrent",
                return_value={"10.1000/test": "https://publisher.example/article"},
            ),
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.wait_for_extension",
                return_value={"extension_connected": True},
            ),
            patch("zotpilot.tools.ingestion._run_save_worker"),
        ):
            result = ingest_papers([{"doi": "10.1000/test"}])

        assert result["failed"] == 0
        assert len(result["pending_items"]) == 1
        assert result["pending_items"][0]["ingest_method"] == "connector"
        assert result["pending_items"][0]["url"] == "https://publisher.example/article"

    def test_doi_only_falls_back_to_api_when_resolution_fails(self):
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch("zotpilot.tools.ingestion._resolve_dois_concurrent", return_value={"10.1000/test": None}),
            patch("zotpilot.tools.ingestion._run_save_worker"),
        ):
            result = ingest_papers([{"doi": "10.1000/test"}])

        assert result["failed"] == 0
        assert len(result["pending_items"]) == 1
        assert result["pending_items"][0]["ingest_method"] == "api"
        assert result["pending_items"][0]["url"] is None

    def test_arxiv_id_has_priority(self):
        papers = [
            {"doi": "10.1000/test", "arxiv_id": "2401.00001", "landing_page_url": "https://publisher.example/paper"}
        ]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config()),
            patch("zotpilot.tools.ingestion._get_writer"),
            patch("zotpilot.tools.ingestion._lookup_local_item_key_by_doi", return_value=None),
            patch("zotpilot.tools.ingestion._ensure_inbox_collection", return_value="INBOX1"),
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.wait_for_extension",
                return_value={"extension_connected": True},
            ),
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
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.wait_for_extension",
                return_value={"extension_connected": True},
            ),
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
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.wait_for_extension",
                return_value={"extension_connected": True},
            ),
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
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.wait_for_extension",
                return_value={"extension_connected": True},
            ),
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
        assert result["is_final"] is True  # early return, no polling needed
        assert result["results"][0]["status"] == "failed"
        assert result["blocked"][0]["url"] == "https://publisher.example/paper"
        assert "halted" in result["_instruction"].lower()  # instructs agent to stop

    def test_bridge_top_level_failure_marks_urls_failed(self):
        papers = [{"landing_page_url": "https://publisher.example/paper"}]
        with (
            patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(preflight_enabled=False)),
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.wait_for_extension",
                return_value={"extension_connected": True},
            ),
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
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.wait_for_extension",
                return_value={"extension_connected": True},
            ),
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
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True),
            patch(
                "zotpilot.tools.ingestion.ingestion_bridge.wait_for_extension",
                return_value={"extension_connected": True},
            ),
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
                [],
                "COL1",
            )

        assert batch.is_final is True
        assert batch.pending_items[0].routing_status == "routed_by_backend"

    def test_early_verification_discovers_item_key_when_bridge_omits_it(self):
        """When bridge returns success=True but no item_key, early verification
        discovers the key via _discover_via_local_api before reconciliation."""
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
            patch("zotpilot.tools.ingestion._discover_via_local_api", return_value="ITEM1") as discover_mock,
            patch("zotpilot.tools.ingestion._route_via_local_api", return_value=True),
            patch("zotpilot.tools.ingestion._get_writer"),
            patch("zotpilot.tools.ingestion.ingestion_bridge._cleanup_publisher_tags"),
        ):
            _run_save_worker(
                batch,
                [{"url": "https://example.com/paper", "_index": 0, "paper": {"title": "Paper"}}],
                [],
                "COL1",
            )

        # Early verification discovers item_key in Phase 1 (before reconciliation)
        discover_mock.assert_called()
        assert batch.pending_items[0].item_key == "ITEM1"
        assert batch.pending_items[0].status == "saved"

    def test_early_verification_marks_failed_when_item_not_found(self):
        """When bridge returns success=True but item cannot be found in Zotero,
        early verification demotes to failed instead of false success."""
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
                        }
                    ],
                },
            ),
            patch("zotpilot.tools.ingestion.time.sleep"),
            patch("zotpilot.tools.ingestion._discover_via_local_api", return_value=None),
            patch("zotpilot.tools.ingestion._discover_via_web_api", return_value=None),
            patch("zotpilot.tools.ingestion._get_writer"),
            patch("zotpilot.tools.ingestion.ingestion_bridge._cleanup_publisher_tags"),
        ):
            _run_save_worker(
                batch,
                [{"url": "https://example.com/paper", "_index": 0, "paper": {"title": "Paper"}}],
                [],
                "COL1",
            )

        assert batch.pending_items[0].status == "failed"
        assert "not found in Zotero" in batch.pending_items[0].error

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
                [],
                "COL1",
            )

        writer.add_to_collection.assert_called_once_with("ITEM1", "COL1")
        assert batch.pending_items[0].routing_status == "routed_by_reconciliation_web"


class TestHelperFunctions:
    def test_is_pdf_or_doi_url_pdf_suffix(self):
        assert _is_pdf_or_doi_url("https://example.com/paper.pdf") is True
        assert _is_pdf_or_doi_url("https://example.com/paper.pdf?download=1") is True
        assert _is_pdf_or_doi_url("https://example.com/paper.pdf#section") is True

    def test_is_pdf_or_doi_url_doi_redirect(self):
        assert _is_pdf_or_doi_url("https://doi.org/10.1000/test") is True
        assert _is_pdf_or_doi_url("https://dx.doi.org/10.1000/test") is True
        assert _is_pdf_or_doi_url("http://doi.org/10.1000/test") is True

    def test_is_pdf_or_doi_url_false_for_regular_urls(self):
        assert _is_pdf_or_doi_url("https://publisher.example/article") is False
        assert _is_pdf_or_doi_url("https://arxiv.org/abs/2401.00001") is False
        assert _is_pdf_or_doi_url("https://scholar.google.com/scholar?q=test") is False

    def test_is_pdf_or_doi_url_none(self):
        assert _is_pdf_or_doi_url(None) is False

    def test_classify_ingest_candidate_arxiv_routes_to_connector(self):
        result = classify_ingest_candidate(
            paper={"doi": "10.1000/test", "arxiv_id": "2401.00001"},
            normalized_doi="10.1000/test",
            arxiv_id="2401.00001",
            landing_page_url="https://arxiv.org/abs/2401.00001",
        )
        assert result == "connector"

    def test_classify_ingest_candidate_non_pdf_landing_page_routes_to_connector(self):
        result = classify_ingest_candidate(
            paper={"doi": "10.1000/test"},
            normalized_doi=None,
            arxiv_id=None,
            landing_page_url="https://publisher.example/article",
        )
        assert result == "connector"

    def test_classify_ingest_candidate_doi_only_routes_to_api(self):
        result = classify_ingest_candidate(
            paper={"doi": "10.1000/test"},
            normalized_doi="10.1000/test",
            arxiv_id=None,
            landing_page_url=None,
        )
        assert result == "api"

    def test_classify_ingest_candidate_doi_with_pdf_url_routes_to_api(self):
        result = classify_ingest_candidate(
            paper={"doi": "10.1000/test"},
            normalized_doi="10.1000/test",
            arxiv_id=None,
            landing_page_url="https://example.com/paper.pdf",
        )
        assert result == "api"

    def test_classify_ingest_candidate_resolved_landing_page_routes_to_connector(self):
        result = classify_ingest_candidate(
            paper={"doi": "10.1000/test", "_resolved_landing_url": "https://publisher.example/article"},
            normalized_doi="10.1000/test",
            arxiv_id=None,
            landing_page_url=None,
        )
        assert result == "connector"

    def test_classify_ingest_candidate_no_identifier_rejects(self):
        result = classify_ingest_candidate(
            paper={"title": "No identifier here"},
            normalized_doi=None,
            arxiv_id=None,
            landing_page_url=None,
        )
        assert result == "reject"


class TestSaveViaApi:
    def setup_method(self):
        _batch_store.clear()

    def test_save_via_api_success(self):
        from zotpilot.tools.ingest_state import BatchState, IngestItemState

        candidate = {
            "_index": 0,
            "paper": {"doi": "10.1000/test"},
            "url": None,
        }
        batch = BatchState(
            total=1,
            collection_used="COL1",
            pending_items=[IngestItemState(index=0, url=None, title=None)],
        )
        writer = MagicMock()
        writer.create_item_from_metadata.return_value = {
            "successful": {"0": {"key": "ITEM1", "data": {"key": "ITEM1"}}}
        }
        writer.try_attach_oa_pdf.return_value = "not_found"
        resolver_mock = MagicMock()
        resolver_mock.resolve.return_value.title = "Test Paper"
        resolver_mock.resolve.return_value.doi = "10.1000/test"
        resolver_mock.resolve.return_value.abstract = "Abstract."
        resolver_mock.resolve.return_value.oa_url = None
        resolver_mock.resolve.return_value.arxiv_id = None

        with (
            patch("zotpilot.state._get_resolver", return_value=resolver_mock),
            patch("zotpilot.state._get_config", return_value=_make_config()),
        ):
            from zotpilot.tools.ingestion import _save_via_api

            result = _save_via_api(
                candidate,
                resolved_collection_key="COL1",
                tags=None,
                batch=batch,
                writer=writer,
                _writer_lock=threading.Lock(),
            )

        assert result["success"] is True
        assert result["item_key"] == "ITEM1"
        assert batch.pending_items[0].status == "saved"
        assert batch.pending_items[0].routing_status == "routed_by_api"
        assert batch.pending_items[0].ingest_method == "api"

    def test_save_via_api_crossref_failure_marks_failed(self):
        from zotpilot.tools.ingest_state import BatchState, IngestItemState

        candidate = {
            "_index": 0,
            "paper": {"doi": "10.1000/test"},
            "url": None,
        }
        batch = BatchState(
            total=1,
            collection_used="COL1",
            pending_items=[IngestItemState(index=0, url=None, title=None)],
        )
        writer = MagicMock()
        resolver_mock = MagicMock()
        resolver_mock.resolve.side_effect = Exception("CrossRef lookup failed")

        with (
            patch("zotpilot.state._get_resolver", return_value=resolver_mock),
            patch("zotpilot.state._get_config", return_value=_make_config()),
        ):
            from zotpilot.tools.ingestion import _save_via_api

            result = _save_via_api(
                candidate,
                resolved_collection_key="COL1",
                tags=None,
                batch=batch,
                writer=writer,
                _writer_lock=threading.Lock(),
            )

        assert result["success"] is False
        assert "CrossRef lookup failed" in result["error"]
        assert batch.pending_items[0].status == "failed"


class TestDoiResolutionHelpers:
    def test_resolve_doi_to_landing_url_returns_location_header(self):
        response = MagicMock(status_code=302, headers={"location": "https://publisher.example/article"})
        with patch("zotpilot.tools.ingestion.httpx.head", return_value=response) as head_mock:
            result = resolve_doi_to_landing_url("10.1000/test")

        assert result == "https://publisher.example/article"
        head_mock.assert_called_once_with(
            "https://doi.org/10.1000/test",
            follow_redirects=False,
            timeout=10.0,
        )

    def test_resolve_dois_concurrent_handles_partial_failures(self):
        def fake_resolver(doi):
            if doi == "10.1000/fail":
                raise RuntimeError("boom")
            return f"https://publisher.example/{doi.rsplit('/', 1)[-1]}"

        with patch("zotpilot.tools.ingestion.resolve_doi_to_landing_url", side_effect=fake_resolver):
            result = _resolve_dois_concurrent(["10.1000/one", "10.1000/fail", "10.1000/two"])

        assert result["10.1000/one"] == "https://publisher.example/one"
        assert result["10.1000/two"] == "https://publisher.example/two"
        assert result["10.1000/fail"] is None


class TestReconciliationSkipsApiItems:
    def setup_method(self):
        _batch_store.clear()

    def test_api_saved_item_excluded_from_unrouted_items(self):
        from zotpilot.tools.ingest_state import BatchState, IngestItemState

        batch = BatchState(
            total=2,
            collection_used="COL1",
            pending_items=[
                IngestItemState(
                    index=0,
                    url="https://example.com/connector",
                    title="Connector Paper",
                    ingest_method="connector",
                ),
                IngestItemState(
                    index=1,
                    url=None,
                    title="API Paper",
                    ingest_method="api",
                    routing_status="routed_by_api",
                    status="saved",
                    item_key="ITEM2",
                ),
            ],
        )
        batch.update_item(0, status="saved", item_key="ITEM1", routing_status=None)

        unrouted_items = [
            item
            for item in batch.pending_items
            if item.status == "saved" and item.item_key and not item.routing_status and item.ingest_method != "api"
        ]
        api_items = [item for item in batch.pending_items if item.ingest_method == "api"]

        assert len(unrouted_items) == 1
        assert unrouted_items[0].index == 0
        assert len(api_items) == 1
        assert api_items[0].index == 1
        assert api_items[0].routing_status == "routed_by_api"


class TestConnectorFallbackToApi:
    def setup_method(self):
        _batch_store.clear()

    def test_connector_failure_with_doi_retries_via_api_before_reconciliation(self):
        from zotpilot.tools.ingest_state import BatchState, IngestItemState

        batch = BatchState(
            total=1,
            collection_used="COL1",
            pending_items=[
                IngestItemState(
                    index=0,
                    url="https://publisher.example/paper",
                    title="Paper",
                    ingest_method="connector",
                )
            ],
        )
        writer = MagicMock()
        with (
            patch(
                "zotpilot.tools.ingestion.save_urls",
                return_value={
                    "success": False,
                    "error": "bridge down",
                    "results": [],
                },
            ),
            patch(
                "zotpilot.tools.ingestion._save_via_api",
                return_value={"success": True, "item_key": "ITEM1", "title": "Paper"},
            ) as save_via_api_mock,
            patch("zotpilot.tools.ingestion._get_writer", return_value=writer),
        ):
            _run_save_worker(
                batch,
                [
                    {
                        "url": "https://publisher.example/paper",
                        "_index": 0,
                        "paper": {"title": "Paper", "doi": "10.1000/test"},
                        "ingest_method": "connector",
                    }
                ],
                [],
                "COL1",
            )

        assert batch.is_final is True
        assert batch.pending_items[0].ingest_method == "api"
        save_via_api_mock.assert_called_once()
