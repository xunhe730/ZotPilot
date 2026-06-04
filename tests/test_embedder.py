"""Tests for embedding providers."""
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from zotpilot.embeddings import create_embedder
from zotpilot.embeddings.dashscope import QUERY_INSTRUCT, DashScopeEmbedder
from zotpilot.embeddings.gemini import EmbeddingError, GeminiEmbedder
from zotpilot.embeddings.local import LocalEmbedder
from zotpilot.embeddings.openai_compat import OpenAICompatEmbedder


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


def _oai_response(embeddings):
    """Build a mocked OpenAI-compatible /embeddings response."""
    resp = MagicMock()
    resp.json.return_value = {
        "data": [{"index": i, "embedding": e} for i, e in enumerate(embeddings)]
    }
    resp.raise_for_status.return_value = None
    return resp


def _status_error(code, text="upstream error"):
    """Build a MagicMock response whose raise_for_status raises HTTP `code`."""
    request = httpx.Request("POST", "http://x/embeddings")
    response = httpx.Response(code, request=request, text=text)
    resp = MagicMock()
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "err", request=request, response=response
    )
    return resp


class TestOpenAICompatEmbedder:
    def test_payload_dims_url_and_format(self):
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=2, base_url="https://api.example.com/v1", max_retries=1
        )
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = _oai_response([[0.1, 0.2]])
            result = embedder.embed(["paper passage"], task_type="RETRIEVAL_DOCUMENT")

        assert result == [[0.1, 0.2]]
        assert embedder.dimensions == 2
        post = mock_client.return_value.__enter__.return_value.post
        assert post.call_args.args[0] == "https://api.example.com/v1/embeddings"
        payload = post.call_args.kwargs["json"]
        assert payload["input"] == ["paper passage"]
        assert payload["dimensions"] == 2  # FROM config, not hardcoded
        assert payload["encoding_format"] == "float"
        assert "task_type" not in payload  # task_type ignored in compatible mode

    def test_empty_input_short_circuits(self):
        embedder = OpenAICompatEmbedder(model="m", dimensions=2, base_url="https://x/v1")
        with patch("httpx.Client") as mock_client:
            assert embedder.embed([]) == []
            mock_client.return_value.__enter__.return_value.post.assert_not_called()

    def test_batching_split_preserves_order(self):
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=2, base_url="https://x/v1", max_retries=1, batch_size=2
        )

        def post(*_args, **kwargs):
            inputs = kwargs["json"]["input"]
            return _oai_response([[0.0, 0.0] for _ in inputs])

        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.openai_compat.time.sleep"):
            mock_client.return_value.__enter__.return_value.post.side_effect = post
            texts = [f"t{i}" for i in range(5)]
            result = embedder.embed(texts)

        posts = mock_client.return_value.__enter__.return_value.post.call_args_list
        assert len(posts) == 3  # 2 + 2 + 1
        assert posts[0].kwargs["json"]["input"] == ["t0", "t1"]
        assert posts[1].kwargs["json"]["input"] == ["t2", "t3"]
        assert posts[2].kwargs["json"]["input"] == ["t4"]
        assert len(result) == 5

    def test_retries_on_429_then_succeeds(self):
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=2, base_url="https://x/v1", max_retries=3
        )
        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.openai_compat.time.sleep"):
            post = mock_client.return_value.__enter__.return_value.post
            post.side_effect = [_status_error(429), _oai_response([[0.1, 0.2]])]
            result = embedder.embed(["x"])
        assert result == [[0.1, 0.2]]
        assert post.call_count == 2

    def test_retries_on_500_then_succeeds(self):
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=2, base_url="https://x/v1", max_retries=3
        )
        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.openai_compat.time.sleep"):
            post = mock_client.return_value.__enter__.return_value.post
            post.side_effect = [_status_error(500), _oai_response([[0.1, 0.2]])]
            result = embedder.embed(["x"])
        assert result == [[0.1, 0.2]]
        assert post.call_count == 2

    def test_retries_on_timeout_then_succeeds(self):
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=2, base_url="https://x/v1", max_retries=3
        )
        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.openai_compat.time.sleep"):
            post = mock_client.return_value.__enter__.return_value.post
            post.side_effect = [httpx.TimeoutException("slow"), _oai_response([[0.1, 0.2]])]
            result = embedder.embed(["x"])
        assert result == [[0.1, 0.2]]
        assert post.call_count == 2

    def test_4xx_client_error_not_retried(self):
        # A non-400 4xx (e.g. 401 auth) fast-fails with no retry. 400 has its own
        # `dimensions`-fallback path covered separately below.
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=2, base_url="https://x/v1", max_retries=3
        )
        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.openai_compat.time.sleep"):
            post = mock_client.return_value.__enter__.return_value.post
            post.side_effect = [_status_error(401), _oai_response([[0.1, 0.2]])]
            with pytest.raises(EmbeddingError, match="HTTP 401"):
                embedder.embed(["x"])
        assert post.call_count == 1  # fast-fail, no retry

    def test_dimensions_400_drops_and_retries(self, caplog):
        # Fixed-dimension endpoint (e.g. SiliconFlow bge-m3) rejects the
        # `dimensions` parameter with HTTP 400; auto-drop it and retry.
        embedder = OpenAICompatEmbedder(
            model="BAAI/bge-m3", dimensions=1024, base_url="https://api.siliconflow.cn/v1", max_retries=3
        )
        body = '{"code":20015,"message":"The parameter is invalid."}'
        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.openai_compat.time.sleep"):
            post = mock_client.return_value.__enter__.return_value.post
            post.side_effect = [
                _status_error(400, text=body),
                _oai_response([[0.0] * 1024]),
            ]
            with caplog.at_level("WARNING"):
                result = embedder.embed(["x"])

            assert result == [[0.0] * 1024]
            assert embedder._send_dimensions is False
            assert post.call_count == 2
            # First request sent `dimensions`; the retry omitted it.
            assert "dimensions" in post.call_args_list[0].kwargs["json"]
            assert "dimensions" not in post.call_args_list[1].kwargs["json"]
            assert any("dimensions" in rec.message for rec in caplog.records)

            # Subsequent calls also omit `dimensions` (latch persists, no 400 retry).
            post.reset_mock()
            post.side_effect = None
            post.return_value = _oai_response([[0.0] * 1024])
            embedder.embed(["y"])
            assert post.call_count == 1  # no extra 400 round-trip
            assert "dimensions" not in post.call_args.kwargs["json"]

    def test_dimensions_400_persists_raises(self):
        # After the latch is off (both attempts 400) the second 400 is a real
        # client error -- raise EmbeddingError, no infinite loop.
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=1024, base_url="https://x/v1", max_retries=3
        )
        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.openai_compat.time.sleep"):
            post = mock_client.return_value.__enter__.return_value.post
            post.side_effect = [_status_error(400), _status_error(400), _oai_response([[0.0] * 1024])]
            with pytest.raises(EmbeddingError, match="HTTP 400"):
                embedder.embed(["x"])
        assert embedder._send_dimensions is False
        assert post.call_count == 2  # first 400 -> drop+retry -> second 400 fast-fails

    def test_c1_still_fires_after_dims_dropped(self):
        # C1 dimension assertion still applies after the `dimensions` fallback:
        # a wrong-length vector still raises the dimension-specific error naming
        # both numbers, even though the request no longer sends `dimensions`.
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=1024, base_url="https://x/v1", max_retries=3
        )
        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.openai_compat.time.sleep"):
            post = mock_client.return_value.__enter__.return_value.post
            post.side_effect = [_status_error(400), _oai_response([[0.0] * 512])]
            with pytest.raises(EmbeddingError) as exc:
                embedder.embed(["x"])
        message = str(exc.value)
        assert "1024" in message and "512" in message
        assert "failed after" not in message  # routed through C1, not masked

    def test_raises_after_retries_exhausted(self):
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=2, base_url="https://x/v1", max_retries=2
        )
        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.openai_compat.time.sleep"):
            post = mock_client.return_value.__enter__.return_value.post
            post.side_effect = [_status_error(500), _status_error(500)]
            with pytest.raises(EmbeddingError, match="failed after 2 attempts"):
                embedder.embed(["x"])
        assert post.call_count == 2

    def test_connection_refused_fast_fails(self):
        embedder = OpenAICompatEmbedder(
            model="nomic-embed-text", dimensions=2, base_url="http://localhost:11434/v1", max_retries=3
        )
        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.openai_compat.time.sleep"):
            post = mock_client.return_value.__enter__.return_value.post
            post.side_effect = httpx.ConnectError("connection refused")
            with pytest.raises(EmbeddingError) as exc:
                embedder.embed(["x"])
        assert post.call_count == 1  # M2: no retry on connection refused
        assert "is the server running" in str(exc.value)
        assert "nomic-embed-text" in str(exc.value)

    def test_truncation_before_request(self):
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=2, base_url="https://x/v1", max_retries=1, max_input_chars=10
        )
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = _oai_response([[0.1, 0.2]])
            embedder.embed(["a" * 100])
        posted = mock_client.return_value.__enter__.return_value.post.call_args.kwargs["json"]["input"][0]
        assert len(posted) <= 10

    def test_dimension_mismatch_multi_text_raises(self):
        # C1 path (a): a multi-text batch returning the wrong dimension raises,
        # naming BOTH the expected and actual counts.
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=1024, base_url="https://x/v1", max_retries=1
        )
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = _oai_response(
                [[0.0] * 512, [0.0] * 512]
            )
            with pytest.raises(EmbeddingError) as exc:
                embedder.embed(["a", "b"])
        message = str(exc.value)
        assert "1024" in message and "512" in message

    def test_dimension_mismatch_single_text_fallback_raises(self):
        # C1 path (b) / G3: when a multi-text batch falls back to per-text
        # requests, a wrong dimension STILL raises the dimension-specific
        # message -- NOT a generic "batch failed".
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=1024, base_url="https://x/v1", max_retries=1, batch_size=2
        )

        def post(*_args, **kwargs):
            inputs = kwargs["json"]["input"]
            if len(inputs) > 1:
                return _status_error(500)  # force fallback to single-text
            return _oai_response([[0.0] * 512])  # single-text returns wrong dim

        with patch("httpx.Client") as mock_client, patch("zotpilot.embeddings.openai_compat.time.sleep"):
            mock_client.return_value.__enter__.return_value.post.side_effect = post
            with pytest.raises(EmbeddingError) as exc:
                embedder.embed(["a", "b"])
        message = str(exc.value)
        assert "1024" in message and "512" in message
        assert "failed after" not in message  # not masked as a generic batch error

    def test_base64_response_rejected(self):
        # M4: endpoint ignored encoding_format:"float" and returned base64.
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=2, base_url="https://x/v1", max_retries=1
        )
        resp = MagicMock()
        resp.json.return_value = {"data": [{"index": 0, "embedding": "AAECAwQ="}]}
        resp.raise_for_status.return_value = None
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = resp
            with pytest.raises(EmbeddingError, match="base64"):
                embedder.embed(["x"])

    def test_non_list_embedding_rejected(self):
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=2, base_url="https://x/v1", max_retries=1
        )
        resp = MagicMock()
        resp.json.return_value = {"data": [{"index": 0, "embedding": {"oops": 1}}]}
        resp.raise_for_status.return_value = None
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = resp
            with pytest.raises(EmbeddingError, match="malformed"):
                embedder.embed(["x"])

    def test_embed_query_uses_first_vector(self):
        embedder = OpenAICompatEmbedder(model="m", dimensions=2, base_url="https://x/v1", max_retries=1)
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = _oai_response([[0.3, 0.4]])
            assert embedder.embed_query("query") == [0.3, 0.4]


