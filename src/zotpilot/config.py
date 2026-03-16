"""Configuration management."""
from dataclasses import dataclass
from pathlib import Path
import json
import logging
import os

logger = logging.getLogger(__name__)


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
    # Embedding provider: "gemini" (API) or "local" (ChromaDB default all-MiniLM-L6-v2)
    embedding_provider: str
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
    vision_model: str
    anthropic_api_key: str | None
    # Zotero Web API (for write operations)
    zotero_api_key: str | None
    zotero_user_id: str | None
    zotero_library_type: str  # "user" or "group"

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        """Load config from file and/or environment."""
        if path is not None:
            config_path = Path(path).expanduser()
        else:
            config_path = Path("~/.config/zotpilot/config.json").expanduser()

            # Migration support: if new config doesn't exist but old one does, load from old
            if not config_path.exists():
                old_config_path = Path("~/.config/deep-zotero/config.json").expanduser()
                if old_config_path.exists():
                    logger.info(
                        f"Migrating config from {old_config_path} to {config_path}. "
                        f"Please update your config path to {config_path}."
                    )
                    config_path = old_config_path

        data = {}
        if config_path.exists():
            with open(config_path) as f:
                data = json.load(f)

        return cls(
            zotero_data_dir=Path(data.get("zotero_data_dir", "~/Zotero")).expanduser(),
            chroma_db_path=Path(data.get("chroma_db_path", "~/.local/share/zotpilot/chroma")).expanduser(),
            embedding_model=data.get("embedding_model", "gemini-embedding-001"),
            embedding_dimensions=data.get("embedding_dimensions", 768),
            chunk_size=data.get("chunk_size", 400),
            chunk_overlap=data.get("chunk_overlap", 100),
            gemini_api_key=data.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY"),
            # Embedding provider: "gemini" or "local"
            embedding_provider=data.get("embedding_provider", "gemini"),
            # Embedding settings
            embedding_timeout=data.get("embedding_timeout", 120.0),
            embedding_max_retries=data.get("embedding_max_retries", 3),
            # Reranking settings
            rerank_alpha=data.get("rerank_alpha", 0.7),
            rerank_section_weights=data.get("rerank_section_weights"),
            rerank_journal_weights=data.get("rerank_journal_weights"),
            rerank_enabled=data.get("rerank_enabled", True),
            oversample_multiplier=data.get("oversample_multiplier", 3),
            oversample_topic_factor=data.get("oversample_topic_factor", 5),
            stats_sample_limit=data.get("stats_sample_limit", 10000),
            # OCR settings — language passed through to pymupdf-layout
            ocr_language=data.get("ocr_language", "eng"),
            # OpenAlex settings
            openalex_email=data.get("openalex_email") or os.environ.get("OPENALEX_EMAIL"),
            # Vision extraction settings
            vision_enabled=data.get("vision_enabled", True),
            vision_model=data.get("vision_model", "claude-haiku-4-5-20251001"),
            anthropic_api_key=data.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"),
            zotero_api_key=data.get("zotero_api_key") or os.environ.get("ZOTERO_API_KEY"),
            zotero_user_id=data.get("zotero_user_id") or os.environ.get("ZOTERO_USER_ID"),
            zotero_library_type=data.get("zotero_library_type", "user"),
        )

    def save(self, path: Path | str | None = None) -> None:
        """Write the config to JSON.

        Args:
            path: Target file path. Defaults to ~/.config/zotpilot/config.json.
        """
        if path is not None:
            config_path = Path(path).expanduser()
        else:
            config_path = Path("~/.config/zotpilot/config.json").expanduser()

        config_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "zotero_data_dir": str(self.zotero_data_dir),
            "chroma_db_path": str(self.chroma_db_path),
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "gemini_api_key": self.gemini_api_key,
            "embedding_provider": self.embedding_provider,
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
            "vision_model": self.vision_model,
            "anthropic_api_key": self.anthropic_api_key,
            "zotero_api_key": self.zotero_api_key,
            "zotero_user_id": self.zotero_user_id,
            "zotero_library_type": self.zotero_library_type,
        }

        with open(config_path, "w") as f:
            json.dump(data, f, indent=2)

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors = []
        if not self.zotero_data_dir.exists():
            errors.append(f"Zotero data dir not found: {self.zotero_data_dir}")
        if not (self.zotero_data_dir / "zotero.sqlite").exists():
            errors.append(f"Zotero database not found: {self.zotero_data_dir / 'zotero.sqlite'}")

        # Only require API key for Gemini provider
        if self.embedding_provider == "gemini" and not self.gemini_api_key:
            errors.append("GEMINI_API_KEY not set (required for embedding_provider='gemini')")
        elif self.embedding_provider not in ("gemini", "local"):
            errors.append(f"Invalid embedding_provider: {self.embedding_provider}. Must be 'gemini' or 'local'")

        return errors
