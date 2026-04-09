"""Tests for bridge-dependent ingestion paths."""
from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from zotpilot.tools import ingestion_bridge
from zotpilot.tools.ingestion import _apply_bridge_result_routing, save_urls


def _make_config(api_key="KEY"):
    config = MagicMock()
    config.zotero_api_key = api_key
    return config


def _make_writer():
    writer = MagicMock()
    writer.find_items_by_url_and_title.return_value = []
    writer.add_to_collection.return_value = None
    writer.add_item_tags.return_value = None
    writer.set_item_tags.return_value = None
    writer.get_item_type.return_value = "journalArticle"
    writer.delete_item.return_value = True
    return writer


def _make_urlopen_response(body: dict, status: int = 200):
    mock = MagicMock()
    mock.status = status
    mock.read.return_value = json.dumps(body).encode()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _make_http_error(code: int, body: dict | None = None):
    body_bytes = json.dumps(body).encode() if body else b""
    return urllib.error.HTTPError(
        url="http://127.0.0.1:9999/enqueue",
        code=code,
        msg="Error",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(body_bytes),
    )


class TestSaveUrls:
    def test_empty_urls_raise_tool_error(self):
        with pytest.raises(ToolError, match="cannot be empty"):
            save_urls([])

    def test_more_than_10_urls_raise_tool_error(self):
        with pytest.raises(ToolError, match="Too many URLs"):
            save_urls([f"https://example.com/{i}" for i in range(11)])

    def test_defaults_to_inbox_collection(self):
        with patch("zotpilot.tools.ingestion._ensure_inbox_collection", return_value="INBOX1"), \
             patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.enqueue_save_request", return_value=("req-1", None)), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.poll_batch_save_results", return_value=[
                 {"success": True, "url": "https://example.com", "item_key": "ITEM1"},
             ]):
            result = save_urls(["https://example.com"])

        assert result["collection_used"] == "INBOX1"

    def test_auto_start_failure_returns_top_level_error(self):
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=False), \
             patch("zotpilot.tools.ingestion.BridgeServer.auto_start", side_effect=RuntimeError("Bridge failed")):
            result = save_urls(["https://example.com"])

        assert result["success"] is False
        assert result["results"] == []

    def test_json_string_urls_are_supported(self):
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.get_extension_status",
                   return_value={"extension_connected": True}), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.enqueue_save_request", return_value=("req-1", None)), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.poll_batch_save_results", return_value=[
                 {"success": True, "url": "https://example.com", "item_key": "ITEM1"},
             ]):
            result = save_urls('["https://example.com"]')

        assert result["total"] == 1
        assert result["succeeded"] == 1

    def test_enqueue_errors_are_preserved(self):
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.get_extension_status",
                   return_value={"extension_connected": True}), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.enqueue_save_request", return_value=(None, {
                 "success": False,
                 "error_code": "extension_not_connected",
                 "error_message": "No heartbeat",
             })), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.poll_batch_save_results", return_value=[]):
            result = save_urls(["https://example.com"])

        assert result["failed"] == 1
        assert result["results"][0]["url"] == "https://example.com"
        assert result["results"][0]["error_code"] == "extension_not_connected"

    def test_tags_json_string_is_parsed(self):
        with patch("zotpilot.tools.ingestion.BridgeServer.is_running", return_value=True), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.get_extension_status",
                   return_value={"extension_connected": True}), \
             patch("zotpilot.tools.ingestion.ingestion_bridge.poll_batch_save_results", return_value=[]), \
             patch(
                 "zotpilot.tools.ingestion.ingestion_bridge.enqueue_save_request",
                 return_value=("req-1", None),
             ) as enqueue_mock:
            save_urls(["https://example.com"], tags='["ml","nlp"]')

        assert enqueue_mock.call_args.args[3] == ["ml", "nlp"]



