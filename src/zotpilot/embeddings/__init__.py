"""Embedding providers for ZotPilot."""
from ..providers import EMBEDDING_PROVIDERS, _resolve_secret
from .base import EmbedderProtocol, EmbeddingError, RateLimitError
from .dashscope import DashScopeEmbedder
from .gemini import GeminiEmbedder
from .local import LocalEmbedder
from .openai_compat import OpenAICompatEmbedder


def create_embedder(config):
    """Create embedder based on config.embedding_provider."""
    import logging
    logger = logging.getLogger(__name__)

    if config.embedding_provider == "local":
        logger.info("Using local embeddings (all-MiniLM-L6-v2, 384 dimensions)")
        return LocalEmbedder()
    elif config.embedding_provider == "gemini":
        logger.info(f"Using Gemini embeddings ({config.embedding_model}, {config.embedding_dimensions} dimensions)")
        return GeminiEmbedder(
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
            api_key=config.gemini_api_key,
            timeout=config.embedding_timeout,
            max_retries=config.embedding_max_retries,
        )
    elif config.embedding_provider == "dashscope":
        dashscope_endpoint = getattr(config, "dashscope_embedding_endpoint", "compatible")
        if not isinstance(dashscope_endpoint, str):
            dashscope_endpoint = "compatible"
        logger.info(
            f"Using DashScope embeddings ({config.embedding_model}, "
            f"{config.embedding_dimensions} dimensions, endpoint={dashscope_endpoint})"
        )
        return DashScopeEmbedder(
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
            api_key=config.dashscope_api_key,
            endpoint=dashscope_endpoint,
            timeout=config.embedding_timeout,
            max_retries=config.embedding_max_retries,
        )
    elif config.embedding_provider == "openai-compatible":
        api_key = _resolve_secret(
            getattr(config, "embedding_api_key", None),
            "ZOTPILOT_EMBEDDING_API_KEY",
            "OPENAI_API_KEY",
        )
        base_url = _resolve_secret(
            getattr(config, "embedding_base_url", None),
            "ZOTPILOT_EMBEDDING_BASE_URL",
            "OPENAI_BASE_URL",
        )
        if not base_url:
            raise EmbeddingError(
                "embedding_base_url is not set for embedding_provider='openai-compatible'. "
                "Set it via config, ZOTPILOT_EMBEDDING_BASE_URL, or OPENAI_BASE_URL "
                "(e.g. http://localhost:11434/v1 for Ollama)."
            )
        logger.info(
            f"Using OpenAI-compatible embeddings ({config.embedding_model}, "
            f"{config.embedding_dimensions} dimensions, base_url={base_url})"
        )
        return OpenAICompatEmbedder(
            model=config.embedding_model,
            dimensions=config.embedding_dimensions,
            api_key=api_key,
            base_url=base_url,
            timeout=config.embedding_timeout,
            max_retries=config.embedding_max_retries,
        )
    elif config.embedding_provider == "none":
        logger.info("No-RAG mode: embedding disabled, semantic search unavailable")
        return None
    else:
        valid = ", ".join(repr(p) for p in EMBEDDING_PROVIDERS)
        raise ValueError(
            f"Invalid embedding_provider: {config.embedding_provider}. Must be one of: {valid}"
        )


__all__ = ["create_embedder", "GeminiEmbedder", "DashScopeEmbedder", "LocalEmbedder", "OpenAICompatEmbedder", "EmbeddingError", "RateLimitError", "EmbedderProtocol"]  # noqa: E501
