"""Tests for B② same-publisher canary gating helpers."""

from __future__ import annotations

from zotpilot.tools.ingestion import (
    _canary_publisher_key,
    _canary_record,
    _canary_should_skip,
)


class TestCanaryPublisherKey:
    def test_uses_host(self):
        key = _canary_publisher_key({"url": "https://www.sciencedirect.com/science/article/pii/X"})
        assert key == "sciencedirect.com"

    def test_falls_back_to_landing(self):
        key = _canary_publisher_key({"landing_page_url": "https://onlinelibrary.wiley.com/doi/1"})
        assert "wiley.com" in key


class TestCanaryRecordAndSkip:
    def test_first_success_then_not_skipped(self):
        state: dict[str, bool] = {}
        _canary_record("elsevier.com", state, "saved_with_pdf")
        assert state["elsevier.com"] is True
        assert _canary_should_skip("elsevier.com", state) is False

    def test_first_failure_then_skipped(self):
        state: dict[str, bool] = {}
        _canary_record("elsevier.com", state, "blocked")
        assert state["elsevier.com"] is False
        assert _canary_should_skip("elsevier.com", state) is True

    def test_metadata_only_counts_as_success(self):
        state: dict[str, bool] = {}
        _canary_record("x.com", state, "saved_metadata_only")
        assert state["x.com"] is True

    def test_failed_status_is_a_canary_failure(self):
        state: dict[str, bool] = {}
        _canary_record("x.com", state, "failed")
        assert state["x.com"] is False

    def test_record_does_not_overwrite_first_outcome(self):
        # A later item must not flip the publisher's recorded canary result.
        state = {"x.com": False}
        _canary_record("x.com", state, "saved_with_pdf")
        assert state["x.com"] is False

    def test_none_publisher_never_records_or_skips(self):
        # manual-group items pass publisher_key=None → never gated by canary.
        state: dict[str, bool] = {}
        _canary_record(None, state, "blocked")
        assert state == {}
        assert _canary_should_skip(None, {"x": False}) is False

    def test_unseen_publisher_not_skipped(self):
        # The canary (first) item itself is never skipped.
        assert _canary_should_skip("new.com", {}) is False