class TestOpenAICompatBaseUrl:
    def test_rejects_non_http_scheme(self):
        with pytest.raises(EmbeddingError, match="scheme"):
            OpenAICompatEmbedder(model="m", dimensions=2, base_url="ftp://x/v1")

    def test_rejects_embedded_userinfo(self):
        with pytest.raises(EmbeddingError, match="credentials"):
            OpenAICompatEmbedder(model="m", dimensions=2, base_url="http://user:pass@host/v1")

    def test_requires_base_url(self):
        with pytest.raises(EmbeddingError, match="base_url"):
            OpenAICompatEmbedder(model="m", dimensions=2, base_url=None)

    def test_requires_model(self):
        with pytest.raises(EmbeddingError, match="embedding_model"):
            OpenAICompatEmbedder(model="", dimensions=2, base_url="https://x/v1")

    def test_accepts_local_http(self):
        embedder = OpenAICompatEmbedder(model="m", dimensions=2, base_url="http://localhost:11434/v1")
        assert embedder.base_url == "http://localhost:11434/v1"

    def test_accepts_https(self):
        embedder = OpenAICompatEmbedder(model="m", dimensions=2, base_url="https://api.example.com/v1")
        assert embedder.base_url == "https://api.example.com/v1"

    def test_accepts_glm_non_v1_without_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="zotpilot.embeddings.openai_compat"):
            embedder = OpenAICompatEmbedder(
                model="embedding-3", dimensions=2048, base_url="https://open.bigmodel.cn/api/paas/v4"
            )
        assert embedder.base_url == "https://open.bigmodel.cn/api/paas/v4"
        assert caplog.records == []

    def test_strips_trailing_embeddings_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="zotpilot.embeddings.openai_compat"):
            embedder = OpenAICompatEmbedder(model="m", dimensions=2, base_url="https://x/v1/embeddings")
        assert embedder.base_url == "https://x/v1"
        assert any("embeddings" in r.getMessage() for r in caplog.records)

    def test_plaintext_http_nonlocal_key_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="zotpilot.embeddings.openai_compat"):
            OpenAICompatEmbedder(
                model="m", dimensions=2, base_url="http://remote.example.com/v1", api_key="secret"
            )
        assert any("plaintext" in r.getMessage().lower() for r in caplog.records)

    def test_no_key_omits_authorization_header(self):
        embedder = OpenAICompatEmbedder(model="m", dimensions=2, base_url="http://localhost:11434/v1")
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = _oai_response([[0.1, 0.2]])
            embedder.embed(["x"])
        headers = mock_client.return_value.__enter__.return_value.post.call_args.kwargs["headers"]
        assert "Authorization" not in headers

    def test_with_key_sends_bearer_header(self):
        embedder = OpenAICompatEmbedder(
            model="m", dimensions=2, base_url="https://x/v1", api_key="sk-test"
        )
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = _oai_response([[0.1, 0.2]])
            embedder.embed(["x"])
        headers = mock_client.return_value.__enter__.return_value.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-test"


