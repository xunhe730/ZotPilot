"""Tests for bridge-dependent ingestion paths.

Covers _discover_saved_item_key, _apply_bridge_result_routing,
save_from_url, and save_urls — all with mocked HTTP / bridge.
"""
from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from zotpilot.tools.ingestion import (
    _apply_bridge_result_routing,
    _discover_saved_item_key,
    _preflight_urls,
    ingest_papers,
    save_from_url,
    save_urls,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_writer():
    writer = MagicMock()
    writer.find_items_by_url_and_title.return_value = []
    writer.check_duplicate_by_doi.return_value = None
    writer.add_to_collection.return_value = None
    writer.add_item_tags.return_value = None
    writer.get_item_type.return_value = "journalArticle"
    writer.delete_item.return_value = True
    writer.check_has_pdf.return_value = True  # default: PDF attached
    return writer


def _make_config(api_key="TEST_API_KEY"):
    config = MagicMock()
    config.zotero_api_key = api_key
    return config


def _make_urlopen_response(body: dict, status: int = 200):
    """Return a mock that behaves like urllib response."""
    mock = MagicMock()
    mock.status = status
    mock.read.return_value = json.dumps(body).encode()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _make_http_error(code: int, body: dict | None = None):
    """Return a urllib.error.HTTPError with optional JSON body."""
    body_bytes = json.dumps(body).encode() if body else b""
    err = urllib.error.HTTPError(
        url="http://127.0.0.1:9999/enqueue",
        code=code,
        msg="Error",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(body_bytes),
    )
    return err


# ---------------------------------------------------------------------------
# TestDiscoverSavedItemKey
# ---------------------------------------------------------------------------

class TestDiscoverSavedItemKey:
    def test_known_key_returned_immediately(self):
        writer = _make_writer()
        result = _discover_saved_item_key(
            title="Some Title",
            url="https://example.com",
            known_key="KNOWN1",
            writer=writer,
        )
        assert result == "KNOWN1"
        writer.find_items_by_url_and_title.assert_not_called()

    def test_no_title_no_url_returns_none(self):
        writer = _make_writer()
        result = _discover_saved_item_key(
            title="",
            url="",
            known_key=None,
            writer=writer,
        )
        assert result is None
        writer.find_items_by_url_and_title.assert_not_called()

    def test_exactly_one_match_returns_it(self):
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = ["KEY123"]
        result = _discover_saved_item_key(
            title="Test Paper",
            url="https://doi.org/10.1234/test",
            known_key=None,
            writer=writer,
        )
        assert result == "KEY123"

    def test_zero_matches_returns_none(self):
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = []
        result = _discover_saved_item_key(
            title="Test Paper",
            url="https://doi.org/10.1234/test",
            known_key=None,
            writer=writer,
        )
        assert result is None

    def test_multiple_matches_returns_none(self):
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = ["KEY1", "KEY2"]
        result = _discover_saved_item_key(
            title="Duplicate Title",
            url="https://example.com/paper",
            known_key=None,
            writer=writer,
        )
        assert result is None

    def test_window_s_passed_to_writer(self):
        """window_s is forwarded to find_items_by_url_and_title."""
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = ["KEY1"]
        result = _discover_saved_item_key(
            title="Test Paper",
            url="https://example.com",
            known_key=None,
            writer=writer,
            window_s=120,
        )
        assert result == "KEY1"
        writer.find_items_by_url_and_title.assert_called_once_with(
            "https://example.com", "Test Paper", window_s=120
        )

    def test_exception_returns_none_logged(self):
        writer = _make_writer()
        writer.find_items_by_url_and_title.side_effect = Exception("API error")
        result = _discover_saved_item_key(
            title="Test",
            url="https://example.com",
            known_key=None,
            writer=writer,
        )
        assert result is None


# ---------------------------------------------------------------------------
# TestApplyBridgeResultRouting
# ---------------------------------------------------------------------------

class TestApplyBridgeResultRouting:
    def test_no_collection_no_tags_returns_unchanged(self):
        result = {"success": True, "url": "https://example.com", "title": "Test"}
        writer = _make_writer()
        config = _make_config(api_key="KEY")
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion.time.sleep"):
            out = _apply_bridge_result_routing(result, None, None)
        # No routing — no warning added
        assert "warning" not in out
        writer.add_to_collection.assert_not_called()
        writer.add_item_tags.assert_not_called()

    def test_item_key_discovered_routing_applied(self):
        result = {
            "success": True,
            "url": "https://example.com/paper",
            "title": "My Paper",
        }
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = ["ITEM1"]
        config = _make_config(api_key="KEY")
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion.time.sleep"):
            out = _apply_bridge_result_routing(result, "COL1", ["tag1"])
        assert out.get("item_key") == "ITEM1"
        writer.add_to_collection.assert_called_once_with("ITEM1", "COL1")
        writer.add_item_tags.assert_called_once_with("ITEM1", ["tag1"])
        assert "warning" not in out

    def test_item_key_not_discovered_zero_matches_returns_warning(self):
        result = {
            "success": True,
            "url": "https://example.com/paper",
            "title": "Obscure Paper",
        }
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = []
        writer.find_items_by_title.return_value = []
        config = _make_config(api_key="KEY")
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion.time.sleep"):
            out = _apply_bridge_result_routing(result, "COL1", None)
        assert "warning" in out
        assert "not found" in out["warning"]
        writer.add_to_collection.assert_not_called()

    def test_ambiguous_match_returns_warning(self):
        result = {
            "success": True,
            "url": "https://example.com/paper",
            "title": "Common Title",
        }
        writer = _make_writer()
        # First call (in _discover_saved_item_key) returns 2 items → title fallback finds 0 → None
        # Second call (in _apply_bridge_result_routing for count) also returns 2 items
        writer.find_items_by_url_and_title.return_value = ["KEY1", "KEY2"]
        writer.find_items_by_title.return_value = []
        config = _make_config(api_key="KEY")
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion.time.sleep"):
            out = _apply_bridge_result_routing(result, "COL1", None)
        assert "warning" in out
        assert "ambiguous" in out["warning"]

    def test_no_api_key_returns_warning_about_ignored_routing(self):
        result = {
            "success": True,
            "url": "https://example.com",
            "title": "Paper",
        }
        config = _make_config(api_key=None)
        with patch("zotpilot.tools.ingestion._get_config", return_value=config):
            out = _apply_bridge_result_routing(result, "COL1", ["tag1"])
        assert "warning" in out
        assert "ZOTERO_API_KEY" in out["warning"]

    def test_success_false_returns_result_as_is(self):
        result = {
            "success": False,
            "error_code": "translator_failed",
            "error_message": "No translator found",
        }
        out = _apply_bridge_result_routing(result, "COL1", ["tag"])
        assert out is result
        assert out["success"] is False

    def test_item_key_in_bridge_result_skips_sleep(self):
        """When bridge result already has item_key, time.sleep should not be called."""
        result = {
            "success": True,
            "url": "https://example.com",
            "title": "Paper",
            "item_key": "KNOWN_KEY",
        }
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = ["KNOWN_KEY"]
        config = _make_config(api_key="KEY")
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion.time.sleep") as mock_sleep:
            _apply_bridge_result_routing(result, None, None)
        mock_sleep.assert_not_called()

    def test_webpage_item_deleted_and_reported_as_fallback(self):
        """Closed-loop: if Zotero saved item as 'webpage', delete it and return failure."""
        result = {
            "success": True,
            "url": "https://pubs.aip.org/aip/pof/article/36/1/015120",
            "title": "Physics of Fluids",
            "item_key": "JUNK1",
        }
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = ["JUNK1"]
        writer.get_item_type.return_value = "webpage"
        config = _make_config(api_key="KEY")
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion.time.sleep"):
            out = _apply_bridge_result_routing(result, "COL1", ["tag1"])
        assert out["success"] is False
        assert out["translator_fallback_detected"] is True
        assert out["saved_item_type"] == "webpage"
        writer.delete_item.assert_called_once_with("JUNK1")
        writer.add_to_collection.assert_not_called()

    def test_journal_article_type_passes_verification(self):
        """Closed-loop: journalArticle type passes verification, routing applied normally."""
        result = {
            "success": True,
            "url": "https://pubs.acs.org/doi/10.1021/example",
            "title": "Drag Reduction Study",
            "item_key": "GOOD1",
        }
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = ["GOOD1"]
        writer.get_item_type.return_value = "journalArticle"
        config = _make_config(api_key="KEY")
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion.time.sleep"):
            out = _apply_bridge_result_routing(result, "COL1", ["tag1"])
        assert out["success"] is True
        assert "translator_fallback_detected" not in out
        writer.delete_item.assert_not_called()
        writer.add_to_collection.assert_called_once_with("GOOD1", "COL1")

    def test_generic_site_only_title_without_item_key_is_error_page(self):
        result = {
            "success": True,
            "url": "https://www.biorxiv.org/content/10.1101/missing",
            "title": "| bioRxiv",
        }
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = []
        config = _make_config(api_key="KEY")
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            out = _apply_bridge_result_routing(result, None, None)
        assert out["success"] is False
        assert out["error_code"] == "error_page_detected"

    def test_collection_tag_routing_retries_before_warning(self):
        result = {
            "success": True,
            "url": "https://example.com/paper",
            "title": "My Paper",
        }
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = ["ITEM1"]
        writer.add_to_collection.side_effect = [Exception("404 Item does not exist"), None]
        config = _make_config(api_key="KEY")
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion.time.sleep"):
            out = _apply_bridge_result_routing(result, "COL1", ["tag1"])
        assert "warning" not in out
        assert writer.add_to_collection.call_count == 2
        writer.add_item_tags.assert_called_once_with("ITEM1", ["tag1"])


# ---------------------------------------------------------------------------
# TestSaveFromUrl
# ---------------------------------------------------------------------------

class TestSaveFromUrl:
    def _patch_bridge(self, is_running=True, auto_start_exc=None):
        patches = [
            patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=is_running),
            patch("zotpilot.tools.ingestion.time.sleep"),
        ]
        if auto_start_exc:
            patches.append(
                patch("zotpilot.tools.ingestion.BridgeServer.auto_start", side_effect=auto_start_exc)
            )
        else:
            patches.append(
                patch("zotpilot.tools.ingestion.BridgeServer.auto_start", return_value=None)
            )
        return patches

    def test_successful_save_returns_result_with_title(self):
        enqueue_resp = _make_urlopen_response({"request_id": "req-001"})
        poll_resp = _make_urlopen_response(
            {"success": True, "url": "https://example.com", "title": "Great Paper", "request_id": "req-001"},
            status=200,
        )
        config = _make_config(api_key=None)

        call_count = [0]
        def fake_urlopen(req_or_url, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return enqueue_resp
            return poll_resp

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = save_from_url("https://example.com")

        assert result["success"] is True
        assert result["title"] == "Great Paper"
        assert result["collection_used"] is None

    def test_successful_save_defaults_to_inbox_collection(self):
        enqueue_resp = _make_urlopen_response({"request_id": "req-inbox"})
        poll_resp = _make_urlopen_response(
            {"success": True, "url": "https://example.com", "title": "Great Paper", "request_id": "req-inbox"},
            status=200,
        )
        config = _make_config(api_key="KEY")

        call_count = [0]

        def fake_urlopen(req_or_url, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return enqueue_resp
            return poll_resp

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion._ensure_inbox_collection", return_value="INBOX1"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = save_from_url("https://example.com")

        assert result["success"] is True
        assert result["collection_used"] == "INBOX1"

    def test_auto_start_raises_runtime_error_returns_error(self):
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=False), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start",
                   side_effect=RuntimeError("Cannot start bridge")):
            result = save_from_url("https://example.com")

        assert result["success"] is False
        assert "Cannot start bridge" in result["error"]

    def test_enqueue_503_returns_extension_not_connected(self):
        err_body = {
            "error_code": "extension_not_connected",
            "error_message": "No heartbeat received.",
        }
        http_err = _make_http_error(503, err_body)

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("urllib.request.urlopen", side_effect=http_err):
            result = save_from_url("https://example.com")

        assert result["success"] is False
        assert result.get("error_code") == "extension_not_connected"

    def test_poll_timeout_returns_timeout_error(self):
        enqueue_resp = _make_urlopen_response({"request_id": "req-timeout"})

        # Poll always raises (simulates 204 / no result)
        poll_error = urllib.error.URLError("connection refused")

        call_count = [0]
        def fake_urlopen(req_or_url, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return enqueue_resp
            raise poll_error

        # Make time.monotonic advance past the configured deadline quickly.
        mono_values = [0.0] * 10 + [1000.0] * 500

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion.time.monotonic", side_effect=mono_values), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = save_from_url("https://example.com")

        assert result["success"] is False
        assert "Timeout" in result["error"]

    def test_enqueue_url_error_returns_error(self):
        url_err = urllib.error.URLError("Network unreachable")

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("urllib.request.urlopen", side_effect=url_err):
            result = save_from_url("https://example.com")

        assert result["success"] is False
        assert "Failed to enqueue" in result["error"]

    def test_bridge_not_running_auto_start_succeeds(self):
        """Bridge not running → auto_start called → enqueue + poll succeed."""
        enqueue_resp = _make_urlopen_response({"request_id": "req-auto"})
        poll_resp = _make_urlopen_response(
            {"success": True, "url": "https://example.com", "title": "Auto Paper", "request_id": "req-auto"},
            status=200,
        )
        config = _make_config(api_key=None)

        call_count = [0]
        def fake_urlopen(req_or_url, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return enqueue_resp
            return poll_resp

        mock_auto_start = MagicMock()
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=False), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start", mock_auto_start), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = save_from_url("https://example.com")

        mock_auto_start.assert_called_once()
        assert result["success"] is True
        assert result["title"] == "Auto Paper"

    def test_enqueue_returns_success_false_propagated(self):
        enqueue_resp = _make_urlopen_response({"request_id": "req-002"})
        poll_resp = _make_urlopen_response(
            {"success": False, "error": "translator_failed", "request_id": "req-002"},
            status=200,
        )
        config = _make_config(api_key=None)

        call_count = [0]
        def fake_urlopen(req_or_url, timeout=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return enqueue_resp
            return poll_resp

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = save_from_url("https://example.com")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestSaveUrls
# ---------------------------------------------------------------------------

class TestSaveUrls:
    def test_empty_urls_raises_tool_error(self):
        with pytest.raises(ToolError, match="cannot be empty"):
            save_urls([])

    def test_more_than_10_urls_raises_tool_error(self):
        urls = [f"https://example.com/{i}" for i in range(11)]
        with pytest.raises(ToolError, match="Too many URLs"):
            save_urls(urls)

    def test_auto_start_fails_returns_error(self):
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=False), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start",
                   side_effect=RuntimeError("Bridge failed")):
            result = save_urls(["https://example.com/1"])

        assert result["success"] is False
        assert "Bridge failed" in result["error"]
        assert result["results"] == []

    def test_three_urls_all_succeed(self):
        urls = [
            "https://example.com/1",
            "https://example.com/2",
            "https://example.com/3",
        ]
        config = _make_config(api_key=None)

        # Each enqueue returns a unique request_id; each poll returns success
        enqueue_counter = [0]
        poll_results = {
            "req-1": {"success": True, "url": urls[0], "title": "Paper 1", "request_id": "req-1"},
            "req-2": {"success": True, "url": urls[1], "title": "Paper 2", "request_id": "req-2"},
            "req-3": {"success": True, "url": urls[2], "title": "Paper 3", "request_id": "req-3"},
        }
        req_ids = ["req-1", "req-2", "req-3"]

        def fake_urlopen(req_or_url, timeout=None):
            url_str = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
            if "/enqueue" in url_str:
                idx = enqueue_counter[0]
                enqueue_counter[0] += 1
                return _make_urlopen_response({"request_id": req_ids[idx]})
            # Poll
            for rid, body in poll_results.items():
                if rid in url_str:
                    return _make_urlopen_response(body, status=200)
            raise urllib.error.URLError("not found")

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = save_urls(urls)

        assert result["total"] == 3
        assert result["succeeded"] == 3
        assert result["failed"] == 0
        assert len(result["results"]) == 3

    def test_mixed_results_enqueue_fail_and_one_success(self):
        """3 URLs: 1 enqueue 503 + 1 poll timeout + 1 poll success.

        URL 1: enqueue raises 503 → immediate enqueue error
        URL 2: enqueues OK (req-timeout) but all polls raise URLError → per-URL timeout
        URL 3: enqueues OK (req-success) and polls return success

        Expected: succeeded=1, failed=2, total=3, 3 entries in results.
        """
        urls = [
            "https://example.com/fail-enqueue",
            "https://example.com/poll-timeout",
            "https://example.com/success",
        ]
        config = _make_config(api_key=None)

        enqueue_counter = [0]

        def fake_urlopen(req_or_url, timeout=None):
            url_str = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
            if "/enqueue" in url_str:
                idx = enqueue_counter[0]
                enqueue_counter[0] += 1
                if idx == 0:
                    raise _make_http_error(503, {
                        "error_code": "extension_not_connected",
                        "error_message": "No heartbeat.",
                    })
                elif idx == 1:
                    return _make_urlopen_response({"request_id": "req-timeout"})
                else:
                    return _make_urlopen_response({"request_id": "req-success"})
            # Poll path
            if "req-success" in url_str:
                return _make_urlopen_response(
                    {"success": True, "url": urls[2], "title": "Success Paper", "request_id": "req-success"},
                    status=200,
                )
            # req-timeout: always raise URLError so the per-URL deadline triggers
            raise urllib.error.URLError("connection refused")

        # time.monotonic controls per-URL deadlines inside _poll_one threads.
        # We return 0.0 for the first few calls (deadline setup + initial while checks),
        # then 1000.0 so the req-timeout thread exits its while loop after one failed poll.
        # req-success succeeds on first urlopen call so its thread returns before the
        # deadline check matters.
        mono_values = iter([0.0] * 10 + [1000.0] * 500)

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion.time.monotonic", side_effect=mono_values), \
             patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = save_urls(urls)

        assert result["total"] == 3
        assert result["succeeded"] == 1
        assert result["failed"] == 2
        assert len(result["results"]) == 3
        assert any(r.get("error_code") == "extension_not_connected" for r in result["results"])

    def test_enqueue_503_url_appears_in_results(self):
        urls = ["https://example.com/only"]
        config = _make_config(api_key=None)

        def fake_urlopen(req_or_url, timeout=None):
            raise _make_http_error(503, {
                "error_code": "extension_not_connected",
                "error_message": "No heartbeat.",
            })

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = save_urls(urls)

        assert result["total"] == 1
        assert result["failed"] == 1
        assert result["results"][0]["url"] == "https://example.com/only"
        assert result["results"][0]["error_code"] == "extension_not_connected"

    def test_timeout_uses_dynamic_batch_budget(self):
        urls = [f"https://example.com/{i}" for i in range(5)]
        config = _make_config(api_key=None)
        enqueue_counter = [0]

        def fake_urlopen(req_or_url, timeout=None):
            url_str = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
            if "/enqueue" in url_str:
                enqueue_counter[0] += 1
                return _make_urlopen_response({"request_id": f"req-{enqueue_counter[0]}"})
            raise urllib.error.URLError("connection refused")

        mono_values = [0.0] * 20 + [200.0] * 200 + [1000.0] * 500

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion.time.monotonic", side_effect=lambda: mono_values.pop(0) if mono_values else 1000.0), \
             patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = save_urls(urls)

        assert result["failed"] == 5
        assert all(entry["status"] == "timeout_likely_saved" for entry in result["results"])
        assert all(entry["poll_timeout_s"] == 225 for entry in result["results"])


