"""P10: Ingestion module line-count ceiling.

Asserts:
1. src/zotpilot/tools/ingestion/ is a package directory (not a flat .py file).
2. The total non-blank, non-comment source lines across all .py files in the
   package EXCLUDING __init__.py do not exceed 800 lines.
3. src/zotpilot/tools/ingestion.py does NOT exist as a flat file.
"""

from __future__ import annotations

from pathlib import Path

LOC_CEILING = 800
INGESTION_PKG = (
    Path(__file__).parent.parent / "src" / "zotpilot" / "tools" / "ingestion"
)
INGESTION_FLAT = INGESTION_PKG.parent / "ingestion.py"


def test_ingestion_is_package_not_flat_file() -> None:
    """ingestion must be a package directory, not a single .py module."""
    assert INGESTION_PKG.is_dir(), (
        f"Expected {INGESTION_PKG} to be a directory (package), "
        f"but it is {'a file' if INGESTION_PKG.is_file() else 'absent'}."
    )
    assert (INGESTION_PKG / "__init__.py").exists(), (
        f"{INGESTION_PKG} exists as a directory but has no __init__.py — "
        "it is not a proper Python package."
    )


def test_flat_ingestion_file_does_not_exist() -> None:
    """src/zotpilot/tools/ingestion.py must NOT exist (replaced by the package)."""
    assert not INGESTION_FLAT.exists(), (
        f"Flat file {INGESTION_FLAT} still exists alongside the ingestion/ package. "
        "Remove it — having both causes import ambiguity."
    )


def _count_lines(path: Path) -> int:
    """Count non-blank, non-comment source lines (LLOC-style).

    Matches the metric stated in the module docstring: "non-blank, non-comment
    source lines". Blank lines, pure comment lines, and stand-alone docstring
    delimiters are excluded so that formatting choices don't pressure the
    ceiling. This aligns with `coding-style.md` which talks about logical
    code, not literal `wc -l` output.
    """
    in_docstring = False
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        # Handle triple-quoted docstrings (opening/closing on same line toggles)
        triple_count = stripped.count('"""') + stripped.count("'''")
        if in_docstring:
            if triple_count % 2 == 1:
                in_docstring = False
            continue
        if triple_count == 1:
            in_docstring = True
            continue
        # Skip blanks, comments, and docstring-only lines opened+closed same line
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            # Whole-line docstring that opens and closes (triple_count == 2)
            continue
        count += 1
    return count


def test_ingestion_package_loc_within_ceiling() -> None:
    """Total line count of all non-__init__.py .py files must be <= 800."""
    py_files = [
        f for f in INGESTION_PKG.glob("*.py")
        if f.name != "__init__.py"
    ]
    assert py_files, (
        f"No .py files (other than __init__.py) found under {INGESTION_PKG}. "
        "At least one implementation module is expected."
    )

    totals: dict[str, int] = {}
    for f in sorted(py_files):
        totals[f.name] = _count_lines(f)

    total = sum(totals.values())
    breakdown = ", ".join(f"{name}={n}" for name, n in sorted(totals.items()))
    assert total <= LOC_CEILING, (
        f"Ingestion package LOC ceiling exceeded: {total} > {LOC_CEILING} lines "
        f"({breakdown}). Refactor or split modules to stay within the ceiling."
    )
