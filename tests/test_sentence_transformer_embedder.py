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
            with pytest.raises(Exception, match="sentence-transformers is not installed"):
                SentenceTransformerEmbedder("allenai/specter2", 768)

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
    def test_embed_returns_vectors(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder("allenai/specter2", 768)

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
    def test_embed_query(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder("allenai/specter2", 768)

        mock_model = MagicMock()
        mock_embedding = MagicMock()
        mock_embedding.tolist.return_value = [0.2] * 768
        mock_model.encode.return_value = [mock_embedding]
        embedder._model = mock_model

        result = embedder.embed_query("test query")
        assert len(result) == 768
        mock_model.encode.assert_called_once_with(["test query"], show_progress_bar=False)

    @patch("zotpilot.embeddings.sentence_transformer.importlib.util.find_spec")
    def test_lazy_model_loading(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder("allenai/specter2", 768)
        assert embedder._model is None

        mock_model = MagicMock()
        mock_embedding = MagicMock()
        mock_embedding.tolist.return_value = [0.1] * 768
        mock_model.encode.return_value = [mock_embedding]

        mock_st_class = MagicMock(return_value=mock_model)
        mock_st_module = MagicMock()
        mock_st_module.SentenceTransformer = mock_st_class

        with patch.dict("sys.modules", {"sentence_transformers": mock_st_module}):
            embedder.embed(["text"])
            mock_st_class.assert_called_once_with("allenai/specter2")
            assert embedder._model is mock_model

    @patch("zotpilot.embeddings.sentence_transformer.importlib.util.find_spec")
    def test_model_reused_after_loading(self, mock_find_spec):
        mock_find_spec.return_value = True
        embedder = SentenceTransformerEmbedder("allenai/specter2", 768)

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


class TestCreateEmbedderSentenceTransformer:
    def test_create_sentence_transformer(self):
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
