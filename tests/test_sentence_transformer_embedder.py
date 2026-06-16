"""Tests for SentenceTransformerEmbedder."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.embeddings import create_embedder
from zotpilot.embeddings.sentence_transformer import SentenceTransformerEmbedder


class TestSentenceTransformerEmbedder:
    def test_import_error_without_sentence_transformers(self):
        with patch(
            "zotpilot.embeddings.sentence_transformer.importlib.util.find_spec",
            return_value=None,
        ):
            embedder = SentenceTransformerEmbedder(
                "microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext", 768
            )
            with pytest.raises(Exception, match="sentence-transformers is not installed"):
                embedder.embed(["test"])

    def test_import_error_without_adapters(self):
        with patch(
            "zotpilot.embeddings.sentence_transformer.importlib.util.find_spec",
            return_value=None,
        ):
            embedder = SentenceTransformerEmbedder("allenai/specter2", 768)
            with pytest.raises(Exception, match="adapters is not installed"):
                embedder.embed(["test"])

    @patch("zotpilot.embeddings.sentence_transformer.importlib.util.find_spec")
    def test_dimensions_attribute(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder("allenai/specter2", 768)
        assert embedder.dimensions == 768

    @patch("zotpilot.embeddings.sentence_transformer.importlib.util.find_spec")
    def test_embed_empty_input(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder("allenai/specter2", 768)
        result = embedder.embed([])
        assert result == []

    @patch("zotpilot.embeddings.sentence_transformer.importlib.util.find_spec")
    def test_specter2_uses_adapters(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder("allenai/specter2", 768)
        assert embedder._use_adapters is True

    @patch("zotpilot.embeddings.sentence_transformer.importlib.util.find_spec")
    def test_pubmedbert_uses_sentence_transformers(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder(
            "microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext", 768
        )
        assert embedder._use_adapters is False

    @patch("zotpilot.embeddings.sentence_transformer.importlib.util.find_spec")
    def test_embed_returns_vectors_pubmedbert(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder(
            "microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext", 768
        )

        mock_model = MagicMock()
        mock_embedding = MagicMock()
        mock_embedding.tolist.return_value = [0.1] * 768
        mock_model.encode.return_value = [mock_embedding]
        embedder._model = mock_model

        result = embedder.embed(["test text"])
        assert len(result) == 1
        assert len(result[0]) == 768
        mock_model.encode.assert_called_once_with(["test text"], show_progress_bar=False)

    @patch("zotpilot.embeddings.sentence_transformer.importlib.util.find_spec")
    def test_embed_query_pubmedbert(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder(
            "microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext", 768
        )

        mock_model = MagicMock()
        mock_embedding = MagicMock()
        mock_embedding.tolist.return_value = [0.2] * 768
        mock_model.encode.return_value = [mock_embedding]
        embedder._model = mock_model

        result = embedder.embed_query("test query")
        assert len(result) == 768
        mock_model.encode.assert_called_once_with(["test query"], show_progress_bar=False)

    @patch("zotpilot.embeddings.sentence_transformer.importlib.util.find_spec")
    def test_model_reused_after_loading(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder(
            "microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext", 768
        )

        mock_model = MagicMock()
        mock_embedding = MagicMock()
        mock_embedding.tolist.return_value = [0.1] * 768
        mock_model.encode.return_value = [mock_embedding]

        mock_st_class = MagicMock(return_value=mock_model)
        mock_st_module = MagicMock()
        mock_st_module.SentenceTransformer = mock_st_class

        with patch.dict("sys.modules", {"sentence_transformers": mock_st_module}):
            embedder.embed(["first"])
            embedder.embed(["second"])
            mock_st_class.assert_called_once()

    @patch("zotpilot.embeddings.sentence_transformer.importlib.util.find_spec")
    def test_specter2_flags_use_adapters(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder("allenai/specter2", 768)
        assert embedder._use_adapters is True
        assert embedder._model_name == "allenai/specter2"

    @patch("zotpilot.embeddings.sentence_transformer.importlib.util.find_spec")
    def test_specter2_embed_dispatches_to_adapters(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder("allenai/specter2", 768)
        assert embedder._use_adapters is True
        assert callable(embedder._embed_specter2)
        assert callable(embedder._load_specter2)


class TestCreateEmbedderSentenceTransformer:
    def test_create_sentence_transformer_specter2(self):
        config = SimpleNamespace(
            embedding_provider="sentence-transformer",
            embedding_model="allenai/specter2",
            embedding_dimensions=768,
        )
        with patch(
            "zotpilot.embeddings.sentence_transformer.importlib.util.find_spec",
            return_value=True,
        ):
            embedder = create_embedder(config)
            assert isinstance(embedder, SentenceTransformerEmbedder)
            assert embedder.dimensions == 768
            assert embedder._use_adapters is True

    def test_create_sentence_transformer_pubmedbert(self):
        config = SimpleNamespace(
            embedding_provider="sentence-transformer",
            embedding_model="microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext",
            embedding_dimensions=768,
        )
        with patch(
            "zotpilot.embeddings.sentence_transformer.importlib.util.find_spec",
            return_value=True,
        ):
            embedder = create_embedder(config)
            assert isinstance(embedder, SentenceTransformerEmbedder)
            assert embedder._model_name == "microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract-fulltext"
            assert embedder._use_adapters is False