class TestApplyBridgeResultRouting:
    def test_routing_applies_collection_and_tags(self):
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = ["ITEM1"]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config("KEY")), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion.time.sleep"):
            result = _apply_bridge_result_routing(
                {"success": True, "url": "https://example.com", "title": "Paper"},
                "COL1",
                ["tag1"],
            )

        assert result["item_key"] == "ITEM1"
        writer.add_to_collection.assert_called_once_with("ITEM1", "COL1")
        writer.add_item_tags.assert_called_once_with("ITEM1", ["tag1"])
        assert result["routing_status"] == "routed_by_backend"

    def test_routing_applied_true_skips_backend_routing(self):
        writer = _make_writer()
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config("KEY")), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            result = _apply_bridge_result_routing(
                {
                    "success": True,
                    "url": "https://example.com",
                    "title": "Paper",
                    "item_key": "ITEM1",
                    "routing_applied": True,
                },
                "COL1",
                ["tag1"],
            )

        assert result["routing_status"] == "routed_by_connector"
        writer.add_to_collection.assert_not_called()
        writer.add_item_tags.assert_not_called()

    def test_routing_applied_false_falls_back_to_backend(self):
        writer = _make_writer()
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config("KEY")), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion.time.sleep"):
            result = _apply_bridge_result_routing(
                {
                    "success": True,
                    "url": "https://example.com",
                    "title": "Paper",
                    "item_key": "ITEM1",
                    "routing_applied": False,
                },
                "COL1",
                ["tag1"],
            )

        assert result["routing_status"] == "routed_by_backend"
        writer.add_to_collection.assert_called_once_with("ITEM1", "COL1")
        writer.add_item_tags.assert_called_once_with("ITEM1", ["tag1"])

    def test_routing_applied_false_without_item_key_is_deferred(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config("KEY")):
            result = _apply_bridge_result_routing(
                {
                    "success": True,
                    "url": "https://example.com",
                    "title": "Paper",
                    "item_key": None,
                    "routing_applied": False,
                },
                "COL1",
                None,
            )

        assert result["routing_status"] == "routing_deferred"
        assert "post-batch reconciliation" in result["warning"]

    def test_missing_routing_applied_uses_legacy_backend_path(self):
        writer = _make_writer()
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config("KEY")), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer), \
             patch("zotpilot.tools.ingestion.time.sleep"):
            result = _apply_bridge_result_routing(
                {"success": True, "url": "https://example.com", "title": "Paper", "item_key": "ITEM1"},
                "COL1",
                None,
            )

        assert result["routing_status"] == "routed_by_backend"
        writer.add_to_collection.assert_called_once_with("ITEM1", "COL1")

    def test_missing_api_key_returns_warning(self):
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config(None)):
            result = _apply_bridge_result_routing(
                {"success": True, "url": "https://example.com", "title": "Paper"},
                "COL1",
                ["tag1"],
            )

        assert "warning" in result
        assert "ZOTERO_API_KEY" in result["warning"]

    def test_error_page_is_rejected(self):
        writer = _make_writer()
        writer.find_items_by_url_and_title.return_value = ["BAD1"]
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config("KEY")), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            result = _apply_bridge_result_routing(
                {"success": True, "url": "https://example.com", "title": "Page not found | ScienceDirect"},
                None,
                None,
            )

        assert result["success"] is False
        assert result["error_code"] == "error_page_detected"
        writer.delete_item.assert_called_once_with("BAD1")

    def test_arxiv_urls_clear_publisher_tags(self):
        writer = _make_writer()
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config("KEY")), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            result = _apply_bridge_result_routing(
                {
                    "success": True,
                    "url": "https://arxiv.org/abs/2401.00001",
                    "title": "Paper",
                    "item_key": "ITEM1",
                    "routing_applied": True,
                },
                "COL1",
                None,
            )

        assert result["routing_status"] == "routed_by_connector"
        writer.set_item_tags.assert_called_once_with("ITEM1", [])

    def test_non_publisher_urls_also_clear_tags(self):
        writer = _make_writer()
        with patch("zotpilot.tools.ingestion._get_config", return_value=_make_config("KEY")), \
             patch("zotpilot.tools.ingestion._get_writer", return_value=writer):
            _apply_bridge_result_routing(
                {
                    "success": True,
                    "url": "https://example.com/paper",
                    "title": "Paper",
                    "item_key": "ITEM1",
                    "routing_applied": True,
                },
                "COL1",
                None,
            )

        writer.set_item_tags.assert_called_once_with("ITEM1", [])


