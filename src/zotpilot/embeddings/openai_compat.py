"""Generic OpenAI-compatible embedding provider.

A single, vendor-neutral embedder that talks to any OpenAI-compatible
``/embeddings`` endpoint (SiliconFlow, Zhipu/GLM, Ollama, vLLM, custom...).
Vendor reachability is a ``base_url`` + ``model`` choice, not new code.

Modeled on the DashScope *compatible* path. The output ``dimensions`` is an
explicit user input that is NEVER inferred from the server response -- it flows
from config into ``self.dimensions`` and is asserted against the first returned
vector length (C1). This structurally prevents the PR #16 hardcoded-dimensions
bug class.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any

import httpx

from .gemini import EmbeddingError

logger = logging.getLogger(__name__)

# Conservative generic defaults. GLM ``embedding-3`` caps a single input at 3072
# tokens (far below others' 8192+); for CJK text 1 char can be ~1 token, so keep
# the char limit safely below 3072 to avoid overflowing GLM.
SAFE_INPUT_CHARS = 2048
# Conservative batch size: verified per-vendor max input counts are
# SiliconFlow 32, GLM 64, OpenAI 2048, Ollama undocumented -- 16 is safe
# everywhere. Do NOT inherit DashScope's hard cap of 10.
DEFAULT_BATCH_SIZE = 16
MAX_BATCH_SIZE = 2048

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _truncate_text(text: str, max_chars: int = SAFE_INPUT_CHARS) -> str:
    """Keep inputs comfortably below the per-text token limit.

    Copied from ``dashscope.py`` as a module-level function to avoid
    cross-provider coupling.
    """
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0].strip() or text[:max_chars].strip()


class _EmbeddingContractError(EmbeddingError):
    """Embedding-contract violation (dimension mismatch / non-float shape).

    A dedicated subclass so the single-text fallback in :meth:`embed` can
    re-raise it immediately rather than masking it as a generic "batch failed"
    error (G3). It IS an :class:`EmbeddingError`, so all existing
    ``except EmbeddingError`` callers still catch it.
    """


class OpenAICompatEmbedder:
    """Embedder for any OpenAI-compatible ``/embeddings`` endpoint.

    POSTs to ``{base_url}/embeddings`` with
    ``{model, input, dimensions, encoding_format: "float"}``. The ``base_url``
    is the vendor's OpenAI-compatible root -- usually ends in ``/v1`` but not
    always (Zhipu/GLM uses ``/api/paas/v4``); we do not assume a ``/v1`` suffix.

    The configured ``dimensions`` is required and is asserted against the first
    response vector (C1); it is never inferred from the server.
    """

    def __init__(
        self,
        model: str,
        dimensions: int,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 3,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_input_chars: int = SAFE_INPUT_CHARS,
    ):
        if not model:
            raise EmbeddingError(
                "embedding_model is required for the openai-compatible provider."
            )
        self.api_key = api_key or None
        self.base_url = self._normalize_and_validate_base_url(base_url, self.api_key)
        self.model = model
        self.dimensions = dimensions
        self.timeout = timeout
        self.max_retries = max_retries
        self.batch_size = min(max(1, batch_size), MAX_BATCH_SIZE)
        self.max_input_chars = max_input_chars
        # One-way latch: start by sending `dimensions`; if a fixed-dimension
        # endpoint rejects it with HTTP 400, drop it for this and all later
        # requests on this instance (see _post_with_retry).
        self._send_dimensions: bool = True

    @staticmethod
    def _normalize_and_validate_base_url(raw: str | None, api_key: str | None) -> str:
        """Normalize (M3) and security-validate (H1/H2) the base URL."""
        url = (raw or "").strip().rstrip("/")
        if not url:
            raise EmbeddingError(
                "embedding_base_url is not set for the openai-compatible provider. "
                "Set it via config, ZOTPILOT_EMBEDDING_BASE_URL, or OPENAI_BASE_URL "
                "(e.g. http://localhost:11434/v1 for Ollama)."
            )

        # M3: strip a mistakenly-pasted trailing ``/embeddings`` (copy-paste error).
        if url.endswith("/embeddings"):
            url = url[: -len("/embeddings")].rstrip("/")
            logger.warning(
                "Stripped trailing '/embeddings' from embedding_base_url; "
                "requests will POST to %s/embeddings",
                url,
            )

        parsed = urllib.parse.urlsplit(url)
        # H1: scheme must be http/https.
        if parsed.scheme not in ("http", "https"):
            raise EmbeddingError(
                f"Invalid embedding_base_url scheme {parsed.scheme!r}: must be http or https."
            )
        # H1: reject embedded userinfo so credentials cannot leak into logs.
        if "@" in parsed.netloc:
            raise EmbeddingError(
                "embedding_base_url must not contain embedded credentials "
                "(user:pass@host). Pass the API key via embedding_api_key instead."
            )

        # H2: warn when sending a key over plaintext http to a non-local host.
        host = parsed.hostname or ""
        is_local = host in _LOCAL_HOSTS or host.endswith(".local")
        if parsed.scheme == "http" and not is_local and api_key:
            logger.warning(
                "Sending an API key over plaintext http:// to non-local host %r. "
                "Use https:// to avoid leaking the key in transit.",
                host,
            )
        return url

    def _post_with_retry(
        self, batch: list[str], batch_num: int, total_batches: int
    ) -> dict[str, Any]:
        """POST one batch with retry. Retries ONLY 429/5xx/timeout (M2).

        Connection-refused fast-fails on the first attempt (no retry); other
        4xx client errors fast-fail too. The Authorization header is never
        logged and response bodies are truncated to 300 chars (H2).
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # Ollama needs no key: when none resolves, OMIT the header entirely.
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = f"{self.base_url}/embeddings"

        def _build_payload() -> dict[str, Any]:
            payload: dict[str, Any] = {
                "model": self.model,
                "input": batch,
                "encoding_format": "float",
            }
            # Only send `dimensions` while the latch is still set. Fixed-dim
            # (non-matryoshka) endpoints reject it with HTTP 400; see below.
            if self._send_dimensions:
                payload["dimensions"] = self.dimensions
            return payload

        last_error = ""
        attempt = 0
        latched_this_call = False
        while attempt < self.max_retries:
            attempt += 1
            sent_dimensions = self._send_dimensions
            dims_fallback = False
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(url, headers=headers, json=_build_payload())
                    response.raise_for_status()
                    data: dict[str, Any] = response.json()
                return data
            except httpx.ConnectError as e:
                # M2: do not retry connection-refused -- fast-fail actionably.
                raise EmbeddingError(
                    f"Cannot reach {self.base_url} -- is the server running? "
                    f"For Ollama, try: `ollama serve` then `ollama pull {self.model}`."
                ) from e
            except httpx.TimeoutException:
                last_error = f"timeout after {self.timeout}s"
                logger.warning(
                    "Batch %s/%s timed out after %ss (attempt %s/%s)",
                    batch_num, total_batches, self.timeout, attempt, self.max_retries,
                )
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                body = e.response.text[:300]
                if status == 400 and sent_dimensions:
                    # Fixed-dimension (non-matryoshka) endpoint rejecting the
                    # `dimensions` parameter (e.g. SiliconFlow bge-m3). Latch it
                    # off for this and all later requests, then immediately retry
                    # the SAME request once without `dimensions`. This one-time
                    # fallback does not consume a retry slot and does not sleep;
                    # a subsequent 400 (latch now off) is a real client error and
                    # fast-fails as EmbeddingError on the next loop pass.
                    logger.warning(
                        "Endpoint rejected the `dimensions` parameter (HTTP 400); "
                        "retrying without it -- the model is likely fixed-dimension. "
                        "base_url=%s",
                        self.base_url,
                    )
                    self._send_dimensions = False
                    latched_this_call = True
                    dims_fallback = True
                elif status != 429 and status < 500:
                    # Non-transient client error -- fast-fail, no retry.
                    if latched_this_call:
                        # The no-`dimensions` retry ALSO 400'd: `dimensions` was
                        # not the culprit. Restore the latch so an unrelated 400
                        # does not poison `dimensions` for the rest of the
                        # session (matryoshka endpoints keep honoring it).
                        self._send_dimensions = True
                    raise EmbeddingError(
                        f"HTTP {status} from {url}: {body}"
                    ) from e
                else:
                    last_error = f"HTTP {status}: {body}"
                    logger.warning(
                        "Batch %s/%s HTTP %s (attempt %s/%s)",
                        batch_num, total_batches, status, attempt, self.max_retries,
                    )

            if dims_fallback:
                # Re-run this attempt immediately without the `dimensions` key;
                # do not count it as a retry and do not back off.
                attempt -= 1
                continue
            if attempt < self.max_retries:
                time.sleep(2 ** attempt)

        raise EmbeddingError(
            f"Batch {batch_num}/{total_batches} failed after {self.max_retries} attempts"
            + (f": {last_error}" if last_error else "")
        )

    def _parse_embeddings(self, data: dict[str, Any]) -> list[list[float]]:
        """Parse + shape-validate the response (M4)."""
        items = sorted(data["data"], key=lambda x: x["index"])
        result: list[list[float]] = []
        for item in items:
            emb = item["embedding"]
            if isinstance(emb, str):
                # M4: endpoint ignored encoding_format:"float" and returned base64.
                raise _EmbeddingContractError(
                    "Endpoint returned base64 embeddings; ZotPilot requires float "
                    "format. Check that the server supports `encoding_format: float`."
                )
            if not isinstance(emb, list) or not all(
                isinstance(v, (int, float)) for v in emb
            ):
                raise _EmbeddingContractError(
                    "Endpoint returned a malformed embedding (expected a list of "
                    "floats). The server response does not match the OpenAI format."
                )
            result.append([float(v) for v in emb])
        return result

    def _assert_dimensions(self, result: list[list[float]]) -> None:
        """Runtime dimension assertion (C1) -- un-swallowable via the subclass."""
        for vec in result:
            if len(vec) != self.dimensions:
                raise _EmbeddingContractError(
                    f"Server returned {len(vec)}-dimensional vectors but config "
                    f"specifies embedding_dimensions={self.dimensions}. The endpoint "
                    f"may not support the `dimensions` parameter (non-matryoshka "
                    f"model). Set embedding_dimensions to {len(vec)} to match the "
                    f"server's native output."
                )
            break  # first vector is sufficient

    def _embed_batch(
        self, batch: list[str], batch_num: int, total_batches: int
    ) -> list[list[float]]:
        """Embed one batch. The C1/M4 checks run OUTSIDE the retry loop (G3)."""
        data = self._post_with_retry(batch, batch_num, total_batches)
        result = self._parse_embeddings(data)
        self._assert_dimensions(result)
        return result

    def embed(
        self, texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> list[list[float]]:
        """Embed texts. ``task_type`` is accepted but ignored (compatible mode)."""
        if not texts:
            return []

        texts = [_truncate_text(text, self.max_input_chars) for text in texts]
        results: list[list[float]] = []
        batch_size = self.batch_size
        total_batches = (len(texts) + batch_size - 1) // batch_size

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch_num = i // batch_size + 1
            try:
                batch_results = self._embed_batch(batch, batch_num, total_batches)
            except _EmbeddingContractError:
                # Dimension/shape errors are never masked or retried per-text.
                raise
            except Exception:
                if len(batch) == 1:
                    raise
                logger.warning(
                    "Batch %s/%s failed; retrying as single-text requests",
                    batch_num, total_batches,
                )
                batch_results = []
                for j, text in enumerate(batch, 1):
                    batch_results.extend(
                        self._embed_batch([text], (batch_num * 100) + j, total_batches * 100)
                    )
            results.extend(batch_results)

        return results

    def embed_query(self, query: str) -> list[float]:
        """Embed a search query."""
        return self.embed([query], task_type="RETRIEVAL_QUERY")[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed documents for indexing."""
        return self.embed(texts, task_type="RETRIEVAL_DOCUMENT")
