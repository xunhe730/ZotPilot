"""
Journal quality ranking via SCImago quartile lookup.

Provides a 3-tier matching strategy:
1. Exact match on normalized journal name
2. Acronym expansion then exact match
3. Fuzzy matching with rapidfuzz (score >= 85)
"""
import csv
import logging
import re
from pathlib import Path
from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)


# Common journal abbreviations for tier 2 expansion
ABBREVIATIONS: dict[str, list[str]] = {
    "trans.": ["transactions"],
    "biomed.": ["biomedical"],
    "eng.": ["engineering"],
    "j.": ["journal"],
    "proc.": ["proceedings"],
    "int.": ["international"],
    "sci.": ["science", "sciences"],
    "rev.": ["review", "reviews"],
    "lett.": ["letters"],
    "comput.": ["computer", "computing", "computational"],
    "med.": ["medicine", "medical"],
    "phys.": ["physics", "physical"],
    "chem.": ["chemistry", "chemical"],
    "appl.": ["applied"],
    "res.": ["research"],
    "biol.": ["biology", "biological"],
    "conf.": ["conference"],
    "symp.": ["symposium"],
    "ann.": ["annual", "annals"],
    "eur.": ["european"],
    "am.": ["american"],
    "nat.": ["national", "nature", "natural"],
    "tech.": ["technology", "technical"],
    "syst.": ["systems"],
    "commun.": ["communications"],
    "electr.": ["electrical", "electronic", "electronics"],
    "rehabil.": ["rehabilitation"],
    "neurosci.": ["neuroscience"],
    "cardiovasc.": ["cardiovascular"],
    "physiol.": ["physiology", "physiological"],
}


def _normalize_title(title: str) -> str:
    """
    Normalize a journal title for lookup.

    - Lowercase
    - Replace punctuation (& : - /) with spaces
    - Collapse multiple spaces
    - Strip
    """
    title = title.lower()
    title = re.sub(r"[&:\-/]", " ", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip()


def _expand_abbreviations(title: str) -> list[str]:
    """
    Generate all expansions of abbreviated journal name.

    Returns list of possible expanded forms.
    """
    title_lower = title.lower()

    # Find all abbreviations in the title
    expansions = [title_lower]

    for abbrev, full_forms in ABBREVIATIONS.items():
        new_expansions = []
        for exp in expansions:
            if abbrev in exp:
                for full in full_forms:
                    new_expansions.append(exp.replace(abbrev, full, 1))
            else:
                new_expansions.append(exp)
        expansions = new_expansions

    # Normalize all expansions
    return [_normalize_title(e) for e in expansions]


class JournalRanker:
    """
    SCImago-based journal quartile lookup.

    Loads a pre-processed CSV with normalized journal titles and their
    best quartile (Q1/Q2/Q3/Q4).
    """

    def __init__(self, csv_path: Path | None = None, overrides_path: Path | None = None):
        """
        Load the lookup table.

        Args:
            csv_path: Path to scimago_quartiles.csv. If None, uses the
                      bundled data file in the package data directory.
            overrides_path: Path to journal_overrides.csv. If None, uses the
                            bundled overrides file in the package data directory.
        """
        if csv_path is None:
            csv_path = Path(__file__).parent / "data" / "scimago_quartiles.csv"

        self._lookup: dict[str, str] = {}
        self._all_titles: list[str] = []  # For fuzzy matching
        self._cache: dict[str, str | None] = {}  # Query cache
        self._overrides: dict[str, str] = {}  # Manual override mappings
        self._csv_path: Path | None = csv_path
        self._csv_mtime: float | None = None

        if csv_path.exists():
            self._load_csv(csv_path)
            self._csv_mtime = csv_path.stat().st_mtime
        else:
            logger.warning(
                f"SCImago CSV not found at {csv_path}. "
                "Journal quartile ranking will be disabled. "
                "Run scripts/prepare_scimago.py to generate the file."
            )

        # Load overrides (takes precedence over SCImago lookups)
        if overrides_path is None:
            overrides_path = Path(__file__).parent / "data" / "journal_overrides.csv"

        if overrides_path.exists():
            self._load_overrides(overrides_path)

    def _load_csv(self, csv_path: Path) -> None:
        """Load the lookup table from CSV."""
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                title = row.get("title_normalized", "").strip()
                quartile = row.get("quartile", "").strip()
                if title and quartile:
                    self._lookup[title] = quartile
                    self._all_titles.append(title)

    def _load_overrides(self, path: Path) -> None:
        """Load manual override mappings from CSV.

        Overrides take precedence over SCImago lookups. Use this to correct
        fuzzy matching mistakes or add journals not in the SCImago database.

        File format: input_title,correct_quartile (comments start with #)
        """
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",", 1)
                if len(parts) == 2:
                    title, quartile = parts
                    normalized = _normalize_title(title.strip())
                    self._overrides[normalized] = quartile.strip()

    def lookup(self, publication: str) -> str | None:
        """
        Look up journal quartile.

        Args:
            publication: Journal/publication name from Zotero

        Returns:
            'Q1', 'Q2', 'Q3', 'Q4', or None if not found
        """
        if not publication:
            return None

        # Check cache first
        if publication in self._cache:
            return self._cache[publication]

        result = self._lookup_uncached(publication)
        self._cache[publication] = result
        return result

    def _lookup_uncached(self, publication: str) -> str | None:
        """Perform the actual lookup (without caching)."""
        normalized = _normalize_title(publication)

        # Tier 0: Check manual overrides first (highest priority)
        if normalized in self._overrides:
            return self._overrides[normalized]

        # Tier 1: Exact match on normalized title
        if normalized in self._lookup:
            return self._lookup[normalized]

        # Tier 2: Expand abbreviations and try exact match
        for expanded in _expand_abbreviations(publication):
            if expanded in self._overrides:
                return self._overrides[expanded]
            if expanded in self._lookup:
                return self._lookup[expanded]

        # Tier 3: Fuzzy match using extractOne (optimized)
        # Threshold raised to 90 to reduce false positive matches
        if self._all_titles:
            match = process.extractOne(
                normalized,
                self._all_titles,
                scorer=fuzz.ratio,
                score_cutoff=90
            )
            if match:
                matched_title, score, _ = match
                return self._lookup[matched_title]

        return None

    @property
    def loaded(self) -> bool:
        """Check if lookup table is loaded."""
        return len(self._lookup) > 0

    def is_stale(self) -> bool:
        """Check if CSV has been modified since loading."""
        if self._csv_path is None or not self._csv_path.exists():
            return False
        current_mtime = self._csv_path.stat().st_mtime
        return current_mtime != self._csv_mtime

    def reload_if_stale(self) -> bool:
        """Reload CSV if modified since loading.

        Returns True if reloaded.

        Note: This is not called automatically. Callers should invoke this
        at appropriate points (e.g., before a batch indexing run) if they
        want hot-reload behavior. For the MCP server, reloading would require
        restarting the server process.
        """
        if not self.is_stale():
            return False

        self._lookup.clear()
        self._all_titles.clear()
        self._cache.clear()
        self._load_csv(self._csv_path)
        self._csv_mtime = self._csv_path.stat().st_mtime
        logger.info(f"Reloaded SCImago data: {len(self._lookup)} journals")
        return True

    def stats(self) -> dict:
        """Return statistics about the loaded data."""
        quartile_counts = {"Q1": 0, "Q2": 0, "Q3": 0, "Q4": 0}
        for q in self._lookup.values():
            if q in quartile_counts:
                quartile_counts[q] += 1

        return {
            "total_journals": len(self._lookup),
            "quartile_counts": quartile_counts,
        }
