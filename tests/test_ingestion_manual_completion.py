"""Tests for D④ manual-completion rescue smoothing (links + tailored hint)."""

from __future__ import annotations

from zotpilot.tools.ingestion import _build_lookup_links, _build_manual_completion_action


class TestBuildLookupLinks:
    def test_item_key_yields_zotero_select(self):
        links = _build_lookup_links("ABC123", {})
        assert any(link["url"] == "zotero://select/library/items/ABC123" for link in links)

    def test_doi_and_arxiv(self):
        links = _build_lookup_links(None, {"doi": "10.1016/j.x", "arxiv_id": "2401.00001"})
        urls = {link["url"] for link in links}
        assert "https://doi.org/10.1016/j.x" in urls
        assert "https://arxiv.org/abs/2401.00001" in urls

    def test_source_doi_preferred(self):
        links = _build_lookup_links(None, {"source_doi": "10.1/src", "doi": "10.1/other"})
        assert any(link["url"] == "https://doi.org/10.1/src" for link in links)

    def test_empty_candidate_no_links(self):
        assert _build_lookup_links(None, {}) == []


class TestManualCompletionAction:
    def _build(self, resume_action):
        return _build_manual_completion_action(
            pending_candidate={"doi": "10.1/x", "existing_item_key": "K1"},
            current_result={
                "item_key": "K1",
                "resume_action": resume_action,
                "timeout_stage": "save_confirmation",
            },
            retry_payload=[],
            completed_count=0,
            completed_indexes=[],
            message="base message",
        )

    def test_reconcile_hint(self):
        action = self._build("reconcile_existing")
        assert action["resume_action"] == "reconcile_existing"
        assert "已存入 Zotero" in action["specific_hint"]
        assert action["message"] == "base message"

    def test_retry_save_hint(self):
        action = self._build("retry_save")
        assert "重试该篇" in action["specific_hint"]

    def test_lookup_links_present(self):
        action = self._build("reconcile_existing")
        urls = {link["url"] for link in action["lookup_links"]}
        assert "zotero://select/library/items/K1" in urls
        assert "https://doi.org/10.1/x" in urls

    def test_unknown_resume_action_has_empty_hint(self):
        action = self._build(None)
        assert action["specific_hint"] == ""
