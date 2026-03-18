"""Chinese detection and query translation."""
import logging
import os

logger = logging.getLogger(__name__)


def _contains_chinese(text: str) -> bool:
    """Return True if text contains at least one Chinese character."""
    import re
    return bool(re.search(r'[\u4e00-\u9fff\u3400-\u4dbf\U00020000-\U0002a6df]', text))


def _translate_to_english(text: str) -> str | None:
    """Translate Chinese text to English using Gemini. Returns None on failure."""
    from .state import _get_config  # lazy import to avoid circular dependency
    config = _get_config()
    api_key = config.gemini_api_key if config else None
    if not api_key:
        api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        import google.genai as genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=(
                "Translate the following Chinese academic query to English. "
                "Output only the translated text, nothing else.\n\n" + text
            ),
        )
        translated = response.text.strip()
        logger.debug(f"Translated query: '{text}' -> '{translated}'")
        return translated if translated else None
    except Exception as e:
        logger.warning(f"Query translation failed: {e}")
        return None