def _oai_config(**overrides):
    """A fully-specified config-like object for the openai-compatible factory.

    NOT a bare MagicMock: every attribute the factory reads is explicit, so
    _resolve_secret sees real values (None vs str) instead of truthy mocks.
    """
    base = dict(
        embedding_provider="openai-compatible",
        embedding_model="m",
        embedding_dimensions=1024,
        embedding_api_key=None,
        embedding_base_url=None,
        embedding_timeout=120.0,
        embedding_max_retries=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCreateEmbedderOpenAICompat:
    def test_returns_openai_compat_embedder_with_config_dims(self):
        cfg = _oai_config(embedding_base_url="https://x/v1", embedding_dimensions=1536)
        embedder = create_embedder(cfg)
        assert isinstance(embedder, OpenAICompatEmbedder)
        assert embedder.dimensions == 1536
        assert embedder.base_url == "https://x/v1"

    def test_raises_on_missing_base_url(self, monkeypatch):
        monkeypatch.delenv("ZOTPILOT_EMBEDDING_BASE_URL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        cfg = _oai_config()  # base_url None, no env
        with pytest.raises(EmbeddingError, match="embedding_base_url"):
            create_embedder(cfg)

    def test_base_url_and_key_flow_through_resolve_secret(self, monkeypatch):
        monkeypatch.setenv("ZOTPILOT_EMBEDDING_BASE_URL", "https://env.example/v1")
        monkeypatch.setenv("ZOTPILOT_EMBEDDING_API_KEY", "env-secret-key")
        cfg = _oai_config()  # both None in config -> must resolve from env
        embedder = create_embedder(cfg)
        assert embedder.base_url == "https://env.example/v1"
        assert embedder.api_key == "env-secret-key"

    def test_embed_documents_delegates_to_embed(self):
        embedder = OpenAICompatEmbedder(model="m", dimensions=2, base_url="https://x/v1")
        with patch.object(embedder, "embed", return_value=[[1.0, 2.0]]) as mock_embed:
            out = embedder.embed_documents(["a"])
        mock_embed.assert_called_once_with(["a"], task_type="RETRIEVAL_DOCUMENT")
        assert out == [[1.0, 2.0]]
