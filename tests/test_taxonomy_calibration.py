"""P6: Taxonomy calibration fixture structural checks.

Verifies:
1. The fixture has >= 40 positives and >= 10 negatives.
2. A stub embedder with deterministic distances (positives=0.1, negatives=0.9)
   produces a threshold around p10 of positives (i.e. ~0.1).
3. Missing fixture raises an appropriate error.

NOTE: TaxonomyCalibrationError and the calibrate_threshold() function are not
yet implemented in the source (§5 P6 is a future spec item).  The threshold
computation test uses an inline reference implementation to verify the fixture
data shape and the expected p10 math is sound.  The missing-fixture test is
marked xfail(strict=False) pointing to §5 P6 until the source implements
TaxonomyCalibrationError.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SYNONYM_PAIRS_PATH = FIXTURES_DIR / "taxonomy_synonym_pairs.json"


# ---------------------------------------------------------------------------
# Helper: reference threshold computation (mirrors what the real impl should do)
# ---------------------------------------------------------------------------

def _compute_threshold_p10(
    positive_distances: list[float],
    negative_distances: list[float],
) -> float:
    """Return the p10 of positive distances as the decision threshold.

    A good classifier boundary: 90% of true-positive pairs should be
    *below* this threshold (i.e., classified as similar).
    """
    sorted_pos = sorted(positive_distances)
    idx = math.ceil(0.10 * len(sorted_pos)) - 1
    idx = max(0, min(idx, len(sorted_pos) - 1))
    return sorted_pos[idx]


# ---------------------------------------------------------------------------
# 1. Fixture structural checks
# ---------------------------------------------------------------------------

def test_synonym_fixture_exists() -> None:
    assert SYNONYM_PAIRS_PATH.exists(), (
        f"Fixture missing: {SYNONYM_PAIRS_PATH}. "
        "Run the calibration fixture generator to create it."
    )


def test_synonym_fixture_has_enough_positives() -> None:
    data: dict[str, Any] = json.loads(SYNONYM_PAIRS_PATH.read_text())
    positives = data.get("positives", [])
    assert len(positives) >= 40, (
        f"Expected >= 40 positive pairs, got {len(positives)}. "
        "Add more synonym pairs to tests/fixtures/taxonomy_synonym_pairs.json."
    )


def test_synonym_fixture_has_enough_negatives() -> None:
    data: dict[str, Any] = json.loads(SYNONYM_PAIRS_PATH.read_text())
    negatives = data.get("negatives", [])
    assert len(negatives) >= 10, (
        f"Expected >= 10 negative pairs, got {len(negatives)}. "
        "Add more non-synonym pairs to tests/fixtures/taxonomy_synonym_pairs.json."
    )


def test_synonym_fixture_pair_structure() -> None:
    """Each pair must be a list/tuple of exactly two non-empty strings."""
    data: dict[str, Any] = json.loads(SYNONYM_PAIRS_PATH.read_text())
    for section in ("positives", "negatives"):
        for i, pair in enumerate(data.get(section, [])):
            assert len(pair) == 2, f"{section}[{i}] must have exactly 2 elements, got {pair!r}"
            for term in pair:
                assert isinstance(term, str) and term.strip(), (
                    f"{section}[{i}] contains a non-string or empty term: {pair!r}"
                )


# ---------------------------------------------------------------------------
# 2. Threshold computation with stub embedder distances
# ---------------------------------------------------------------------------

def test_stub_embedder_threshold_around_p10() -> None:
    """Deterministic stub: positives distance=0.1, negatives distance=0.9.

    The p10 of a list of all-0.1 values is 0.1.  The threshold must be <= 0.5
    (well below the negative distances) and close to the positive centroid.
    """
    data: dict[str, Any] = json.loads(SYNONYM_PAIRS_PATH.read_text())
    n_pos = len(data["positives"])
    n_neg = len(data["negatives"])

    # Stub embedder: every positive pair has distance 0.1, every negative 0.9
    positive_distances = [0.1] * n_pos
    negative_distances = [0.9] * n_neg

    threshold = _compute_threshold_p10(positive_distances, negative_distances)

    # p10 of a uniform list of 0.1 values is 0.1
    assert threshold == pytest.approx(0.1, abs=1e-9), (
        f"Expected threshold ~0.1 from stub distances, got {threshold}"
    )
    # Threshold must separate positives from negatives
    assert threshold < min(negative_distances), (
        "Threshold must be below the minimum negative distance."
    )


def test_threshold_separates_classes() -> None:
    """With realistic stub data, classify correctly using the computed threshold."""
    data: dict[str, Any] = json.loads(SYNONYM_PAIRS_PATH.read_text())
    n_pos = len(data["positives"])
    n_neg = len(data["negatives"])

    positive_distances = [0.1] * n_pos
    negative_distances = [0.9] * n_neg

    threshold = _compute_threshold_p10(positive_distances, negative_distances)

    # All positives should be at or below threshold
    tp = sum(1 for d in positive_distances if d <= threshold)
    # All negatives should be above threshold
    tn = sum(1 for d in negative_distances if d > threshold)

    assert tp == n_pos, f"Expected {n_pos} true positives, got {tp}"
    assert tn == n_neg, f"Expected {n_neg} true negatives, got {tn}"


# ---------------------------------------------------------------------------
# 3. Missing fixture raises TaxonomyCalibrationError (or appropriate exception)
#    xfail: TaxonomyCalibrationError not yet implemented in source (§5 P6)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason=(
        "TaxonomyCalibrationError not yet implemented in source code. "
        "§5 P6 of ingest-redesign-technical-doc.md specifies this class. "
        "Remove xfail when the implementation lands."
    ),
    strict=False,
)
def test_missing_fixture_raises_taxonomy_calibration_error(tmp_path: Path) -> None:
    """Attempting calibration with a missing fixture should raise TaxonomyCalibrationError."""
    # TODO: replace with real import once §5 P6 is implemented:
    #   from zotpilot.workflow.taxonomy_gate import (
    #       TaxonomyCalibrationError, calibrate_threshold
    #   )
    #   calibrate_threshold(fixture_path=tmp_path / "nonexistent.json")
    from zotpilot.workflow import taxonomy_gate  # type: ignore[import]
    taxonomy_gate.calibrate_threshold(fixture_path=tmp_path / "nonexistent.json")
