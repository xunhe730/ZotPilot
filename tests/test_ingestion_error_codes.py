"""Tests for the centralized ingest error/warning code dictionary (P1-D)."""

from __future__ import annotations

from zotpilot.tools.ingestion import error_codes


class TestNextStepsZh:
    def test_known_code_returns_chinese(self):
        msg = error_codes.next_steps_zh("pdf_antibot_blocked")
        assert "养热会话" in msg
        assert "单篇重试" in msg

    def test_unknown_code_returns_default(self):
        assert error_codes.next_steps_zh("does_not_exist") == ""
        assert error_codes.next_steps_zh("does_not_exist", "fallback") == "fallback"

    def test_every_code_has_required_fields(self):
        for code, entry in error_codes.ERROR_CODE_DICT.items():
            assert entry["zh"], code
            assert entry["next_steps_zh"], code
            assert isinstance(entry["rescuable"], bool), code


class TestPdfAttentionEntry:
    def test_pdf_not_attached_builds_notice(self):
        row = {
            "status": "saved_metadata_only",
            "warning_code": "pdf_not_attached",
            "warning": "raw",
            "identifier": "10.1016/j.x",
            "item_key": "ABC123",
            "title": "Some Paper",
        }
        entry = error_codes.pdf_attention_entry(row)
        assert entry is not None
        assert entry["type"] == "pdf_attention"
        assert entry["code"] == "pdf_not_attached"
        assert entry["identifier"] == "10.1016/j.x"
        assert entry["item_key"] == "ABC123"
        assert "Continue" in entry["message"]  # from the dict, not the raw warning

    def test_second_antibot_code_builds_notice(self):
        row = {"warning_code": "pdf_antibot_blocked", "identifier": "x"}
        entry = error_codes.pdf_attention_entry(row)
        assert entry is not None
        assert entry["code"] == "pdf_antibot_blocked"
        assert "二次反爬" in entry["message"]

    def test_no_warning_code_returns_none(self):
        assert error_codes.pdf_attention_entry({"status": "saved_metadata_only"}) is None

    def test_blocking_code_is_not_a_notice(self):
        # anti_bot_detected is a blocking (action_required) code, not a notice
        assert error_codes.pdf_attention_entry({"warning_code": "anti_bot_detected"}) is None

    def test_unknown_warning_code_returns_none(self):
        assert error_codes.pdf_attention_entry({"warning_code": "weird"}) is None
