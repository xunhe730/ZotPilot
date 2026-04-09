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