class TestBridgeHelpers:
    def test_discovery_backoff_is_shortened(self):
        assert ingestion_bridge.DISCOVERY_BACKOFF_DELAYS == [2.0, 4.0, 8.0, 16.0, 32.0]

    def test_enqueue_save_request_handles_503(self):
        error = _make_http_error(503, {
            "error_code": "extension_not_connected",
            "error_message": "No heartbeat",
        })
        with patch("urllib.request.urlopen", side_effect=error):
            request_id, enqueue_error = ingestion_bridge.enqueue_save_request(
                "http://127.0.0.1:9999",
                "https://example.com",
                None,
                None,
            )

        assert request_id is None
        assert enqueue_error["error_code"] == "extension_not_connected"

    def test_poll_single_save_result_times_out(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")), \
             patch("time.sleep"), \
             patch("time.monotonic", side_effect=[0.0, 0.0, 10.0]):
            result = ingestion_bridge.poll_single_save_result(
                "http://127.0.0.1:9999",
                "req-1",
                5.0,
            )

        assert result["status"] == "timeout_likely_saved"


class TestA1NoZombieWaitForExtension:
    """A1: Only one wait_for_extension should exist and it must return dict."""

    def test_wait_for_extension_returns_dict(self):
        """The surviving wait_for_extension returns a dict (not bool)."""
        calls = []

        def fake_monotonic():
            calls.append(1)
            # First call: deadline setup; subsequent calls past deadline
            return 0.0 if len(calls) == 1 else 999.0

        result = ingestion_bridge.wait_for_extension(
            "http://127.0.0.1:9999",
            timeout_s=1.0,
            sleep_fn=lambda _: None,
            monotonic_fn=fake_monotonic,
        )
        assert isinstance(result, dict)

    def test_wait_for_extension_returns_connected_status(self):
        """Returns last /status payload with extension_connected=True on success."""
        responses = [
            json.dumps({"extension_connected": True, "bridge": "running"}).encode()
        ]
        mock_resp = MagicMock()
        mock_resp.read.return_value = responses[0]

        monotonic_values = iter([0.0, 0.5])

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = ingestion_bridge.wait_for_extension(
                "http://127.0.0.1:9999",
                timeout_s=5.0,
                sleep_fn=lambda _: None,
                monotonic_fn=lambda: next(monotonic_values),
            )

        assert result.get("extension_connected") is True


class TestA2FIFODeadlines:
    """A2: poll_batch_save_results uses FIFO-position-based per-URL deadlines."""

    def test_deadlines_are_fifo_staggered(self):
        """The k-th URL gets deadline t0 + k * per_url_timeout_s."""
        t0 = 1000.0
        per_url = 150.0
        call_count = 0

        def fake_monotonic():
            nonlocal call_count
            call_count += 1
            # First N+1 calls are setup: N per-URL deadlines + 1 overall_deadline.
            # After that, return time well beyond overall_deadline so the loop
            # exits immediately via the while condition.
            return t0 if call_count <= 4 else t0 + 9999.0

        urls = {
            "req-1": "https://arxiv.org/abs/1",
            "req-2": "https://arxiv.org/abs/2",
            "req-3": "https://arxiv.org/abs/3",
        }

        # The function will hit overall deadline immediately and fall through to
        # the setdefault block — so all results will be timeout entries.
        results = ingestion_bridge.poll_batch_save_results(
            bridge_url="http://127.0.0.1:9999",
            id_to_url=urls,
            per_url_timeout_s=per_url,
            overall_timeout_s=per_url * len(urls) + 120,
            apply_bridge_result_routing_fn=lambda r, c, t: r,
            collection_key=None,
            tags=None,
            logger=MagicMock(),
            sleep_fn=lambda _: None,
            monotonic_fn=fake_monotonic,
        )

        # All timed out because fake_monotonic returns past overall_deadline after t0
        assert len(results) == 3
        for r in results:
            assert r["success"] is False

    def test_overall_timeout_scales_with_batch_size(self):
        """compute_save_result_poll_overall_timeout_s grows linearly with N."""
        t1 = ingestion_bridge.compute_save_result_poll_overall_timeout_s(1)
        t3 = ingestion_bridge.compute_save_result_poll_overall_timeout_s(3)
        t7 = ingestion_bridge.compute_save_result_poll_overall_timeout_s(7)
        # With N=3, overall should be at least 3x per-URL + grace
        per_url = ingestion_bridge.compute_save_result_poll_timeout_s(3)
        assert t3 >= 3 * per_url
        # N=7 overall should be strictly greater than N=1 overall
        assert t7 > t1


class TestA3DomainGranularPreflight:
    """A3: _classify_preflight_error_code and domain-granular blocking."""

    def test_classify_anti_bot_by_status(self):
        result = ingestion_bridge._classify_preflight_error_code(
            {"status": "anti_bot_detected", "title": "Just a moment...", "error": ""}
        )
        assert result == "anti_bot_detected"

    def test_classify_anti_bot_by_cloudflare_title(self):
        result = ingestion_bridge._classify_preflight_error_code(
            {"status": "error", "title": "Cloudflare security check", "error": ""}
        )
        assert result == "anti_bot_detected"

    def test_classify_subscription_required(self):
        result = ingestion_bridge._classify_preflight_error_code(
            {"status": "error", "title": "Subscribe to read full article", "error": ""}
        )
        assert result == "subscription_required"

    def test_classify_preflight_timeout(self):
        result = ingestion_bridge._classify_preflight_error_code(
            {"status": "error", "title": "", "error": "Timeout (60s) — page did not load"}
        )
        assert result == "preflight_timeout"

    def test_classify_preflight_failed_fallback(self):
        result = ingestion_bridge._classify_preflight_error_code(
            {"status": "error", "title": "Unknown error", "error": "connection refused"}
        )
        assert result == "preflight_failed"


class TestA4PreflightErrorCodes:
    """A4: preflight_urls returns error_code on blocked and error entries."""

    def _run_preflight_with_result(self, connector_result: dict) -> dict:
        """Helper: run preflight_urls with a stubbed bridge that returns connector_result."""
        import uuid

        request_id = str(uuid.uuid4())
        enqueue_response = json.dumps({"request_id": request_id}).encode()
        result_response = json.dumps(connector_result).encode()

        call_count = 0

        def fake_urlopen(req_or_url, timeout=None):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            mock.status = 200
            url_str = req_or_url if isinstance(req_or_url, str) else getattr(req_or_url, "full_url", str(req_or_url))
            if "enqueue" in str(url_str):
                mock.read.return_value = enqueue_response
            else:
                mock.read.return_value = result_response
            mock.__enter__ = lambda s: s
            mock.__exit__ = MagicMock(return_value=False)
            return mock

        bridge_cls = MagicMock()
        bridge_cls.is_running.return_value = True  # bridge already running

        tick = [0.0]

        def fake_monotonic():
            tick[0] += 0.1
            return tick[0]

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            return ingestion_bridge.preflight_urls(
                urls=["https://example.com/paper"],
                sample_size=5,
                default_port=9999,
                bridge_server_cls=bridge_cls,
                logger=MagicMock(),
                sleep_fn=lambda _: None,
                monotonic_fn=fake_monotonic,
            )

    def test_blocked_entry_has_error_code(self):
        report = self._run_preflight_with_result({
            "status": "anti_bot_detected",
            "title": "Just a moment...",
            "final_url": "https://example.com/paper",
        })
        assert len(report["blocked"]) == 1
        assert report["blocked"][0]["error_code"] == "anti_bot_detected"

    def test_error_entry_has_error_code_on_timeout(self):
        report = self._run_preflight_with_result({
            "status": "error",
            "title": "",
            "error": "Timeout (60s) — page did not finish loading in time.",
            "error_code": "preflight_timeout",
        })
        assert len(report["errors"]) == 1
        assert report["errors"][0]["error_code"] == "preflight_timeout"

    def test_subscription_wall_classified_correctly(self):
        report = self._run_preflight_with_result({
            "status": "error",
            "title": "Subscribe to access full article",
            "error": "",
        })
        assert len(report["errors"]) == 1
        assert report["errors"][0]["error_code"] == "subscription_required"
