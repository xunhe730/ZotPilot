"""Tests for embedding providers."""
from unittest.mock import MagicMock, patch

import pytest

from zotpilot.embeddings import create_embedder
from zotpilot.embeddings.dashscope import QUERY_INSTRUCT, DashScopeEmbedder
from zotpilot.embeddings.gemini import GeminiEmbedder
from zotpilot.embeddings.local import LocalEmbedder


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

        with patch("google.genai.Client"):
            embedder = create_embedder(config)
            assert isinstance(embedder, GeminiEmbedder)

    def test_create_dashscope(self):
        config = MagicMock()
        config.embedding_provider = "dashscope"
        config.embedding_model = "text-embedding-v4"
        config.embedding_dimensions = 1536
        config.dashscope_api_key = "test-key"
        config.dashscope_embedding_endpoint = "native"
        config.embedding_timeout = 120.0
        config.embedding_max_retries = 3

        embedder = create_embedder(config)

        assert isinstance(embedder, DashScopeEmbedder)
        assert embedder.dimensions == 1536
        assert embedder.endpoint == "native"

    def test_invalid_provider(self):
        config = MagicMock()
        config.embedding_provider = "invalid"
        with pytest.raises(ValueError, match="Invalid embedding_provider"):
            create_embedder(config)


class TestDashScopeEmbedder:
    def test_compatible_endpoint_is_default(self):
        embedder = DashScopeEmbedder(api_key="test-key", max_retries=1)
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [{"index": 0, "embedding": [0.1, 0.2]}]
        }
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response
            result = embedder.embed(["paper passage"], task_type="RETRIEVAL_DOCUMENT")

        assert result == [[0.1, 0.2]]
        post = mock_client.return_value.__enter__.return_value.post
        assert post.call_args.args[0] == "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
        payload = post.call_args.kwargs["json"]
        assert payload["input"] == ["paper passage"]
        assert payload["dimensions"] == 1024
        assert payload["encoding_format"] == "float"
        assert "parameters" not in payload

    def test_document_payload_uses_native_text_type(self):
        embedder = DashScopeEmbedder(api_key="test-key", endpoint="native", max_retries=1)
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "output": {"embeddings": [{"text_index": 0, "embedding": [0.1, 0.2]}]}
        }
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response
            result = embedder.embed(["paper passage"], task_type="RETRIEVAL_DOCUMENT")

        assert result == [[0.1, 0.2]]
        payload = mock_client.return_value.__enter__.return_value.post.call_args.kwargs["json"]
        assert payload["input"]["texts"] == ["paper passage"]
        assert payload["parameters"]["text_type"] == "document"
        assert payload["parameters"]["dimension"] == 1024
        assert "instruct" not in payload["parameters"]

    def test_document_payload_truncates_long_text(self):
        embedder = DashScopeEmbedder(api_key="test-key", endpoint="native", max_retries=1)
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "output": {"embeddings": [{"text_index": 0, "embedding": [0.1, 0.2]}]}
        }
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response
            embedder.embed(["x" * 7000], task_type="RETRIEVAL_DOCUMENT")

        payload = mock_client.return_value.__enter__.return_value.post.call_args.kwargs["json"]
        assert len(payload["input"]["texts"][0]) == 6000

    def test_document_batches_use_conservative_size(self):
        embedder = DashScopeEmbedder(api_key="test-key", endpoint="native", max_retries=1)

        def post(*_args, **kwargs):
            n_texts = len(kwargs["json"]["input"]["texts"])
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "output": {
                    "embeddings": [{"text_index": i, "embedding": [float(i)]} for i in range(n_texts)]
                }
            }
            mock_response.raise_for_status.return_value = None
            return mock_response

        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.dashscope.time.sleep"):
            mock_client.return_value.__enter__.return_value.post.side_effect = post
            result = embedder.embed([f"text {i}" for i in range(6)], task_type="RETRIEVAL_DOCUMENT")

        post_calls = mock_client.return_value.__enter__.return_value.post.call_args_list
        assert len(result) == 6
        assert len(post_calls) == 2
        assert len(post_calls[0].kwargs["json"]["input"]["texts"]) == 5
        assert len(post_calls[1].kwargs["json"]["input"]["texts"]) == 1

    def test_document_batch_falls_back_to_single_text_requests(self):
        embedder = DashScopeEmbedder(api_key="test-key", endpoint="native", max_retries=1, batch_size=2)
        failed_response = MagicMock()
        failed_response.raise_for_status.side_effect = RuntimeError("batch too large")

        def post(*_args, **kwargs):
            texts = kwargs["json"]["input"]["texts"]
            if len(texts) > 1:
                return failed_response
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "output": {"embeddings": [{"text_index": 0, "embedding": [float(len(texts[0]))]}]}
            }
            mock_response.raise_for_status.return_value = None
            return mock_response

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.side_effect = post
            result = embedder.embed(["one", "two"], task_type="RETRIEVAL_DOCUMENT")

        assert result == [[3.0], [3.0]]

    def test_query_payload_uses_text_type_and_instruct(self):
        embedder = DashScopeEmbedder(api_key="test-key", endpoint="native", max_retries=1)
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "output": {"embeddings": [{"text_index": 0, "embedding": [0.3, 0.4]}]}
        }
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response
            result = embedder.embed_query("sleep spindle memory")

        assert result == [0.3, 0.4]
        payload = mock_client.return_value.__enter__.return_value.post.call_args.kwargs["json"]
        assert payload["parameters"]["text_type"] == "query"
        assert payload["parameters"]["instruct"] == QUERY_INSTRUCT

    def test_invalid_endpoint_rejected(self):
        with pytest.raises(ValueError, match="compatible.*native"):
            DashScopeEmbedder(api_key="test-key", endpoint="invalid")
