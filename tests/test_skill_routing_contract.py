from __future__ import annotations

from pathlib import Path


def test_ztp_research_skill_no_longer_mentions_legacy_research_session():
    skill = (Path(__file__).parent.parent / "src" / "zotpilot" / "skills" / "ztp-research.md").read_text()
    assert "research_session(" not in skill
    assert "confirm_candidates" in skill
    assert "approve_ingest" in skill
    assert "approve_post_ingest" in skill
    assert "approve_post_process" in skill
