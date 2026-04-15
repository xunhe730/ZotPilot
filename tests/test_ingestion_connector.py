"""Tests for ingestion connector module (v0.5.0)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# validate_saved_item tests
# ---------------------------------------------------------------------------

class TestValidateSavedItem:
    def test_valid_journal_article(self):
        from zotpilot.tools.ingestion.connector import validate_saved_item

        mock_writer = MagicMock()
        mock_writer._zot.item.return_value = {
            "data": {"itemType": "journalArticle", "title": "Deep Learning for PDEs"},
        }

        result = validate_saved_item(
            "ITEM1", get_writer=lambda: mock_writer,
        )
        assert result["valid"] is True
        assert result["item_type"] == "journalArticle"
        assert result["title"] == "Deep Learning for PDEs"
        assert result["reason"] is None

    def test_invalid_webpage_type(self):
        from zotpilot.tools.ingestion.connector import validate_saved_item

        mock_writer = MagicMock()
        mock_writer._zot.item.return_value = {
            "data": {"itemType": "webpage", "title": "Some Page"},
        }

        result = validate_saved_item(
            "ITEM2", get_writer=lambda: mock_writer,
        )
        assert result["valid"] is False
        assert "invalid_item_type" in result["reason"]

    def test_snapshot_title(self):
        from zotpilot.tools.ingestion.connector import validate_saved_item

        mock_writer = MagicMock()
        mock_writer._zot.item.return_value = {
            "data": {"itemType": "journalArticle", "title": "Snapshot"},
        }

        result = validate_saved_item(
            "ITEM3", get_writer=lambda: mock_writer,
        )
        assert result["valid"] is False
        assert result["reason"] == "title_is_snapshot"

    def test_url_title(self):
        from zotpilot.tools.ingestion.connector import validate_saved_item

        mock_writer = MagicMock()
        mock_writer._zot.item.return_value = {
            "data": {
                "itemType": "journalArticle",
                "title": "https://www.sciencedirect.com/science/article/pii/S123",
            },
        }

        result = validate_saved_item(
            "ITEM4", get_writer=lambda: mock_writer,
        )
        assert result["valid"] is False
        assert result["reason"] == "title_is_url"

    def test_error_page_title(self):
        from zotpilot.tools.ingestion.connector import validate_saved_item

        mock_writer = MagicMock()
        mock_writer._zot.item.return_value = {
            "data": {"itemType": "journalArticle", "title": "Access Denied"},
        }

        result = validate_saved_item(
            "ITEM5", get_writer=lambda: mock_writer,
        )
        assert result["valid"] is False
        assert result["reason"] == "error_page_title"


# ---------------------------------------------------------------------------
# save_single_and_verify tests
# ---------------------------------------------------------------------------

class TestSaveSingleAndVerify:
    @patch("zotpilot.tools.ingestion.connector.check_pdf_status", return_value="attached")
    @patch("zotpilot.tools.ingestion.connector.validate_saved_item")
    @patch("zotpilot.tools.ingestion.connector.poll_single_save_result")
    @patch("zotpilot.tools.ingestion.connector.enqueue_save_request")
    def test_success_path(
        self, mock_enqueue, mock_poll, mock_validate, mock_pdf,
    ):
        from zotpilot.tools.ingestion.connector import save_single_and_verify

        mock_enqueue.return_value = ("req-1", None)
        mock_poll.return_value = {"success": True, "item_key": "KEY1", "title": "Paper"}
        mock_validate.return_value = {
            "valid": True, "item_type": "journalArticle",
            "title": "Paper", "reason": None,
        }

        mock_writer = MagicMock()
        result = save_single_and_verify(
            "https://arxiv.org/abs/2301.0001",
            doi="10.48550/arxiv.2301.0001",
            title="Paper",
            collection_key=None, tags=None,
            bridge_url="http://127.0.0.1:23119",
            get_writer=lambda: mock_writer,
            writer_lock=MagicMock(),
        )

        assert result["status"] == "saved_with_pdf"
        assert result["item_key"] == "KEY1"
        assert result["has_pdf"] is True
        assert result["method"] == "connector"

    @patch("zotpilot.tools.ingestion.connector.check_pdf_status")
    @patch("zotpilot.tools.ingestion.connector.validate_saved_item")
    @patch("zotpilot.tools.ingestion.connector.poll_single_save_result")
    @patch("zotpilot.tools.ingestion.connector.enqueue_save_request")
    def test_pdf_confirmed_verified_via_local_api(
        self, mock_enqueue, mock_poll, mock_validate, mock_pdf,
    ):
        """When connector reports pdf_connector_confirmed, we still verify
        via local API (short 10s window) to guard against false positives
        from Springer embedded-PDF pages."""
        from zotpilot.tools.ingestion.connector import save_single_and_verify

        mock_enqueue.return_value = ("req-1", None)
        mock_poll.return_value = {
            "success": True,
            "item_key": "KEY1",
            "title": "Paper",
            "pdf_connector_confirmed": True,
        }
        mock_validate.return_value = {
            "valid": True, "item_type": "journalArticle",
            "title": "Paper", "reason": None,
        }
        mock_pdf.return_value = "attached"

        mock_writer = MagicMock()
        result = save_single_and_verify(
            "https://example.com/paper",
            doi="10.1234/test",
            title="Paper",
            collection_key=None, tags=None,
            bridge_url="http://127.0.0.1:23119",
            get_writer=lambda: mock_writer,
            writer_lock=MagicMock(),
        )

        mock_pdf.assert_called_once()
        assert mock_pdf.call_args.kwargs.get("timeout_s") == 10.0
        assert result["status"] == "saved_with_pdf"
        assert result["has_pdf"] is True

    @patch("zotpilot.tools.ingestion.connector.check_pdf_status")
    @patch("zotpilot.tools.ingestion.connector.validate_saved_item")
    @patch("zotpilot.tools.ingestion.connector.poll_single_save_result")
    @patch("zotpilot.tools.ingestion.connector.enqueue_save_request")
    def test_pdf_failed_verifies_then_uses_oa_fallback(
        self, mock_enqueue, mock_poll, mock_validate, mock_pdf,
    ):
        """When connector reports pdf_failed, we still verify via local API
        (full 30s window) then run OA fallback when PDF is absent."""
        from zotpilot.tools.ingestion.connector import save_single_and_verify

        mock_enqueue.return_value = ("req-1", None)
        mock_poll.return_value = {
            "success": True,
            "item_key": "KEY1",
            "title": "Paper",
            "pdf_failed": True,
        }
        mock_validate.return_value = {
            "valid": True, "item_type": "journalArticle",
            "title": "Paper", "reason": None,
        }
        mock_pdf.return_value = "none"

        resolver = MagicMock()
        resolver.resolve.return_value = SimpleNamespace(
            doi="10.1234/test",
            oa_url="https://example.com/paper.pdf",
            arxiv_id=None,
        )
        mock_writer = MagicMock()
        mock_writer.try_attach_oa_pdf.return_value = "attached"

        with patch("zotpilot.state._get_resolver", return_value=resolver):
            result = save_single_and_verify(
                "https://example.com/paper",
                doi="10.1234/test",
                title="Paper",
                collection_key=None, tags=None,
                bridge_url="http://127.0.0.1:23119",
                get_writer=lambda: mock_writer,
                writer_lock=MagicMock(),
            )

        mock_pdf.assert_called_once()
        mock_writer.try_attach_oa_pdf.assert_called_once()
        assert result["status"] == "saved_with_pdf"
        assert result["has_pdf"] is True

    @patch("zotpilot.tools.ingestion.connector.check_pdf_status", return_value="attached")
    @patch("zotpilot.tools.ingestion.connector.validate_saved_item")
    @patch("zotpilot.tools.ingestion.connector.poll_single_save_result")
    @patch("zotpilot.tools.ingestion.connector.enqueue_save_request")
    def test_no_pdf_signal_uses_pdf_poll(
        self, mock_enqueue, mock_poll, mock_validate, mock_pdf,
    ):
        from zotpilot.tools.ingestion.connector import save_single_and_verify

        mock_enqueue.return_value = ("req-1", None)
        mock_poll.return_value = {
            "success": True,
            "item_key": "KEY1",
            "title": "Paper",
        }
        mock_validate.return_value = {
            "valid": True, "item_type": "journalArticle",
            "title": "Paper", "reason": None,
        }

        result = save_single_and_verify(
            "https://example.com/paper",
            doi=None,
            title="Paper",
            collection_key=None, tags=None,
            bridge_url="http://127.0.0.1:23119",
            get_writer=lambda: MagicMock(),
            writer_lock=MagicMock(),
        )

        mock_pdf.assert_called_once()
        assert result["status"] == "saved_with_pdf"
        assert result["has_pdf"] is True

    @patch("zotpilot.tools.ingestion.connector.check_pdf_status", return_value="attached")
    @patch("zotpilot.tools.ingestion.connector.validate_saved_item")
    @patch("zotpilot.tools.ingestion.connector.poll_single_save_result")
    @patch("zotpilot.tools.ingestion.connector.enqueue_save_request")
    def test_generic_attachment_failure_without_signal_keeps_old_behavior(
        self, mock_enqueue, mock_poll, mock_validate, mock_pdf,
    ):
        from zotpilot.tools.ingestion.connector import save_single_and_verify

        mock_enqueue.return_value = ("req-1", None)
        mock_poll.return_value = {
            "success": True,
            "item_key": "KEY1",
            "title": "Paper",
            "error_code": "pdf_download_failed",
            "error": "PDF download failed",
        }
        mock_validate.return_value = {
            "valid": True, "item_type": "journalArticle",
            "title": "Paper", "reason": None,
        }

        result = save_single_and_verify(
            "https://example.com/paper",
            doi=None,
            title="Paper",
            collection_key=None, tags=None,
            bridge_url="http://127.0.0.1:23119",
            get_writer=lambda: MagicMock(),
            writer_lock=MagicMock(),
        )

        mock_pdf.assert_called_once()
        assert result["status"] == "saved_with_pdf"
        assert result["has_pdf"] is True

    @patch("zotpilot.tools.ingestion.connector._doi_api_fallback")
    @patch("zotpilot.tools.ingestion.connector.delete_item_safe", return_value=True)
    @patch("zotpilot.tools.ingestion.connector.validate_saved_item")
    @patch("zotpilot.tools.ingestion.connector.poll_single_save_result")
    @patch("zotpilot.tools.ingestion.connector.enqueue_save_request")
    def test_invalid_webpage_retries_then_falls_back_to_api(
        self, mock_enqueue, mock_poll, mock_validate, mock_delete, mock_fallback,
    ):
        """When Connector saves a webpage (JS throttling in background tab),
        retry once with a fresh tab.  If retry also returns webpage, fall back
        to API.  delete_item_safe is called twice (once per attempt)."""
        from zotpilot.tools.ingestion.connector import save_single_and_verify

        mock_enqueue.return_value = ("req-1", None)
        mock_poll.return_value = {"success": True, "item_key": "JUNK1", "title": "Snapshot"}
        mock_validate.return_value = {
            "valid": False, "item_type": "webpage",
            "title": "Snapshot", "reason": "invalid_item_type:webpage",
        }
        mock_fallback.return_value = {
            "status": "saved_metadata_only", "method": "api_fallback",
            "item_key": "API1", "has_pdf": False, "title": "Real Paper",
            "action_required": None, "warning": "Created via DOI API",
        }

        result = save_single_and_verify(
            "https://example.com/paper",
            doi="10.1234/test",
            title="Real Paper",
            collection_key=None, tags=None,
            bridge_url="http://127.0.0.1:23119",
            get_writer=lambda: MagicMock(),
            writer_lock=MagicMock(),
        )

        assert result["status"] == "saved_metadata_only"
        assert result["method"] == "api_fallback"
        assert mock_delete.call_count == 2  # once per attempt
        mock_fallback.assert_called_once()

    @patch("zotpilot.tools.ingestion.connector.poll_single_save_result")
    @patch("zotpilot.tools.ingestion.connector.enqueue_save_request")
    def test_anti_bot_returns_blocked(self, mock_enqueue, mock_poll):
        from zotpilot.tools.ingestion.connector import save_single_and_verify

        mock_enqueue.return_value = ("req-1", None)
        mock_poll.return_value = {
            "success": False,
            "title": "Just a moment...",
            "item_key": None,
        }

        result = save_single_and_verify(
            "https://example.com/paper",
            doi=None, title="Paper",
            collection_key=None, tags=None,
            bridge_url="http://127.0.0.1:23119",
            get_writer=lambda: MagicMock(),
            writer_lock=MagicMock(),
        )

        assert result["status"] == "blocked"
        assert result["error"] == "anti_bot_detected"
        assert result["action_required"] is not None


# ---------------------------------------------------------------------------
# looks_like_error_page_title tests
# ---------------------------------------------------------------------------

class TestLooksLikeErrorPageTitle:
    def test_error_page_patterns(self):
        from zotpilot.tools.ingestion.connector import looks_like_error_page_title

        assert looks_like_error_page_title("Page Not Found", None) is True
        assert looks_like_error_page_title("404 - Not Found", None) is True
        assert looks_like_error_page_title("Access Denied", None) is True
        assert looks_like_error_page_title("", None) is False
        # With item_key, generic patterns are skipped
        assert looks_like_error_page_title("Deep Learning Paper", "KEY1") is False


# ---------------------------------------------------------------------------
# sample_preflight_urls tests
# ---------------------------------------------------------------------------

class TestSamplePreflightUrls:
    def test_sample_preserves_publisher_diversity(self):
        from zotpilot.tools.ingestion.connector import sample_preflight_urls

        urls = [
            "https://nature.com/paper1",
            "https://nature.com/paper2",
            "https://arxiv.org/abs/2301.0001",
            "https://wiley.com/paper1",
        ]
        sample, skipped = sample_preflight_urls(urls, 3)
        assert len(sample) == 3
        domains = {u.split("/")[2] for u in sample}
        assert len(domains) == 3  # one from each publisher

    def test_small_list_returns_all(self):
        from zotpilot.tools.ingestion.connector import sample_preflight_urls

        urls = ["https://arxiv.org/abs/1", "https://arxiv.org/abs/2"]
        sample, skipped = sample_preflight_urls(urls, 5)
        assert sample == urls
        assert skipped == []


# ---------------------------------------------------------------------------
# run_preflight_check tests — blocking strategy
# ---------------------------------------------------------------------------


class TestRunPreflightCheck:
    """Test that run_preflight_check blocks on anti_bot, timeout, and subscription."""

    def _make_candidate(self, url: str) -> dict:
        return {"url": url, "title": "Paper", "doi": None}

    def _run_preflight(self, candidates, preflight_report):
        """Helper: mock preflight_urls and call run_preflight_check."""
        from zotpilot.tools.ingestion.connector import run_preflight_check
        mock_logger = MagicMock()
        with patch(
            "zotpilot.tools.ingestion.connector.preflight_urls",
            return_value=preflight_report,
        ):
            return run_preflight_check(
                candidates, default_port=2619,
                bridge_server_cls=MagicMock(),
                _logger=mock_logger,
            )

    def test_anti_bot_detected_blocks(self):
        """anti_bot_detected error should block the domain."""
        candidates = [
            self._make_candidate("https://science.org/paper1"),
            self._make_candidate("https://science.org/paper2"),
        ]
        report = {
            "all_clear": False,
            "blocked": [{
                "url": "https://science.org/paper1",
                "title": "Just a moment...",
                "error_code": "anti_bot_detected",
            }],
            "errors": [],
        }
        remaining, failures, blocking, publishers = self._run_preflight(candidates, report)
        assert blocking is not None
        assert len(remaining) == 0  # Both Science papers blocked
        assert len(failures) == 1

    def test_preflight_timeout_blocks(self):
        """preflight_timeout error should block the domain (NEW behavior)."""
        candidates = [
            self._make_candidate("https://science.org/paper1"),
            self._make_candidate("https://arxiv.org/abs/2301.0001"),
        ]
        report = {
            "all_clear": False,
            "blocked": [],
            "errors": [{
                "url": "https://science.org/paper1",
                "error": "Timeout (60s)",
                "error_code": "preflight_timeout",
            }],
        }
        remaining, failures, blocking, publishers = self._run_preflight(candidates, report)
        assert blocking is not None
        assert len(remaining) == 1  # arXiv passes
        assert remaining[0]["url"] == "https://arxiv.org/abs/2301.0001"
        assert any(f["error_code"] == "preflight_timeout" for f in failures)

    def test_subscription_required_blocks(self):
        """subscription_required error should block the domain (NEW behavior)."""
        candidates = [
            self._make_candidate("https://ieee.org/paper1"),
            self._make_candidate("https://arxiv.org/abs/2301.0001"),
        ]
        report = {
            "all_clear": False,
            "blocked": [],
            "errors": [{
                "url": "https://ieee.org/paper1",
                "error": "subscription required",
                "error_code": "subscription_required",
            }],
        }
        remaining, failures, blocking, publishers = self._run_preflight(candidates, report)
        assert blocking is not None
        assert len(remaining) == 1  # arXiv passes
        assert remaining[0]["url"] == "https://arxiv.org/abs/2301.0001"
        assert any(f["error_code"] == "subscription_required" for f in failures)

    def test_preflight_failed_does_not_block(self):
        """Generic preflight_failed should NOT block the domain."""
        candidates = [
            self._make_candidate("https://ieee.org/paper1"),
        ]
        report = {
            "all_clear": False,
            "blocked": [],
            "errors": [{
                "url": "https://ieee.org/paper1",
                "error": "some error",
                "error_code": "preflight_failed",
            }],
        }
        remaining, failures, blocking, publishers = self._run_preflight(candidates, report)
        assert blocking is None  # NOT blocking for generic errors
        assert len(remaining) == 1  # IEEE paper still allowed
        assert any(f["error_code"] == "preflight_failed" for f in failures)

