"""
Composite reranking for search results.

Combines semantic similarity with section and journal quality weights
to produce a composite relevance score.
"""
import logging
from dataclasses import replace

from .models import RetrievalResult

logger = logging.getLogger(__name__)


# Default section weights (can be overridden per-query)
DEFAULT_SECTION_WEIGHTS: dict[str, float] = {
    "results": 1.0,
    "conclusion": 1.0,
    "table": 0.9,  # Tables are high-value structured content
    "methods": 0.85,
    "abstract": 0.75,
    "background": 0.7,
    "unknown": 0.85,
    "discussion": 0.65,
    "introduction": 0.5,
    "preamble": 0.3,
    "appendix": 0.3,
    "references": 0.1,
}

# Journal quartile weights (not overridable)
QUARTILE_WEIGHTS: dict[str | None, float] = {
    "Q1": 1.0,
    "Q2": 0.85,
    "Q3": 0.65,
    "Q4": 0.45,
    None: 0.7,
    "": 0.7,  # Empty string from DB treated as None
}

# Valid section labels for parameter validation
VALID_SECTIONS = set(DEFAULT_SECTION_WEIGHTS.keys())

# Valid quartile keys for parameter validation
VALID_QUARTILES = {"Q1", "Q2", "Q3", "Q4", "unknown"}


class Reranker:
    """
    Reranks search results using composite scoring.

    Composite score formula:
        composite = similarity^alpha x section_weight x journal_weight

    Where:
        - similarity: raw cosine similarity from vector search (0-1)
        - alpha: 0.7 (compresses range, gives metadata more influence)
        - section_weight: from default map or caller override
        - journal_weight: from quartile map (Q1=1.0, Q2=0.85, etc.)
    """

    def __init__(self, alpha: float = 0.7):
        """Initialize with default weight maps.

        Args:
            alpha: Similarity exponent (0-1). Lower values compress similarity
                   range, giving metadata weights more influence. Default 0.7.
        """
        self.default_section_weights = DEFAULT_SECTION_WEIGHTS.copy()
        self.quartile_weights = QUARTILE_WEIGHTS.copy()
        self.alpha = alpha

    def rerank(
        self,
        results: list[RetrievalResult],
        section_weights: dict[str, float] | None = None,
        journal_weights: dict[str, float] | None = None,
    ) -> list[RetrievalResult]:
        """
        Rerank results by composite score.

        Args:
            results: List of RetrievalResult from retriever
            section_weights: Optional overrides for section weights.
                             Keys are section labels, values are 0.0-1.0.
                             Unspecified sections keep defaults.
                             Set to 0 to exclude that section entirely.
            journal_weights: Optional overrides for journal quartile weights.
                            Use "unknown" key for papers without quartile data.

        Returns:
            New list of RetrievalResult with composite_score populated,
            sorted by composite_score descending.
            Results with composite_score=0 are excluded.
        """
        if not results:
            return []

        logger.debug(f"Reranking {len(results)} results with alpha={self.alpha}")

        # Build effective section weights
        effective_section = self.default_section_weights.copy()
        if section_weights:
            for section, weight in section_weights.items():
                effective_section[section] = max(0.0, min(1.0, weight))

        # Build effective journal weights
        # Map "unknown" -> None and "" for internal lookup
        effective_journal = self.quartile_weights.copy()
        if journal_weights:
            for quartile, weight in journal_weights.items():
                clamped = max(0.0, min(1.0, weight))
                if quartile == "unknown":
                    effective_journal[None] = clamped
                    effective_journal[""] = clamped
                else:
                    effective_journal[quartile] = clamped

        # Score each result
        scored: list[tuple[RetrievalResult, float]] = []

        for result in results:
            section_weight = effective_section.get(result.section, 0.7)
            journal_weight = effective_journal.get(result.journal_quartile, 0.7)

            composite = (max(result.score, 0.0) ** self.alpha) * section_weight * journal_weight

            logger.debug(
                f"  {result.doc_id}[{result.chunk_index}]: "
                f"sim={result.score:.3f} sect={result.section}({section_weight}) "
                f"jrnl={result.journal_quartile}({journal_weight}) "
                f"composite={composite:.3f}"
            )

            if composite > 0:
                # Create new result with composite_score set
                scored_result = replace(result, composite_score=composite)
                scored.append((scored_result, composite))

        scored.sort(key=lambda x: x[1], reverse=True)
        logger.debug(f"Reranking complete: {len(scored)} results after filtering")
        return [r for r, _ in scored]

    def score_result(
        self,
        result: RetrievalResult,
        section_weights: dict[str, float] | None = None,
        journal_weights: dict[str, float] | None = None,
    ) -> float:
        """
        Calculate composite score for a single result.

        Useful for getting the score without reranking a full list.
        """
        effective_section = self.default_section_weights.copy()
        if section_weights:
            for section, weight in section_weights.items():
                effective_section[section] = max(0.0, min(1.0, weight))

        effective_journal = self.quartile_weights.copy()
        if journal_weights:
            for quartile, weight in journal_weights.items():
                clamped = max(0.0, min(1.0, weight))
                if quartile == "unknown":
                    effective_journal[None] = clamped
                    effective_journal[""] = clamped
                else:
                    effective_journal[quartile] = clamped

        section_weight = effective_section.get(result.section, 0.7)
        journal_weight = effective_journal.get(result.journal_quartile, 0.7)

        return (max(result.score, 0.0) ** self.alpha) * section_weight * journal_weight


def validate_section_weights(section_weights: dict) -> list[str]:
    """
    Validate section_weights parameter.

    Returns list of error messages (empty if valid).
    """
    errors = []

    if not isinstance(section_weights, dict):
        return ["section_weights must be a dictionary"]

    for key, value in section_weights.items():
        if not isinstance(key, str):
            errors.append(f"section_weights key must be string, got {type(key).__name__}")
            continue

        if key not in VALID_SECTIONS:
            valid_list = ", ".join(sorted(VALID_SECTIONS))
            errors.append(f"Unknown section '{key}'. Valid sections: {valid_list}")

        if not isinstance(value, (int, float)):
            errors.append(f"section_weights['{key}'] must be numeric, got {type(value).__name__}")

    return errors


def validate_journal_weights(journal_weights: dict) -> list[str]:
    """
    Validate journal_weights parameter.

    Returns list of error messages (empty if valid).
    Note: "unknown" is used for papers with no quartile data.
    """
    errors = []

    if not isinstance(journal_weights, dict):
        return ["journal_weights must be a dictionary"]

    for key, value in journal_weights.items():
        if not isinstance(key, str):
            errors.append(f"journal_weights key must be string, got {type(key).__name__}")
            continue

        if key not in VALID_QUARTILES:
            valid_list = ", ".join(sorted(VALID_QUARTILES))
            errors.append(f"Unknown quartile '{key}'. Valid quartiles: {valid_list}")

        if not isinstance(value, (int, float)):
            errors.append(f"journal_weights['{key}'] must be numeric, got {type(value).__name__}")

    return errors