class TestPreflightUrls:
    def test_all_urls_accessible(self):
        urls = ["https://arxiv.org/abs/1", "https://doi.org/10.1/test"]
        enqueue_counter = [0]

        def fake_urlopen(req_or_url, timeout=None):
            url_str = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
            if "/enqueue" in url_str:
                body = json.loads(req_or_url.data.decode())
                assert body["action"] == "preflight"
                enqueue_counter[0] += 1
                return _make_urlopen_response({"request_id": f"req-{enqueue_counter[0]}"})
            if "req-1" in url_str:
                return _make_urlopen_response({
                    "request_id": "req-1",
                    "status": "accessible",
                    "url": urls[0],
                    "title": "arXiv",
                    "final_url": urls[0],
                })
            if "req-2" in url_str:
                return _make_urlopen_response({
                    "request_id": "req-2",
                    "status": "accessible",
                    "url": urls[1],
                    "title": "DOI",
                    "final_url": "https://publisher.example/paper",
                })
            raise urllib.error.URLError("not found")

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch(
                 "zotpilot.tools.ingestion.time.monotonic",
                 side_effect=iter([0.0] * 20 + [1.0] * 20 + [1000.0] * 100),
             ), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            report = _preflight_urls(urls)

        assert report["checked"] == 2
        assert report["all_clear"] is True
        assert len(report["accessible"]) == 2
        assert report["blocked"] == []
        assert report["errors"] == []

    def test_blocked_url_sets_all_clear_false(self):
        urls = ["https://www.sciencedirect.com/science/article/pii/S1"]
        requests = []

        def fake_urlopen(req_or_url, timeout=None):
            url_str = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
            if "/enqueue" in url_str:
                body = json.loads(req_or_url.data.decode())
                requests.append(body)
                return _make_urlopen_response({"request_id": "req-1"})
            return _make_urlopen_response({
                "request_id": "req-1",
                "status": "anti_bot_detected",
                "url": urls[0],
                "title": "Just a moment...",
                "final_url": urls[0],
            })

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            report = _preflight_urls(urls)

        assert requests[0]["action"] == "preflight"
        assert report["all_clear"] is False
        assert report["blocked"][0]["title"] == "Just a moment..."

    def test_bridge_autostart_failure_returns_errors(self):
        urls = ["https://example.com/1", "https://example.com/2"]
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=False), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start", side_effect=RuntimeError("bridge down")):
            report = _preflight_urls(urls)

        assert report["all_clear"] is False
        assert len(report["errors"]) == 2
        assert all(entry["error"] == "bridge down" for entry in report["errors"])

    def test_timeout_classified_as_error(self):
        urls = ["https://example.com/timeout"]
        mono_values = iter([0.0] * 20 + [1000.0] * 500)

        def fake_urlopen(req_or_url, timeout=None):
            url_str = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
            if "/enqueue" in url_str:
                return _make_urlopen_response({"request_id": "req-timeout"})
            raise urllib.error.URLError("connection refused")

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion.time.monotonic", side_effect=mono_values), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            report = _preflight_urls(urls)

        assert report["all_clear"] is False
        assert "Timeout" in report["errors"][0]["error"]

    def test_pending_result_keeps_polling_until_accessible(self):
        urls = ["https://example.com/pending-then-ok"]
        poll_count = {"req-1": 0}

        def fake_urlopen(req_or_url, timeout=None):
            url_str = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
            if "/enqueue" in url_str:
                return _make_urlopen_response({"request_id": "req-1"})
            if "req-1" in url_str:
                poll_count["req-1"] += 1
                if poll_count["req-1"] == 1:
                    return _make_urlopen_response({
                        "request_id": "req-1",
                        "status": "pending",
                    })
                return _make_urlopen_response({
                    "request_id": "req-1",
                    "status": "accessible",
                    "url": urls[0],
                    "title": "ok",
                    "final_url": urls[0],
                })
            raise urllib.error.URLError("not found")

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            report = _preflight_urls(urls)

        assert report["all_clear"] is True
        assert report["errors"] == []
        assert len(report["accessible"]) == 1

    def test_sampling_caps_checks_at_five(self):
        urls = [f"https://publisher{i}.example.com/paper" for i in range(10)]
        enqueued = []

        def fake_urlopen(req_or_url, timeout=None):
            url_str = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
            if "/enqueue" in url_str:
                body = json.loads(req_or_url.data.decode())
                enqueued.append(body["url"])
                return _make_urlopen_response({"request_id": f"req-{len(enqueued)}"})
            request_id = url_str.rsplit("/", 1)[-1]
            index = int(request_id.split("-")[-1]) - 1
            return _make_urlopen_response({
                "request_id": request_id,
                "status": "accessible",
                "url": enqueued[index],
                "title": "ok",
                "final_url": enqueued[index],
            })

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            report = _preflight_urls(urls)

        assert report["checked"] == 5
        assert len(enqueued) == 5
        assert len(report["skipped"]) == 5

    def test_sampling_prefers_unique_publishers(self):
        urls = [
            "https://a.example.com/1",
            "https://a.example.com/2",
            "https://a.example.com/3",
            "https://b.example.com/1",
            "https://c.example.com/1",
            "https://d.example.com/1",
            "https://e.example.com/1",
            "https://f.example.com/1",
        ]
        enqueued = []

        def fake_urlopen(req_or_url, timeout=None):
            url_str = req_or_url.full_url if hasattr(req_or_url, "full_url") else str(req_or_url)
            if "/enqueue" in url_str:
                body = json.loads(req_or_url.data.decode())
                enqueued.append(body["url"])
                return _make_urlopen_response({"request_id": f"req-{len(enqueued)}"})
            request_id = url_str.rsplit("/", 1)[-1]
            index = int(request_id.split("-")[-1]) - 1
            return _make_urlopen_response({
                "request_id": request_id,
                "status": "accessible",
                "url": enqueued[index],
                "title": "ok",
                "final_url": enqueued[index],
            })

        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            report = _preflight_urls(urls)

        sampled_domains = {url.split("/")[2] for url in enqueued}
        assert report["checked"] == 5
        assert len(sampled_domains) == 5


