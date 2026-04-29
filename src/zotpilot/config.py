"""Configuration management."""
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ANTHROPIC_DEFAULT_VISION_MODEL = "claude-haiku-4-5-20251001"
DASHSCOPE_DEFAULT_VISION_MODEL = "qwen3-vl-flash"


def _default_config_dir() -> Path:
    """Platform-aware config directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", "~/AppData/Roaming")).expanduser()
    else:
        base = Path("~/.config").expanduser()
    return base / "zotpilot"


def _default_data_dir() -> Path:
    """Platform-aware data directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", "~/AppData/Local")).expanduser()
    else:
        base = Path("~/.local/share").expanduser()
    return base / "zotpilot"


def _old_config_path() -> Path:
    """Legacy deep-zotero config path."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", "~/AppData/Roaming")).expanduser()
    else:
        base = Path("~/.config").expanduser()
    return base / "deep-zotero" / "config.json"


@dataclass
class Config:
    """Application configuration."""
    zotero_data_dir: Path
    chroma_db_path: Path
    embedding_model: str
    embedding_dimensions: int
    chunk_size: int
    chunk_overlap: int
    gemini_api_key: str | None
    dashscope_api_key: str | None
    # Embedding provider: "gemini", "dashscope", "local", or "none" (No-RAG mode)
    embedding_provider: str
    # DashScope embedding endpoint: "compatible" or "native"
    dashscope_embedding_endpoint: str
    # Embedding settings
    embedding_timeout: float
    embedding_max_retries: int
    # Reranking settings
    rerank_alpha: float
    rerank_section_weights: dict[str, float] | None
    rerank_journal_weights: dict[str, float] | None  # Use "unknown" for null quartile
    rerank_enabled: bool
    oversample_multiplier: int
    oversample_topic_factor: int  # Additional factor for search_topic
    stats_sample_limit: int
    # OCR settings (language passed through to pymupdf-layout)
    ocr_language: str
    # OpenAlex settings
    openalex_email: str | None  # Optional email for polite pool (10 req/sec vs 1 req/sec)
    # Vision extraction settings
    vision_enabled: bool
    vision_provider: str
    vision_model: str
    anthropic_api_key: str | None
    vision_max_tables_per_run: int | None
    vision_max_cost_usd: float | None
    # Long document filtering
    max_pages: int  # Maximum PDF pages to index (0 = no limit)
    preflight_enabled: bool
    # Zotero Web API (for write operations)
    zotero_api_key: str | None
    zotero_user_id: str | None
    zotero_library_type: str  # "user" or "group"
    # Semantic Scholar API key (optional, increases rate limit)
    semantic_scholar_api_key: str | None

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        """Load shared config from disk."""
        if path is not None:
            config_path = Path(path).expanduser()
        else:
            config_path = _default_config_dir() / "config.json"

            # Migration support: if new config doesn't exist but old one does, load from old
            if not config_path.exists():
                old_path = _old_config_path()
                if old_path.exists():
                    logger.info(
                        f"Migrating config from {old_path} to {config_path}. "
                        f"Please update your config path to {config_path}."
                    )
                    config_path = old_path

        data = {}
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)

        default_chroma = str(_default_data_dir() / "chroma")

        provider = data.get("embedding_provider", "gemini")
        # Provider-aware defaults for model and dimensions
        model_defaults = {
            "gemini": ("gemini-embedding-001", 768),
            "dashscope": ("text-embedding-v4", 1024),
            "local": ("all-MiniLM-L6-v2", 384),
            "none": ("none", 0),
        }
        default_model, default_dims = model_defaults.get(provider, ("gemini-embedding-001", 768))

        vision_provider = data.get("vision_provider", "anthropic")
        default_vision_model = (
            DASHSCOPE_DEFAULT_VISION_MODEL
            if vision_provider == "dashscope"
            else ANTHROPIC_DEFAULT_VISION_MODEL
        )
        vision_model = data.get("vision_model", default_vision_model)
        if vision_provider == "dashscope" and vision_model == ANTHROPIC_DEFAULT_VISION_MODEL:
            vision_model = DASHSCOPE_DEFAULT_VISION_MODEL
        elif vision_provider == "anthropic" and vision_model == DASHSCOPE_DEFAULT_VISION_MODEL:
            vision_model = ANTHROPIC_DEFAULT_VISION_MODEL

        return cls(
            zotero_data_dir=Path(data.get("zotero_data_dir", "~/Zotero")).expanduser(),
            chroma_db_path=Path(data.get("chroma_db_path", default_chroma)).expanduser(),
            embedding_model=data.get("embedding_model", default_model),
            embedding_dimensions=data.get("embedding_dimensions", default_dims),
            chunk_size=data.get("chunk_size", 400),
            chunk_overlap=data.get("chunk_overlap", 100),
            gemini_api_key=data.get("gemini_api_key"),
            dashscope_api_key=data.get("dashscope_api_key"),
            embedding_provider=data.get("embedding_provider", "gemini"),
            dashscope_embedding_endpoint=data.get("dashscope_embedding_endpoint", "compatible"),
            embedding_timeout=data.get("embedding_timeout", 120.0),
            embedding_max_retries=data.get("embedding_max_retries", 3),
            rerank_alpha=data.get("rerank_alpha", 0.7),
            rerank_section_weights=data.get("rerank_section_weights"),
            rerank_journal_weights=data.get("rerank_journal_weights"),
            rerank_enabled=data.get("rerank_enabled", True),
            oversample_multiplier=data.get("oversample_multiplier", 3),
            oversample_topic_factor=data.get("oversample_topic_factor", 5),
            stats_sample_limit=data.get("stats_sample_limit", 10000),
            ocr_language=data.get("ocr_language", "eng"),
            openalex_email=data.get("openalex_email"),
            vision_enabled=data.get("vision_enabled", True),
            vision_provider=vision_provider,
            vision_model=vision_model,
            anthropic_api_key=data.get("anthropic_api_key"),
            vision_max_tables_per_run=data.get("vision_max_tables_per_run"),
            vision_max_cost_usd=data.get("vision_max_cost_usd"),
            max_pages=data.get("max_pages", 40),
            preflight_enabled=data.get("preflight_enabled", True),
            zotero_api_key=data.get("zotero_api_key"),
            zotero_user_id=data.get("zotero_user_id"),
            zotero_library_type=data.get("zotero_library_type", "user"),
            semantic_scholar_api_key=data.get("semantic_scholar_api_key"),
        )

    def save(self, path: Path | str | None = None) -> None:
        """Write the config to JSON using an atomic write pattern."""
        if path is not None:
            config_path = Path(path).expanduser()
        else:
            config_path = _default_config_dir() / "config.json"

        # Create parent dirs if missing
        config_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "zotero_data_dir": str(self.zotero_data_dir),
            "chroma_db_path": str(self.chroma_db_path),
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "embedding_provider": self.embedding_provider,
            "dashscope_embedding_endpoint": self.dashscope_embedding_endpoint,
            "embedding_timeout": self.embedding_timeout,
            "embedding_max_retries": self.embedding_max_retries,
            "rerank_alpha": self.rerank_alpha,
            "rerank_section_weights": self.rerank_section_weights,
            "rerank_journal_weights": self.rerank_journal_weights,
            "rerank_enabled": self.rerank_enabled,
            "oversample_multiplier": self.oversample_multiplier,
            "oversample_topic_factor": self.oversample_topic_factor,
            "stats_sample_limit": self.stats_sample_limit,
            "ocr_language": self.ocr_language,
            "openalex_email": self.openalex_email,
            "vision_enabled": self.vision_enabled,
            "vision_provider": self.vision_provider,
            "vision_model": self.vision_model,
            "gemini_api_key": self.gemini_api_key,
            "dashscope_api_key": self.dashscope_api_key,
            "anthropic_api_key": self.anthropic_api_key,
            "vision_max_tables_per_run": self.vision_max_tables_per_run,
            "vision_max_cost_usd": self.vision_max_cost_usd,
            "max_pages": self.max_pages,
            "preflight_enabled": self.preflight_enabled,
            "zotero_api_key": self.zotero_api_key,
            "zotero_user_id": self.zotero_user_id,
            "zotero_library_type": self.zotero_library_type,
            "semantic_scholar_api_key": self.semantic_scholar_api_key,
        }
        data = {key: value for key, value in data.items() if value is not None}

        # Atomic write: temp file + rename
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=config_path.parent, suffix=".tmp", prefix="zotpilot_"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            # Set restrictive permissions on Unix before atomic rename
            if sys.platform != "win32":
                os.chmod(tmp_path, 0o600)

            os.replace(tmp_path, config_path)
            tmp_path = None  # Successfully replaced, no cleanup needed
        except OSError as e:
            # Clean up temp file on failure, original config untouched
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise RuntimeError(f"Failed to write config to {config_path}: {e}") from e

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors = []
        if not self.zotero_data_dir.exists():
            errors.append(f"Zotero data dir not found: {self.zotero_data_dir}")
        if not (self.zotero_data_dir / "zotero.sqlite").exists():
            errors.append(f"Zotero database not found: {self.zotero_data_dir / 'zotero.sqlite'}")

        if self.embedding_provider == "gemini" and not self.gemini_api_key:
            errors.append("GEMINI_API_KEY not set (required for embedding_provider='gemini')")
        elif self.embedding_provider == "dashscope" and not self.dashscope_api_key:
            errors.append("DASHSCOPE_API_KEY not set (required for embedding_provider='dashscope')")
        elif self.embedding_provider not in ("gemini", "dashscope", "local", "none"):
            errors.append(f"Invalid embedding_provider: {self.embedding_provider}. Must be 'gemini', 'dashscope', 'local', or 'none'")  # noqa: E501
        if self.dashscope_embedding_endpoint not in ("compatible", "native"):
            errors.append("Invalid dashscope_embedding_endpoint: must be 'compatible' or 'native'")

        if self.vision_provider not in ("anthropic", "dashscope"):
            errors.append("Invalid vision_provider: must be 'anthropic' or 'dashscope'")
        elif self.vision_enabled and self.vision_provider == "dashscope" and not self.dashscope_api_key:
            errors.append("DASHSCOPE_API_KEY not set (required for vision_provider='dashscope')")
        if self.vision_provider == "dashscope" and self.vision_model.startswith("claude-"):
            errors.append("Invalid vision_model for vision_provider='dashscope'")
        elif self.vision_provider == "anthropic" and self.vision_model.startswith("qwen"):
            errors.append("Invalid vision_model for vision_provider='anthropic'")

        return errors
