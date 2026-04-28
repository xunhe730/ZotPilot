"""Tests for OpenAlex API client rate limiting and retry logic."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.openalex_client import OpenAlexClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int = 200, body: dict | None = None) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = body or {"results": [], "meta": {}}
    mock_resp.raise_for_status = MagicMock()
    if status_code >= 400:

        def raise_for_status():
            import httpx

            raise httpx.HTTPStatusError(f"HTTP {status_code}", request=MagicMock(), response=mock_resp)

        mock_resp.raise_for_status.side_effect = raise_for_status
    return mock_resp


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_anonymous_delay_is_1s(self):
        client = OpenAlexClient()
        assert client._rate_limit_delay == 1.0

    def test_polite_email_delay_is_0_1s(self):
        client = OpenAlexClient(email="test@example.com")
        assert client._rate_limit_delay == 0.1

    def test_rate_limit_sleeps_when_called_quickly(self):
        with patch("zotpilot.openalex_client.time.sleep") as mock_sleep:
            client = OpenAlexClient()
            client._last_request = 0.0  # pretend a long time ago
            client._rate_limit()
            # No sleep needed since _last_request was long ago
            mock_sleep.assert_not_called()

            # Now call again immediately
            client._rate_limit()
            # Should sleep approximately 1.0s
            mock_sleep.assert_called_once()
            assert mock_sleep.call_args[0][0] == pytest.approx(1.0, abs=0.01)

    def test_rate_limit_no_sleep_after_delay(self):
        with patch("zotpilot.openalex_client.time.sleep") as mock_sleep:
            client = OpenAlexClient()
            # Set last request to 2 seconds ago (beyond 1s delay)
            import time

            client._last_request = time.time() - 2.0
            client._rate_limit()
            mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# 429 Retry logic
# ---------------------------------------------------------------------------


class Test429Retry:
    def test_no_retry_on_200(self):
        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            mock_get.return_value = _make_response(200)
            client = OpenAlexClient()
            resp = client._request("/works")
            assert mock_get.call_count == 1
            assert resp.status_code == 200

    def test_retries_on_429_then_succeeds(self):
        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            mock_get.side_effect = [
                _make_response(429),
                _make_response(429),
                _make_response(200),
            ]
            client = OpenAlexClient()
            resp = client._request("/works")
            assert mock_get.call_count == 3
            assert resp.status_code == 200

    def test_exhausts_retries_on_429(self):
        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            mock_get.return_value = _make_response(429)
            client = OpenAlexClient()
            resp = client._request("/works")
            # Initial + 3 retries = 4 total calls
            assert mock_get.call_count == 4
            assert resp.status_code == 429

    def test_exponential_backoff_timing(self):
        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            with patch("zotpilot.openalex_client.time.sleep") as mock_sleep:
                mock_get.return_value = _make_response(429)
                client = OpenAlexClient()
                client._request("/works")

                # _last_request=0 is long ago, so first _rate_limit doesn't sleep.
                # Pattern: [backoff_1s, rate_limit, backoff_2s, rate_limit, backoff_4s]
                sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
                backoff_sleeps = [sleep_calls[i] for i in [0, 2, 4]]
                assert backoff_sleeps[0] == pytest.approx(1.0, abs=0.05)
                assert backoff_sleeps[1] == pytest.approx(2.0, abs=0.05)
                assert backoff_sleeps[2] == pytest.approx(4.0, abs=0.05)

    def test_429_retry_logs_warning(self, caplog):
        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            mock_get.side_effect = [
                _make_response(429),
                _make_response(200),
            ]
            client = OpenAlexClient()
            with caplog.at_level(logging.WARNING):
                client._request("/works")
            assert "Rate limited (429)" in caplog.text
            assert "retry 1/3" in caplog.text

    def test_exhausted_retries_logs_error(self, caplog):
        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            mock_get.return_value = _make_response(429)
            client = OpenAlexClient()
            with caplog.at_level(logging.ERROR):
                client._request("/works")
            assert "all 3 retries exhausted" in caplog.text

    def test_rate_limit_enforced_between_retries(self):
        """Verify _rate_limit is called on each retry attempt."""
        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            with patch.object(OpenAlexClient, "_rate_limit") as mock_rl:
                mock_get.return_value = _make_response(429)
                client = OpenAlexClient()
                client._request("/works")
                # Called for initial + 3 retries = 4 times
                assert mock_rl.call_count == 4

    def test_mailto_param_included_on_retries(self):
        """Verify mailto param is preserved across retries."""
        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            mock_get.side_effect = [
                _make_response(429),
                _make_response(200),
            ]
            client = OpenAlexClient(email="polite@example.com")
            client._request("/works", params={"search": "test"})

            for call in mock_get.call_args_list:
                params = call.kwargs.get("params") or call[1].get("params")
                assert params is not None
                assert params.get("mailto") == "polite@example.com"


# ---------------------------------------------------------------------------
# Network-error retry (SSL EOF, ConnectError, TimeoutException)
# ---------------------------------------------------------------------------


class TestNetworkErrorRetry:
    def test_connect_error_retries_then_succeeds(self):
        import httpx

        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            with patch("zotpilot.openalex_client.time.sleep"):
                mock_get.side_effect = [
                    httpx.ConnectError("SSL: UNEXPECTED_EOF_WHILE_READING"),
                    _make_response(200),
                ]
                client = OpenAlexClient()
                resp = client._request("/works")
                assert resp.status_code == 200
                assert mock_get.call_count == 2

    def test_connect_error_exhausts_retries_and_raises(self):
        import httpx

        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            with patch("zotpilot.openalex_client.time.sleep"):
                mock_get.side_effect = httpx.ConnectError("SSL EOF")
                client = OpenAlexClient()
                with pytest.raises(httpx.ConnectError):
                    client._request("/works")
                assert mock_get.call_count == 4  # initial + 3 retries

    def test_connect_error_backoff_grows(self):
        import httpx

        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            with patch("zotpilot.openalex_client.time.sleep") as mock_sleep:
                mock_get.side_effect = [
                    httpx.ConnectError("x"),
                    httpx.ConnectError("x"),
                    _make_response(200),
                ]
                client = OpenAlexClient()
                client._last_request = 0.0  # skip first rate-limit sleep
                client._request("/works")
                # sleep pattern: [backoff_0, rate_limit_1, backoff_1, rate_limit_2]
                sleeps = [c[0][0] for c in mock_sleep.call_args_list]
                assert sleeps[0] == pytest.approx(1.0, abs=0.05)  # backoff after attempt 0
                assert sleeps[2] == pytest.approx(2.0, abs=0.05)  # backoff after attempt 1

    def test_timeout_exception_also_retries(self):
        """TimeoutException is a RequestError subclass — should retry."""
        import httpx

        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            with patch("zotpilot.openalex_client.time.sleep"):
                mock_get.side_effect = [
                    httpx.ReadTimeout("read timed out"),
                    _make_response(200),
                ]
                client = OpenAlexClient()
                resp = client._request("/works")
                assert resp.status_code == 200

    def test_network_retry_logs_warning(self, caplog):
        import httpx

        with patch("zotpilot.openalex_client.httpx.get") as mock_get:
            with patch("zotpilot.openalex_client.time.sleep"):
                mock_get.side_effect = [
                    httpx.ConnectError("SSL EOF"),
                    _make_response(200),
                ]
                client = OpenAlexClient()
                with caplog.at_level(logging.WARNING):
                    client._request("/works")
                assert "OpenAlex network error (ConnectError)" in caplog.text
                assert "retry 1/3" in caplog.text
