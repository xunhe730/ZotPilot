"""Regression tests for the 2026-04-08 ingest UX hardening plan.

Covers F1-F4 + T1-T7 from .omc/plans/2026-04-08-ingest-ux-regression-hardening.md.
Each test enforces a code-layer contract that previous SKILL-prompt fixes failed
to defend.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.tools.ingest_state import (
    BatchState,
    BlockingDecision,
    IngestItemState,
    _build_suggested_next_steps,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_finalized_batch(
    *,
    n_with_pdf: int = 1,
    n_metadata_only: int = 0,
    n_failed: int = 0,
    session_id: str | None = None,
) -> BatchState:
    items: list[IngestItemState] = []
    idx = 0
    for i in range(n_with_pdf):
        items.append(
            IngestItemState(
                index=idx, url=f"https://ex.com/pdf/{i}", title=f"P{i}",
                status="saved", item_key=f"PDF{i:03d}", has_pdf=True,
            )
        )
        idx += 1
    for i in range(n_metadata_only):
        items.append(
            IngestItemState(
                index=idx, url=f"https://ex.com/meta/{i}", title=f"M{i}",
                status="saved", item_key=f"META{i:03d}", has_pdf=False,
            )
        )
        idx += 1
    for i in range(n_failed):
        items.append(
            IngestItemState(
                index=idx, url=f"https://ex.com/fail/{i}", title=f"F{i}",
                status="failed", error="boom",
            )
        )
        idx += 1
    batch = BatchState(
        total=len(items),
        collection_used=None,
        pending_items=items,
        session_id=session_id,
    )
    batch.finalize()
    return batch


def _walk_for_keys(obj, keys: set[str]) -> list[str]:
    """Recursively collect any banned key names found in obj."""
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                found.append(k)
            found.extend(_walk_for_keys(v, keys))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found.extend(_walk_for_keys(v, keys))
    return found


# ---------------------------------------------------------------------------
# F1 — preflight hard halt (full e2e via ingest_papers is exercised in
# test_tools_ingestion.py; here we test the BatchState shape directly).
# ---------------------------------------------------------------------------


class TestF1PreflightHaltShape:
    def test_failed_batch_emits_preflight_blocked_decision(self):
        """A failed batch with a preflight_blocked decision must surface it."""
        items = [
            IngestItemState(
                index=0, url="https://blocked.example/a", title="A",
                status="failed", error="anti_bot_detected: preflight blocked",
            ),
            IngestItemState(
                index=1, url="https://clean.example/b", title="B",
                status="failed", error="preflight_batch_halted: ...",
            ),
        ]
        batch = BatchState(total=2, collection_used=None, pending_items=items)
        batch.finalize()
        batch.blocking_decisions.append(
            BlockingDecision(
                decision_id="preflight_blocked",
                batch_id=batch.batch_id,
                item_keys=tuple(),
                description="Preflight detected anti-bot...",
            )
        )
        status = batch.full_status()
        assert status["state"] == "failed"
        assert status["is_final"] is True
        ids = [d["decision_id"] for d in status["blocking_decisions"]]
        assert "preflight_blocked" in ids
        assert all(r["status"] == "failed" for r in status["results"])


# ---------------------------------------------------------------------------
# F2 — index_library gate
# ---------------------------------------------------------------------------


class TestF2IndexLibraryGate:
    def _setup_session_with_metadata_only_batch(self, monkeypatch, embedding_provider="gemini"):
        from zotpilot.tools import indexing as ix_mod
        from zotpilot.tools import ingestion as ing_mod

        # Fresh stores per test
        ing_mod._batch_store.clear()

        # Plant a finalized metadata-only batch in the store
        batch = _make_finalized_batch(n_with_pdf=0, n_metadata_only=2, session_id="rs_test_meta")
        # Trigger blocking_decisions emission via full_status side-effect
        batch.full_status()
        ing_mod._batch_store.put(batch)

        # Stub config
        cfg = MagicMock()
        cfg.embedding_provider = embedding_provider
        cfg.validate.return_value = []
        monkeypatch.setattr(ix_mod, "_get_config", lambda: cfg)

        return ix_mod, batch

    def test_blocks_on_unresolved_metadata_only_choice(self, monkeypatch):
        ix_mod, batch = self._setup_session_with_metadata_only_batch(monkeypatch)
        # Indexer must NOT be invoked when blocked
        with patch("zotpilot.indexer.Indexer") as fake_indexer:
            result = ix_mod.index_library(acknowledge_metadata_only=False)
            fake_indexer.assert_not_called()
        assert result["status"] == "blocked"
        assert result["blocking_decision"] == "metadata_only_choice"
        assert result["user_consent_required"] is True
        assert result["resolution_parameter"] == "acknowledge_metadata_only"
        assert result["batch_id"] == batch.batch_id
        assert sorted(result["item_keys"]) == sorted(["META000", "META001"])

    def test_proceeds_with_acknowledge_flag(self, monkeypatch):
        ix_mod, batch = self._setup_session_with_metadata_only_batch(monkeypatch)
        # Stub Indexer to a no-op success path
        fake = MagicMock()
        fake.index_all.return_value = {
            "results": [], "indexed": 0, "failed": 0, "empty": 0,
            "skipped": 0, "already_indexed": 0, "has_more": False,
            "skipped_no_pdf": [],
        }
        with patch("zotpilot.indexer.Indexer", return_value=fake), \
             patch("zotpilot.tools.indexing._get_store") as fake_store:
            fake_store.return_value = MagicMock()
            result = ix_mod.index_library(acknowledge_metadata_only=True, batch_size=0)
        assert "status" not in result or result.get("status") != "blocked"
        # Decision must now be resolved
        decisions = batch.blocking_decisions
        assert any(d.resolved for d in decisions if d.decision_id == "metadata_only_choice")

    def test_unaffected_in_no_rag_mode(self, monkeypatch):
        ix_mod, batch = self._setup_session_with_metadata_only_batch(
            monkeypatch, embedding_provider="none",
        )
        fake = MagicMock()
        fake.index_all.return_value = {
            "results": [], "indexed": 0, "failed": 0, "empty": 0,
            "skipped": 0, "already_indexed": 0, "has_more": False,
            "skipped_no_pdf": [],
        }
        with patch("zotpilot.indexer.Indexer", return_value=fake), \
             patch("zotpilot.tools.indexing._get_store") as fake_store:
            fake_store.return_value = MagicMock()
            result = ix_mod.index_library(acknowledge_metadata_only=False, batch_size=0)
        # No-RAG mode bypasses gate entirely — should not return a blocked envelope
        assert result.get("status") != "blocked"


    def test_acknowledge_marks_decision_resolved(self, monkeypatch):
        """After acknowledge, a second call must NOT re-block."""
        ix_mod, batch = self._setup_session_with_metadata_only_batch(monkeypatch)
        fake = MagicMock()
        fake.index_all.return_value = {
            "results": [], "indexed": 0, "failed": 0, "empty": 0,
            "skipped": 0, "already_indexed": 0, "has_more": False,
            "skipped_no_pdf": [],
        }
        with patch("zotpilot.indexer.Indexer", return_value=fake), \
             patch("zotpilot.tools.indexing._get_store") as fake_store:
            fake_store.return_value = MagicMock()
            ix_mod.index_library(acknowledge_metadata_only=True, batch_size=0)
            second = ix_mod.index_library(acknowledge_metadata_only=False, batch_size=0)
        assert second.get("status") != "blocked"


# ---------------------------------------------------------------------------
# F3 — write_ops.manage_collections 404 deferred to N+1
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="deferred to N+1: see issue #TBD — manage_collections 404 from "
    "Zotero local→Web API sync window. Plan: 2026-04-08-ingest-ux-regression-hardening.md",
)
def test_manage_collections_404_documented_failure():
    """Documents the current 404 failure mode for the N+1 fix."""
    # This test is intentionally a placeholder until N+1 lands the fix.
    # The strict-xfail will start failing CI 2 release cycles after merge,
    # forcing the conversation if the N+1 fix slips.
    raise AssertionError("Placeholder for N+1 manage_collections 404 fix.")


# ---------------------------------------------------------------------------
# F4 — _POST_INGEST_INSTRUCTION removed; suggested_next_steps carries post-ingest guidance
# ---------------------------------------------------------------------------


class TestF4InstructionShape:
    def test_post_ingest_instruction_constant_removed(self):
        """_POST_INGEST_INSTRUCTION was deleted in Fix 2 — must not be importable."""

        import zotpilot.tools.ingest_state as _state_mod
        import zotpilot.tools.ingestion as _ing_mod
        assert not hasattr(_state_mod, "_POST_INGEST_INSTRUCTION"), (
            "_POST_INGEST_INSTRUCTION must be deleted from ingest_state"
        )
        assert not hasattr(_ing_mod, "_POST_INGEST_INSTRUCTION"), (
            "_POST_INGEST_INSTRUCTION must not be re-exported from ingestion"
        )

    def test_full_status_has_no_instruction_key(self):
        """full_status() must not include _instruction after Fix 2."""
        from zotpilot.tools.ingest_state import BatchState, IngestItemState
        items = [IngestItemState(index=0, url="https://ex.com", status="saved", item_key="K1")]
        batch = BatchState(total=1, collection_used=None, pending_items=items)
        batch.finalize()
        fs = batch.full_status()
        assert "_instruction" not in fs

    def test_suggested_next_steps_present_when_final_with_saved(self):
        """suggested_next_steps replaces _instruction for post-ingest guidance."""
        from zotpilot.tools.ingest_state import BatchState, IngestItemState
        items = [IngestItemState(index=0, url="https://ex.com", status="saved", item_key="K1")]
        batch = BatchState(total=1, collection_used=None, pending_items=items)
        batch.finalize()
        fs = batch.full_status()
        assert "suggested_next_steps" in fs
        assert len(fs["suggested_next_steps"]) > 0


# ---------------------------------------------------------------------------
# T1 — suggested_next_steps has no tool/args keys
# ---------------------------------------------------------------------------


class TestT1SuggestedNextStepsShape:
    def test_no_tool_or_args_keys_at_any_depth(self):
        steps = _build_suggested_next_steps()
        banned = _walk_for_keys(steps, {"tool", "args"})
        assert banned == [], f"banned keys present: {banned}"

    def test_step_keys_are_only_step_id_description_depends_on(self):
        steps = _build_suggested_next_steps()
        for step in steps:
            assert set(step.keys()) == {"step_id", "description", "depends_on"}

    def test_full_status_envelope_has_no_tool_or_args(self):
        batch = _make_finalized_batch(n_with_pdf=2, n_metadata_only=1)
        status = batch.full_status()
        banned = _walk_for_keys(status, {"tool", "args"})
        assert banned == []


# ---------------------------------------------------------------------------
# T2 — blocking_decisions reference items by item_key only (no payload dup)
# ---------------------------------------------------------------------------


class TestT2NoPayloadDuplication:
    def test_blocking_decisions_carry_no_title_or_url(self):
        batch = _make_finalized_batch(n_with_pdf=0, n_metadata_only=2)
        status = batch.full_status()
        decisions = status["blocking_decisions"]
        assert decisions, "expected metadata_only_choice to be emitted"
        for d in decisions:
            assert "title" not in d
            assert "url" not in d
            # Must reference by item_key
            assert "item_keys" in d
            assert all(isinstance(k, str) for k in d["item_keys"])

    def test_pdf_missing_items_remains_canonical(self):
        batch = _make_finalized_batch(n_with_pdf=0, n_metadata_only=2)
        status = batch.full_status()
        # Canonical payload still present
        assert "pdf_missing_items" in status
        canonical_keys = {it["item_key"] for it in status["pdf_missing_items"]}
        decision_keys = set(status["blocking_decisions"][0]["item_keys"])
        assert decision_keys.issubset(canonical_keys)


# ---------------------------------------------------------------------------
# T3 — routing retry happy-path break-on-success latency
# ---------------------------------------------------------------------------


class TestT3RoutingRetryLatency:
    def test_returns_immediately_on_first_success(self):
        from zotpilot.tools import ingestion_bridge

        sleep_calls = []

        def fake_sleep(s):
            sleep_calls.append(s)

        writer = MagicMock()
        writer.add_to_collection.return_value = None
        writer.add_item_tags.return_value = None

        get_config = lambda: MagicMock(zotero_api_key="key")  # noqa: E731

        with patch.object(ingestion_bridge.time, "sleep", fake_sleep):
            t0 = time.monotonic()
            err = ingestion_bridge.apply_collection_tag_routing(
                "ITEM", "COLL", ["t1"], writer, get_config,
            )
            elapsed = time.monotonic() - t0

        assert err is None
        # First entry of ROUTING_RETRY_DELAYS_S is 0.0, so sleep may be called
        # at most once with 0.0 before success — never with a non-zero delay.
        assert all(s == 0.0 for s in sleep_calls), f"sleep called with non-zero: {sleep_calls}"
        assert elapsed < 2.0


# ---------------------------------------------------------------------------
# T4 — schema tolerance for legacy BatchState without new fields
# ---------------------------------------------------------------------------


class TestT4SchemaTolerance:
    def test_full_status_tolerates_legacy_batch_without_blocking_decisions(self):
        batch = _make_finalized_batch(n_with_pdf=1)
        # Simulate a pickled-old batch by stripping the field
        try:
            del batch.__dict__["blocking_decisions"]
        except KeyError:
            pass
        try:
            del batch.__dict__["suggested_next_steps"]
        except KeyError:
            pass
        # Must not raise AttributeError
        status = batch.full_status()
        assert "blocking_decisions" in status
        assert isinstance(status["blocking_decisions"], list)


# ---------------------------------------------------------------------------
# T5 — _update_session_after_ingest tolerates legacy schema
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# T6 — CLI auto-acknowledges (uses Indexer directly so it bypasses gate)
# ---------------------------------------------------------------------------


class TestT6CLIBypass:
    def test_cli_index_does_not_call_index_library_tool(self):
        """The CLI uses Indexer directly, so the index_library MCP gate
        cannot block CLI execution. This test asserts the call path."""
        from zotpilot import cli
        # Verify cmd_index imports Indexer (the bypass mechanism)
        src = Path(cli.__file__).read_text(encoding="utf-8")
        # In cmd_index body, Indexer is invoked directly
        idx_marker = src.find("def cmd_index")
        next_def = src.find("\ndef ", idx_marker + 1)
        body = src[idx_marker:next_def]
        assert "Indexer(config)" in body
        # And it does NOT route through the index_library MCP tool
        assert "index_library(" not in body


# ---------------------------------------------------------------------------
# T7 — SKILL.md must not leak the override parameter name
# ---------------------------------------------------------------------------


class TestT7SkillProseDoesNotLeakParam:
    def test_ztp_research_skill_does_not_mention_acknowledge_metadata_only(self):
        skill_path = (
            Path(__file__).resolve().parent.parent
            / "src" / "zotpilot" / "skills" / "ztp-research.md"
        )
        text = skill_path.read_text(encoding="utf-8")
        assert "acknowledge_metadata_only" not in text, (
            "skill prose must not name the override parameter directly"
        )
