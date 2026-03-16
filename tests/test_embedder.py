"""Tests for embedding providers."""
import pytest
from unittest.mock import patch, MagicMock
from zotpilot.embeddings import create_embedder
from zotpilot.embeddings.local import LocalEmbedder
from zotpilot.embeddings.gemini import GeminiEmbedder, EmbeddingError


class TestLocalEmbedder:
    def test_empty_input(self):
        embedder = LocalEmbedder()
        result = embedder.embed([])
        assert result == []

    def test_embed_returns_vectors(self):
        embedder = LocalEmbedder()
        result = embedder.embed(["test text"])
        assert len(result) == 1
        assert len(result[0]) == 384  # all-MiniLM-L6-v2 output size
        assert all(isinstance(v, float) for v in result[0])

    def test_embed_query(self):
        embedder = LocalEmbedder()
        result = embedder.embed_query("test query")
        assert len(result) == 384

    def test_dimensions_attribute(self):
        embedder = LocalEmbedder()
        assert embedder.dimensions == 384

    def test_multiple_texts(self):
        embedder = LocalEmbedder()
        texts = ["first text", "second text", "third text"]
        result = embedder.embed(texts)
        assert len(result) == 3
        for vec in result:
            assert len(vec) == 384


class TestCreateEmbedder:
    def test_create_local(self):
        config = MagicMock()
        config.embedding_provider = "local"
        embedder = create_embedder(config)
        assert isinstance(embedder, LocalEmbedder)

    def test_create_gemini(self):
        config = MagicMock()
        config.embedding_provider = "gemini"
        config.embedding_model = "gemini-embedding-001"
        config.embedding_dimensions = 768
        config.gemini_api_key = "test-key"
        config.embedding_timeout = 120.0
        config.embedding_max_retries = 3

        with patch("google.genai.Client") as mock_client:
            embedder = create_embedder(config)
            assert isinstance(embedder, GeminiEmbedder)

    def test_invalid_provider(self):
        config = MagicMock()
        config.embedding_provider = "invalid"
        with pytest.raises(ValueError, match="Invalid embedding_provider"):
            create_embedder(config)
