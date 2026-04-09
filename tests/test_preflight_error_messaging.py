"""Tests for preflight error messaging (Fix 1.3 — 2026-04-08 remediation).

Verifies that _POST_INGEST_INSTRUCTION, the preflight_blocked BlockingDecision,
and the anti_bot_detected error string all carry the expanded deterrent messaging
introduced in the 2026-04-08 hardening plan.
"""

from __future__ import annotations

from zotpilot.tools.ingest_state import (
    BatchState,
    BlockingDecision,
    IngestItemState,
)

# ---------------------------------------------------------------------------
# Post-Fix2: _POST_INGEST_INSTRUCTION removed; _instruction field gone
# ---------------------------------------------------------------------------


class TestPostIngestInstruction:
    def test_instruction_constant_not_exported(self):
        """_POST_INGEST_INSTRUCTION must not exist after Fix 2."""
        import zotpilot.tools.ingest_state as _state_mod
        import zotpilot.tools.ingestion as _ing_mod
        assert not hasattr(_state_mod, "_POST_INGEST_INSTRUCTION")
        assert not hasattr(_ing_mod, "_POST_INGEST_INSTRUCTION")

    def test_no_instruction_key_when_final_with_saved(self):
        """_instruction must NOT appear in get_ingest_status after Fix 2."""
        from unittest.mock import patch

        from zotpilot.tools.ingestion import _batch_store, get_ingest_status
        items = [
            IngestItemState(
                index=0, url="https://ex.com/paper",
                status="saved", item_key="K1", has_pdf=True,
            )
        ]
        batch = BatchState(total=1, collection_used=None, pending_items=items)
        batch.finalize()
        with patch.object(_batch_store, "get", return_value=batch):
            status = get_ingest_status(batch_id=batch.batch_id)
        assert "_instruction" not in status
        # suggested_next_steps replaces _instruction
        assert "suggested_next_steps" in status
        assert len(status["suggested_next_steps"]) > 0

    def test_not_emitted_when_no_saved_items(self):
        """_instruction must NOT appear when the batch has no saved items."""
        items = [
            IngestItemState(
                index=0, url="https://ex.com/paper",
                status="failed", error="boom",
            )
        ]
        batch = BatchState(total=1, collection_used=None, pending_items=items)
        batch.finalize()
        status = batch.full_status()
        assert "_instruction" not in status


# ---------------------------------------------------------------------------
# BlockingDecision for preflight_blocked
# ---------------------------------------------------------------------------


class TestPreflightBlockedDecision:
    def _make_preflight_blocked_decision(self, batch_id: str = "ing_test") -> BlockingDecision:
        """Return a BlockingDecision matching what ingestion.py emits."""
        return BlockingDecision(
            decision_id="preflight_blocked",
            batch_id=batch_id,
            item_keys=tuple(),
            description=(
                "Preflight detected anti-bot protection (CAPTCHA / Cloudflare / login). "
                "User must complete browser verification in Chrome, then retry the SAME "
                "ingest_papers call with identical inputs. "
                "DO NOT retry with save_urls or DOI links — same wall, worse state."
            ),
        )

    def test_decision_id_is_preflight_blocked(self):
        d = self._make_preflight_blocked_decision()
        assert d.decision_id == "preflight_blocked"

    def test_description_instructs_browser_verification(self):
        d = self._make_preflight_blocked_decision()
        assert "browser verification" in d.description

    def test_description_warns_against_save_urls_fallback(self):
        d = self._make_preflight_blocked_decision()
        desc_lower = d.description.lower()
        assert "do not" in desc_lower
        assert "save_urls" in d.description

    def test_description_surfaces_in_full_status(self):
        """A failed batch with preflight_blocked decision must expose it in full_status."""
        items = [
            IngestItemState(
                index=0, url="https://blocked.example/paper",
                status="failed", error="anti_bot_detected",
            )
        ]
        batch = BatchState(total=1, collection_used=None, pending_items=items)
        batch.finalize()
        decision = self._make_preflight_blocked_decision(batch_id=batch.batch_id)
        batch.blocking_decisions.append(decision)

        status = batch.full_status()
        decisions = status["blocking_decisions"]
        preflight_decisions = [d for d in decisions if d["decision_id"] == "preflight_blocked"]
        assert len(preflight_decisions) == 1
        desc = preflight_decisions[0]["description"]
        assert "browser verification" in desc
        assert "save_urls" in desc


# ---------------------------------------------------------------------------
# anti_bot_detected error message in ingestion.py
# ---------------------------------------------------------------------------


class TestAntiBotDetectedErrorMessage:
    """Verify the per-item error string set when a URL is preflight-blocked.

    The error message is embedded in ingestion.py as a literal string. We
    construct an IngestItemState with that error text (mirroring what
    ingest_papers sets) and assert on its content directly, without running
    the full ingestion pipeline.
    """

    # Mirror of the string set in ingestion.py ~line 1035.
    _ANTI_BOT_ERROR = (
        "preflight blocked — publisher/CDN detected automated requests. "
        "User must complete browser verification (CAPTCHA / Cloudflare / login) "
        "in Chrome, then retry the SAME ingest_papers call with identical inputs. "
        "DO NOT retry with save_urls or DOI links — you'll hit the same wall and "
        "produce a partial-success batch that's hard to roll back."
    )

    def test_error_contains_do_not_retry_with_save_urls(self):
        assert "DO NOT retry with save_urls" in self._ANTI_BOT_ERROR

    def test_error_contains_complete_browser_verification(self):
        assert "complete browser verification" in self._ANTI_BOT_ERROR

    def test_error_string_matches_ingestion_source(self):
        """Cross-check that the ingestion package still contains the exact deterrent phrases.

        After Fix 4, the anti-bot error string lives in _ingest.py (moved from __init__.py).
        We search all .py files in the ingestion package directory.
        """
        import pathlib

        pkg_dir = (
            pathlib.Path(__file__).resolve().parent.parent
            / "src" / "zotpilot" / "tools" / "ingestion"
        )
        combined = "\n".join(
            p.read_text(encoding="utf-8") for p in pkg_dir.glob("*.py")
        )
        assert "DO NOT retry with save_urls" in combined, (
            "ingestion package must contain 'DO NOT retry with save_urls' in its "
            "anti_bot_detected error message"
        )
        assert "complete browser verification" in combined, (
            "ingestion package must contain 'complete browser verification' in its "
            "anti_bot_detected error message"
        )

    def test_item_state_preserves_error_text(self):
        """IngestItemState.to_dict() round-trips the error string unchanged."""
        item = IngestItemState(
            index=0,
            url="https://blocked.example/paper",
            status="failed",
            error=self._ANTI_BOT_ERROR,
        )
        d = item.to_dict()
        assert d["error"] == self._ANTI_BOT_ERROR
        assert "DO NOT retry with save_urls" in d["error"]
        assert "complete browser verification" in d["error"]
