"""Tests for journal_ranker module (pure functions + JournalRanker lookup)."""
import pytest
from pathlib import Path
from zotpilot.journal_ranker import _normalize_title, _expand_abbreviations, JournalRanker


class TestNormalizeTitle:
    def test_basic(self):
        assert _normalize_title("Nature") == "nature"

    def test_punctuation_replaced(self):
        assert _normalize_title("Science & Engineering") == "science engineering"
        assert _normalize_title("A/B-C:D") == "a b c d"

    def test_whitespace_collapse(self):
        result = _normalize_title("  Multiple   Spaces  ")
        assert "  " not in result
        assert result == "multiple spaces"


class TestExpandAbbreviations:
    def test_no_abbreviations(self):
        result = _expand_abbreviations("nature")
        assert "nature" in result

    def test_single_abbreviation(self):
        result = _expand_abbreviations("J. Fluid Mech.")
        # Should expand "j." -> "journal" and produce at least one expansion
        assert any("journal" in r for r in result)

    def test_multiple_abbreviations(self):
        result = _expand_abbreviations("Int. J. Comput. Sci.")
        # Should have many expansions from int./j./comput./sci.
        assert len(result) > 1


class TestJournalRanker:
    @pytest.fixture
    def ranker_with_data(self, tmp_path):
        """Create a JournalRanker with test CSV data."""
        csv_path = tmp_path / "test_quartiles.csv"
        csv_path.write_text(
            "title_normalized,quartile\n"
            "nature,Q1\n"
            "journal applied research,Q1\n"
            "plos one,Q2\n"
            "applied sciences,Q3\n"
        )
        return JournalRanker(csv_path=csv_path, overrides_path=tmp_path / "nonexistent.csv")

    def test_exact_match(self, ranker_with_data):
        assert ranker_with_data.lookup("Nature") == "Q1"

    def test_abbreviation_expansion(self, ranker_with_data):
        # "J." -> "journal", "Appl." -> "applied", "Res." -> "research" are all in ABBREVIATIONS
        result = ranker_with_data.lookup("J. Appl. Res.")
        assert result == "Q1"

    def test_fuzzy_match(self, ranker_with_data):
        # "applied sciences" vs "applied sciences" — exact or near-exact
        result = ranker_with_data.lookup("Applied Sciences")
        assert result == "Q3"

    def test_no_match(self, ranker_with_data):
        result = ranker_with_data.lookup("Totally Unknown Journal XYZ 12345")
        assert result is None

    def test_empty_publication(self, ranker_with_data):
        assert ranker_with_data.lookup("") is None

    def test_cache_works(self, ranker_with_data):
        ranker_with_data.lookup("Nature")
        ranker_with_data.lookup("Nature")
        assert "Nature" in ranker_with_data._cache

    def test_overrides(self, tmp_path):
        csv_path = tmp_path / "q.csv"
        csv_path.write_text("title_normalized,quartile\nnature,Q2\n")
        overrides_path = tmp_path / "overrides.csv"
        overrides_path.write_text("nature,Q1\n")
        ranker = JournalRanker(csv_path=csv_path, overrides_path=overrides_path)
        assert ranker.lookup("Nature") == "Q1"  # Override wins

    def test_stats(self, ranker_with_data):
        stats = ranker_with_data.stats()
        assert stats["total_journals"] == 4
        assert stats["quartile_counts"]["Q1"] == 2

    def test_loaded(self, ranker_with_data):
        assert ranker_with_data.loaded is True

    def test_no_csv(self, tmp_path):
        ranker = JournalRanker(csv_path=tmp_path / "missing.csv")
        assert ranker.loaded is False
        assert ranker.lookup("Nature") is None
