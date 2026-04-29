"""Alibaba Cloud Bailian (DashScope) embedding provider."""
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# China mainland endpoint (default, OpenAI-compatible)
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# International endpoint (Singapore)
INTL_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DEFAULT_NATIVE_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
INTL_NATIVE_URL = "https://dashscope-intl.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"

QUERY_INSTRUCT = "Given a research paper query, retrieve relevant research papers and evidence passages"
SAFE_INPUT_CHARS = 6000
SAFE_BATCH_SIZE = 5


def _truncate_text(text: str, max_chars: int = SAFE_INPUT_CHARS) -> str:
    """Keep DashScope inputs comfortably below the 8192-token per-text limit."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0].strip() or text[:max_chars].strip()


class DashScopeEmbedder:
    """
    Alibaba Cloud Bailian embedding wrapper.

    Supports text-embedding-v3 and text-embedding-v4 (Qwen3-Embedding).
    The default compatible endpoint preserves DashScope's OpenAI-compatible
    behavior. The native endpoint can be selected for asymmetric retrieval via
    DashScope's text_type parameter.

    Default model: text-embedding-v4
    Output dimensions: configurable (v3: 64–1024, v4: 64–2048)
    Max input: 8192 tokens per text
    Batch size: up to 10 texts (v3/v4)
    Pricing: ¥0.0005 / 1k tokens (~$0.07 / million tokens)
    """

    def __init__(
        self,
        model: str = "text-embedding-v4",
        dimensions: int = 1024,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        endpoint: str = "compatible",
        native_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
        batch_size: int = SAFE_BATCH_SIZE,
        max_input_chars: int = SAFE_INPUT_CHARS,
    ):
        import os
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise ValueError(
                "DASHSCOPE_API_KEY not set. Get one at https://bailian.console.aliyun.com/"
            )
        self.model = model
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        if endpoint not in ("compatible", "native"):
            raise ValueError("DashScope embedding endpoint must be 'compatible' or 'native'")
        self.endpoint = endpoint
        self.native_url = native_url or (
            INTL_NATIVE_URL if "dashscope-intl" in self.base_url else DEFAULT_NATIVE_URL
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self.batch_size = min(max(1, batch_size), 10)
        self.max_input_chars = max_input_chars

    def _embed_batch(
        self,
        batch: list[str],
        batch_num: int,
        total_batches: int,
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> list[list[float]]:
        """Embed a single batch with retry."""
        total_chars = sum(len(t) for t in batch)
        logger.debug(
            f"Embedding batch {batch_num}/{total_batches}: "
            f"{len(batch)} texts, {total_chars} chars total"
        )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url, payload = self._build_payload(batch, task_type)

        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(url, headers=headers, json=payload)
                    response.raise_for_status()
                    data = response.json()

                result = self._parse_embeddings(data)
                logger.debug(
                    f"Batch {batch_num}/{total_batches} succeeded "
                    f"(attempt {attempt}), got {len(result)} embeddings"
                )
                return result

            except httpx.TimeoutException:
                last_error = f"timeout after {self.timeout}s"
                logger.warning(
                    f"Batch {batch_num}/{total_batches} timed out after "
                    f"{self.timeout}s (attempt {attempt}/{self.max_retries})"
                )
            except httpx.HTTPStatusError as e:
                last_error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
                logger.warning(
                    f"Batch {batch_num}/{total_batches} HTTP {e.response.status_code} "
                    f"(attempt {attempt}/{self.max_retries}): {e.response.text[:200]}"
                )
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"Batch {batch_num}/{total_batches} failed "
                    f"(attempt {attempt}/{self.max_retries}): {type(e).__name__}: {e}"
                )

            if attempt < self.max_retries:
                backoff = 2 ** attempt
                logger.info(f"Retrying in {backoff}s...")
                time.sleep(backoff)

        from .gemini import EmbeddingError
        raise EmbeddingError(
            f"Batch {batch_num}/{total_batches} failed after "
            f"{self.max_retries} attempts ({len(batch)} texts, {total_chars} chars)"
            + (f": {last_error}" if last_error else "")
        )

    def _build_payload(self, batch: list[str], task_type: str) -> tuple[str, dict[str, Any]]:
        if self.endpoint == "compatible":
            return (
                f"{self.base_url}/embeddings",
                {
                    "model": self.model,
                    "input": batch,
                    "dimensions": self.dimensions,
                    "encoding_format": "float",
                },
            )

        text_type = "query" if task_type == "RETRIEVAL_QUERY" else "document"
        parameters: dict[str, Any] = {
            "dimension": self.dimensions,
            "text_type": text_type,
        }
        if text_type == "query":
            parameters["instruct"] = QUERY_INSTRUCT
        return (
            self.native_url,
            {
                "model": self.model,
                "input": {"texts": batch},
                "parameters": parameters,
            },
        )

    def _parse_embeddings(self, data: dict[str, Any]) -> list[list[float]]:
        if self.endpoint == "compatible":
            embeddings = sorted(data["data"], key=lambda x: x["index"])
        else:
            embeddings = sorted(data["output"]["embeddings"], key=lambda x: x.get("text_index", x.get("index", 0)))
        return [e["embedding"] for e in embeddings]

    def embed(
        self,
        texts: list[str],
        task_type: str = "RETRIEVAL_DOCUMENT",
    ) -> list[list[float]]:
        """
        Embed a batch of texts.

        Args:
            texts: List of texts to embed
            task_type: In native mode, RETRIEVAL_DOCUMENT uses
                       text_type=document and RETRIEVAL_QUERY uses
                       text_type=query + instruct. Compatible mode ignores it.

        Returns:
            List of embedding vectors
        """
        if not texts:
            return []

        texts = [_truncate_text(text, self.max_input_chars) for text in texts]
        results = []
        batch_size = self.batch_size
        total_batches = (len(texts) + batch_size - 1) // batch_size

        logger.debug(
            f"Embedding {len(texts)} texts in {total_batches} batch(es), "
            f"model={self.model}"
        )

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch_num = i // batch_size + 1
            try:
                batch_results = self._embed_batch(batch, batch_num, total_batches, task_type=task_type)
            except Exception:
                if len(batch) == 1 or task_type == "RETRIEVAL_QUERY":
                    raise
                logger.warning(
                    "Batch %s/%s failed; retrying as single-text requests",
                    batch_num,
                    total_batches,
                )
                batch_results = []
                for j, text in enumerate(batch, 1):
                    batch_results.extend(
                        self._embed_batch(
                            [text],
                            batch_num=(batch_num * 100) + j,
                            total_batches=total_batches * 100,
                            task_type=task_type,
                        )
                    )
            results.extend(batch_results)
            if batch_num < total_batches:
                time.sleep(0.5)

        return results

    def embed_query(self, query: str) -> list[float]:
        """Embed a search query."""
        return self.embed([query], task_type="RETRIEVAL_QUERY")[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed documents for indexing."""
        return self.embed(texts, task_type="RETRIEVAL_DOCUMENT")
