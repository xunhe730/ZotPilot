"""Regression tests: translation module fully removed (P2-16)."""
import importlib

import pytest


class TestTranslationRemoved:
    def test_translation_module_removed(self):
        """import zotpilot.translation should raise ModuleNotFoundError."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("zotpilot.translation")

    def test_no_translation_import_in_search(self):
        """search module should not depend on translation."""
        import zotpilot.tools.search as search_mod
        source_file = search_mod.__file__
        with open(source_file) as f:
            source = f.read()
        assert "translate_to_english" not in source
        assert "contains_chinese" not in source

    def test_search_papers_no_auto_translate(self):
        """Chinese query should NOT trigger translation — retriever.search called once."""
        from unittest.mock import MagicMock, patch


        mock_retriever = MagicMock()
        mock_retriever.search.return_value = []
        mock_reranker = MagicMock()
        mock_config = MagicMock()
        mock_config.oversample_multiplier = 3
        mock_config.rerank_enabled = False

        with patch("zotpilot.tools.search._get_retriever", return_value=mock_retriever), \
             patch("zotpilot.tools.search._get_reranker", return_value=mock_reranker), \
             patch("zotpilot.tools.search._get_config", return_value=mock_config):
            from zotpilot.tools.search import search_papers
            search_papers(query="深度学习在流体力学中的应用")

        # Retriever should be called exactly once (no bilingual duplicate)
        assert mock_retriever.search.call_count == 1
