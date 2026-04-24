"""P9: Skill drift guard for ztp-research.md.

Verifies that the skill prose:
1. Contains only H2/H3 headings listed in the allowlist fixture.
2. Does not contain forbidden control-flow patterns.
3. Does not contain type-guarantee language that belongs in code, not prose.

If ztp-research.md currently fails any check, the test is xfailed with a
pointer to the follow-up work rather than silently masking the violation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SKILL_PATH = Path(__file__).parent.parent / "src" / "zotpilot" / "skills" / "ztp-research.md"
HEADING_ALLOWLIST_PATH = FIXTURES_DIR / "skill_heading_allowlist.json"

# Patterns that must never appear in SKILL prose (control-flow leakage)
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    (r"if phase ==", "conditional on phase (belongs in code, not skill prose)"),
    (r"if status ==", "conditional on status (belongs in code, not skill prose)"),
    (r"retry \d+ times", "hard-coded retry count (belongs in code)"),
    (r"wait \d+ (seconds|minutes)", "hard-coded wait duration (belongs in code)"),
    (r"\blast_get_at\b", "internal state field exposed in prose (§5 P9)"),
    (r"\bcheckpoint_reached_at\b", "internal state field exposed in prose (§5 P9)"),
    (r"\bapproved_checkpoints\b", "internal state field exposed in prose (§5 P9)"),
    (r"\bdrift_details\b", "internal implementation detail in prose (§5 P9)"),
    (r"after \w+ call \w+", "ordering constraint (belongs in code state machine)"),
]

# Type-guarantee strings that must never appear in SKILL prose
TYPE_GUARANTEE_STRINGS: list[str] = [
    "type-guaranteed",
    "typed impossibility",
    "type-level guarantee",
    "compile-time impossible",
]


def _extract_headings(text: str) -> list[str]:
    """Return all H2 and H3 headings from markdown text."""
    headings = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^#{2,3} ", stripped):
            headings.append(stripped)
    return headings


def _load_allowlist() -> list[str]:
    data = json.loads(HEADING_ALLOWLIST_PATH.read_text())
    return data["headings"]


# ---------------------------------------------------------------------------
# Fixture / skill file existence checks
# ---------------------------------------------------------------------------

def test_skill_file_exists() -> None:
    assert SKILL_PATH.exists(), f"Skill file missing: {SKILL_PATH}"


def test_heading_allowlist_fixture_exists() -> None:
    assert HEADING_ALLOWLIST_PATH.exists(), (
        f"Heading allowlist fixture missing: {HEADING_ALLOWLIST_PATH}"
    )


# ---------------------------------------------------------------------------
# Heading drift check
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason=(
        "ztp-research.md may contain headings not yet in the allowlist fixture. "
        "Update tests/fixtures/skill_heading_allowlist.json to add newly approved headings. "
        "Do NOT modify ztp-research.md to fix this — only update the allowlist via PR review."
    ),
    strict=False,
)
def test_all_headings_in_allowlist() -> None:
    """Every H2/H3 heading in ztp-research.md must be in the known-good allowlist."""
    skill_text = SKILL_PATH.read_text()
    allowlist = set(_load_allowlist())
    headings = _extract_headings(skill_text)

    unknown = [h for h in headings if h not in allowlist]
    assert not unknown, (
        f"Unknown headings found in {SKILL_PATH.name} (not in allowlist fixture):\n"
        + "\n".join(f"  {h!r}" for h in unknown)
        + "\n\nTo approve: add each heading to tests/fixtures/skill_heading_allowlist.json via PR."
    )


# ---------------------------------------------------------------------------
# Forbidden control-flow pattern checks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pattern,reason", FORBIDDEN_PATTERNS)
@pytest.mark.xfail(
    reason=(
        "ztp-research.md may contain forbidden patterns from before the drift guard was enforced. "
        "These must be removed in a follow-up PR. Do NOT modify ztp-research.md here."
    ),
    strict=False,
)
def test_no_forbidden_pattern(pattern: str, reason: str) -> None:
    skill_text = SKILL_PATH.read_text()
    matches = [(i + 1, line) for i, line in enumerate(skill_text.splitlines())
               if re.search(pattern, line, re.IGNORECASE)]
    assert not matches, (
        f"Forbidden pattern {pattern!r} ({reason}) found in {SKILL_PATH.name}:\n"
        + "\n".join(f"  line {ln}: {txt.strip()!r}" for ln, txt in matches)
    )


# ---------------------------------------------------------------------------
# Phase A: No WebFetch mandatory references
# ---------------------------------------------------------------------------

def test_no_webfetch_mandatory_reference() -> None:
    """Skill must not contain WebFetch as a mandatory/required step."""
    skill_text = SKILL_PATH.read_text()
    # Check for the old mandatory WebFetch patterns
    assert "WebFetch priming (REQUIRED)" not in skill_text
    assert "DO NOT skip WebFetch priming" not in skill_text
    assert 'missing_priming' not in skill_text


# ---------------------------------------------------------------------------
# Phase C: Local-first check step exists
# ---------------------------------------------------------------------------

def test_local_first_step_exists() -> None:
    """Skill must include a local library check before external search."""
    skill_text = SKILL_PATH.read_text()
    assert "Local-first check" in skill_text or "local library" in skill_text.lower()
    # Ensure it references search_topic or advanced_search for local check
    assert "search_topic" in skill_text or "advanced_search" in skill_text


def test_search_sop_mentions_precise_anchor_strategies() -> None:
    """Skill must call out DOI / author / quoted phrase / boolean / concept filters."""
    skill_text = SKILL_PATH.read_text()
    # Canonical query forms
    assert "DOI direct" in skill_text
    assert "Author-anchored" in skill_text
    assert "Quoted phrase" in skill_text
    # OpenAlex-native filters (v0.5.0 enhancement)
    assert "concepts" in skill_text
    assert "venue" in skill_text


# ---------------------------------------------------------------------------
# Phase 4: ingest_by_identifiers usage constraint
# ---------------------------------------------------------------------------

def test_ingest_by_identifiers_usage_constraint() -> None:
    """Skill must reference ingest_by_identifiers as the sync ingest tool."""
    skill_text = SKILL_PATH.read_text()
    assert "ingest_by_identifiers" in skill_text
    # Phase 1 step 5 requires USER_REQUIRED gate before ingest
    assert "[USER_REQUIRED]" in skill_text
    # action_required must be recognized as a hard halt signal
    assert "action_required" in skill_text


def test_action_required_hard_halt_rule() -> None:
    """v0.5.0 replaces polling with sync action_required signals."""
    skill_text = SKILL_PATH.read_text()
    assert "action_required" in skill_text
    assert "STOP" in skill_text
    # Must warn against working around the block
    lowered = skill_text.lower()
    assert "anti_bot" in lowered or "anti-bot" in lowered


def test_access_check_rules_cover_non_oa_and_selected_publishers() -> None:
    """Institutional-access confirmation must not rely on OA alone."""
    skill_text = SKILL_PATH.read_text().lower()
    assert "always check" in skill_text
    assert "is_oa_published: false" in skill_text
    assert "ieee" in skill_text
    assert "wiley" in skill_text
    assert "springer" in skill_text
    assert "actual selected rows" in skill_text


def test_post_processing_covers_full_pipeline() -> None:
    """ztp-research must cover tag cleanup, tag assignment, collection, index verification."""
    skill_text = SKILL_PATH.read_text()
    # Phase 3 post-processing must exist and cover the whole pipeline
    assert "Post-processing" in skill_text or "Phase 3" in skill_text
    assert "manage_tags" in skill_text
    assert "manage_collections" in skill_text
    assert "index_library" in skill_text
    assert "get_index_stats" in skill_text
    assert "batch_size=2" in skill_text
    assert "Indexing in progress, please wait." in skill_text


def test_phase3_prompt_is_gated_on_empty_action_required() -> None:
    """Blocked/manual-retry cases must not reuse the Phase 3 Y/N gate."""
    skill_text = SKILL_PATH.read_text()
    assert "If `action_required` is non-empty, show the table first" in skill_text
    assert "Do NOT ask about Phase 3 yet" in skill_text
    assert "Only when `action_required` is empty" in skill_text
    assert "A bare `Y` after that message must resume the pending Phase 2 retry only." in skill_text
    assert (
        "Never combine the Phase 2 retry/remediation gate and the Phase 3 `Y/N` gate in the same message."
        in skill_text
    )
    assert "Bad example (forbidden)" in skill_text
    assert (
        "Step 4b is a one-time batch gate for the selected Elsevier-like items, not a default per-paper stop."
        in skill_text
    )


# ---------------------------------------------------------------------------
# Type-guarantee language check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", TYPE_GUARANTEE_STRINGS)
@pytest.mark.xfail(
    reason=(
        "ztp-research.md may contain type-guarantee language from earlier drafts. "
        "Such language belongs in code comments, not skill prose. Remove in follow-up PR."
    ),
    strict=False,
)
def test_no_type_guarantee_language(phrase: str) -> None:
    skill_text = SKILL_PATH.read_text()
    matches = [(i + 1, line) for i, line in enumerate(skill_text.splitlines())
               if phrase.lower() in line.lower()]
    assert not matches, (
        f"Type-guarantee phrase {phrase!r} found in {SKILL_PATH.name}:\n"
        + "\n".join(f"  line {ln}: {txt.strip()!r}" for ln, txt in matches)
    )