# ---------------------------------------------------------------------------
# TestIngestPapersPdfVerification
# ---------------------------------------------------------------------------

class TestIngestPapersPdfVerification:
    """Verify that ingest_papers checks actual PDF status via Zotero API after save."""

    def _fake_save_urls_result(self, url, item_key, title="Paper"):
        return {
            "total": 1, "succeeded": 1, "failed": 0,
            "results": [{"success": True, "url": url, "item_key": item_key, "title": title}],
        }

    def _preflight_all_clear(self, urls):
        """Return a preflight report indicating all URLs are accessible."""
        return {
            "checked": len(urls),
            "accessible": [{"url": u, "title": "", "final_url": u} for u in urls],
            "blocked": [],
            "skipped": [],
            "errors": [],
            "all_clear": True,
        }

    def test_pdf_retry_eventually_reports_attached(self):
        """PDF check retries in batch and upgrades to attached once Zotero finishes downloading."""
        writer = _make_writer()
        writer.check_has_pdf.side_effect = [False, True]
        url = "https://www.sciencedirect.com/science/article/pii/S0000000000000001"
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key=None)), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._preflight_urls", side_effect=self._preflight_all_clear), \
             patch("zotpilot.tools.ingestion.save_urls",
                   return_value=self._fake_save_urls_result(url, "ITEM1")):
            result = ingest_papers([{"doi": "10.1016/S0000", "landing_page_url": url, "is_oa": False}])
        entry = result["results"][0]
        assert entry["status"] == "ingested"
        assert entry["pdf"] == "attached"
        assert "warning" not in entry
        assert writer.check_has_pdf.call_count == 2

    def test_pdf_missing_after_retries_reports_warning(self):
        """After 3 rounds with no PDF attachment, pdf remains none and includes warning."""
        writer = _make_writer()
        writer.check_has_pdf.return_value = False
        url = "https://www.sciencedirect.com/science/article/pii/S0000000000000002"
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key=None)), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._preflight_urls", side_effect=self._preflight_all_clear), \
             patch("zotpilot.tools.ingestion.save_urls",
                   return_value=self._fake_save_urls_result(url, "ITEM2")):
            result = ingest_papers([{"doi": "10.1016/S0001", "landing_page_url": url, "is_oa": False}])
        entry = result["results"][0]
        assert entry["status"] == "ingested"
        assert entry["pdf"] == "none"
        assert "warning" in entry
        assert "robot verification" in entry["warning"].lower() or "pdf not attached" in entry["warning"].lower()
        # Dynamic budget: n=1 → 30s / 5s = 6 polls + 1 initial check = 7 calls.
        # Don't hard-code; just assert retries happened.
        assert writer.check_has_pdf.call_count >= 2

    def test_no_item_key_skips_pdf_check(self):
        """When item_key is None (routing failed), check_has_pdf is not called."""
        writer = _make_writer()
        url = "https://www.sciencedirect.com/science/article/pii/S0000000000000003"
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key=None)), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._preflight_urls", side_effect=self._preflight_all_clear), \
             patch("zotpilot.tools.ingestion.save_urls",
                   return_value=self._fake_save_urls_result(url, None)):
            result = ingest_papers([{"doi": "10.1016/S0002", "landing_page_url": url, "is_oa": False}])
        entry = result["results"][0]
        assert entry["status"] == "ingested"
        assert entry["pdf"] == "none"
        writer.check_has_pdf.assert_not_called()

    def test_dedup_hit_skips_save_and_returns_existing_item(self):
        writer = _make_writer()
        writer.check_has_pdf.return_value = True
        url = "https://publisher.example.com/paper"
        with patch.dict(
            "zotpilot.tools.ingestion._recently_saved_dois",
            {"10.1000/existing": 9999999999.0},
            clear=True,
        ), \
             patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key=None)), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._preflight_urls", side_effect=self._preflight_all_clear), \
             patch("zotpilot.tools.ingestion._discover_saved_item_key", return_value="EXISTING1") as discover_mock, \
             patch("zotpilot.tools.ingestion.save_urls") as save_urls_mock:
            result = ingest_papers([{
                "doi": "10.1000/existing",
                "landing_page_url": url,
                "title": "Existing Paper",
            }])

        save_urls_mock.assert_not_called()
        discover_mock.assert_called_once()
        assert discover_mock.call_args.kwargs["window_s"] == 300
        entry = result["results"][0]
        assert entry["status"] == "already_in_library"
        assert entry["item_key"] == "EXISTING1"
        assert entry["pdf"] == "attached"
        assert result["skipped_duplicates"] == 1

    def test_dedup_miss_proceeds_to_save(self):
        writer = _make_writer()
        writer.check_has_pdf.return_value = True
        url = "https://publisher.example.com/new"
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key=None)), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._preflight_urls", side_effect=self._preflight_all_clear), \
             patch("zotpilot.tools.ingestion._discover_saved_item_key", return_value=None), \
             patch("zotpilot.tools.ingestion.save_urls",
                   return_value=self._fake_save_urls_result(url, "ITEM3")) as save_urls_mock:
            result = ingest_papers([{
                "doi": "10.1000/new",
                "landing_page_url": url,
                "title": "New Paper",
            }])

        save_urls_mock.assert_called_once()
        entry = result["results"][0]
        assert entry["status"] == "ingested"
        assert entry["item_key"] == "ITEM3"

    def test_timeout_likely_saved_discovers_item_and_reports_pdf_summary(self):
        writer = _make_writer()
        writer.check_has_pdf.return_value = False
        url = "https://publisher.example.com/slow"
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key=None)), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._preflight_urls", side_effect=self._preflight_all_clear), \
             patch("zotpilot.tools.ingestion._discover_saved_item_key", return_value="SLOW1") as discover_mock, \
             patch("zotpilot.tools.ingestion.save_urls", return_value={
                 "total": 1,
                 "succeeded": 0,
                 "failed": 1,
                 "results": [{
                     "success": False,
                     "status": "timeout_likely_saved",
                     "title": "Slow Paper",
                     "url": url,
                     "error": "save confirmation timed out",
                 }],
             }):
            result = ingest_papers([{
                "doi": "10.1000/slow",
                "landing_page_url": url,
                "title": "Slow Paper",
            }])

        assert discover_mock.call_args.kwargs["window_s"] == 210
        entry = result["results"][0]
        assert entry["status"] == "timeout_likely_saved"
        assert entry["item_key"] == "SLOW1"
        assert entry["pdf"] == "none"
        assert result["pdf_summary"] == {"attached": 0, "none": 1, "unknown": 0}

    def test_error_page_without_item_key_attempts_discovery_and_delete(self):
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = ["BAD1"]
        config = _make_config(api_key="KEY")
        result = {
            "success": True,
            "url": "https://www.sciencedirect.com/science/article/pii/bad",
            "title": "Page not found | ScienceDirect",
        }
        with patch("zotpilot.tools.ingestion._get_config", return_value=config), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            out = _apply_bridge_result_routing(result, None, None)

        assert out["success"] is False
        assert out["error_code"] == "error_page_detected"
        writer.delete_item.assert_called_once_with("BAD1")

    def test_timeout_likely_saved_uses_reported_poll_timeout_for_discovery_window(self):
        writer = _make_writer()
        writer.check_has_pdf.return_value = False
        url = "https://publisher.example.com/slower"
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start"), \
             patch("zotpilot.tools.ingestion.time.sleep"), \
             patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(api_key=None)), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion._preflight_urls", side_effect=self._preflight_all_clear), \
             patch("zotpilot.tools.ingestion._discover_saved_item_key", return_value="SLOW2") as discover_mock, \
             patch("zotpilot.tools.ingestion.save_urls", return_value={
                 "total": 1,
                 "succeeded": 0,
                 "failed": 1,
                 "results": [{
                     "success": False,
                     "status": "timeout_likely_saved",
                     "poll_timeout_s": 225,
                     "title": "Slow Paper",
                     "url": url,
                     "error": "save confirmation timed out",
                 }],
             }):
            ingest_papers([{
                "doi": "10.1000/slower",
                "landing_page_url": url,
                "title": "Slow Paper",
            }])

        assert discover_mock.call_args.kwargs["window_s"] == 285
