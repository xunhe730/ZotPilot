"""Gemini embedding provider."""
import logging
import time
import concurrent.futures

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Raised when embedding fails after retries."""


class GeminiEmbedder:
    """
    Gemini embedding wrapper using gemini-embedding-001.

    This model is #1 on MTEB Multilingual leaderboard.
    Uses asymmetric embeddings: different task types for docs vs queries.

    Output dimensions: configurable (768 default, up to 3072)
    Max input: 2048 tokens per text
    Batch size: up to 100 texts
    """

    def __init__(
        self,
        model: str = "gemini-embedding-001",
        dimensions: int = 768,
        api_key: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
    ):
        from google import genai
        # Uses GEMINI_API_KEY env var if api_key not provided
        if api_key:
            self.client = genai.Client(api_key=api_key)
        else:
            self.client = genai.Client()
        self.model = model
        self.dimensions = dimensions
        self.timeout = timeout
        self.max_retries = max_retries

    def _embed_batch_with_timeout(
        self, batch: list[str], task_type: str, batch_num: int, total_batches: int
    ) -> list[list[float]]:
        """Embed a single batch with timeout, retry, and logging."""
        from google.genai import types
        total_chars = sum(len(t) for t in batch)
        logger.debug(
            f"Embedding batch {batch_num}/{total_batches}: "
            f"{len(batch)} texts, {total_chars} chars total"
        )

        for attempt in range(1, self.max_retries + 1):
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        self.client.models.embed_content,
                        model=self.model,
                        contents=batch,
                        config=types.EmbedContentConfig(
                            task_type=task_type,
                            output_dimensionality=self.dimensions,
                        ),
                    )
                    response = future.result(timeout=self.timeout)

                logger.debug(
                    f"Batch {batch_num}/{total_batches} succeeded "
                    f"(attempt {attempt}), got {len(response.embeddings)} embeddings"
                )
                return [e.values for e in response.embeddings]

            except concurrent.futures.TimeoutError:
                logger.warning(
                    f"Batch {batch_num}/{total_batches} timed out after "
                    f"{self.timeout}s (attempt {attempt}/{self.max_retries})"
                )
            except Exception as e:
                logger.warning(
                    f"Batch {batch_num}/{total_batches} failed "
                    f"(attempt {attempt}/{self.max_retries}): {type(e).__name__}: {e}"
                )

            if attempt < self.max_retries:
                backoff = 2 ** attempt
                logger.info(f"Retrying in {backoff}s...")
                time.sleep(backoff)

        raise EmbeddingError(
            f"Batch {batch_num}/{total_batches} failed after "
            f"{self.max_retries} attempts ({len(batch)} texts, {total_chars} chars)"
        )

    def embed(
        self,
        texts: list[str],
        task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> list[list[float]]:
        """
        Embed a batch of texts.

        Args:
            texts: List of texts to embed
            task_type: One of:
                - "RETRIEVAL_DOCUMENT": For indexing documents (default)
                - "RETRIEVAL_QUERY": For search queries
                - "SEMANTIC_SIMILARITY": For comparing texts
                - "CLASSIFICATION": For classification

        Returns:
            List of embedding vectors

        Raises:
            EmbeddingError: If embedding fails after retries
        """
        if not texts:
            return []

        results = []
        batch_size = 32  # Reduced from 100 to avoid Gemini rate limits
        total_batches = (len(texts) + batch_size - 1) // batch_size

        logger.debug(
            f"Embedding {len(texts)} texts in {total_batches} batch(es), "
            f"task_type={task_type}"
        )

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch_num = i // batch_size + 1
            batch_results = self._embed_batch_with_timeout(
                batch, task_type, batch_num, total_batches
            )
            results.extend(batch_results)
            if batch_num < total_batches:
                time.sleep(1.0)  # Avoid Gemini rate limiting between batches

        return results

    def embed_query(self, query: str) -> list[float]:
        """
        Embed a search query.

        Uses RETRIEVAL_QUERY task type for asymmetric retrieval.
        """
        return self.embed([query], task_type="RETRIEVAL_QUERY")[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """
        Embed documents for indexing.

        Uses RETRIEVAL_DOCUMENT task type.
        """
        return self.embed(texts, task_type="RETRIEVAL_DOCUMENT")
