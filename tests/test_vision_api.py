"""Tests for vision API request construction and prompt budgets."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_spec():
    from zotpilot.feature_extraction.vision_api import TableVisionSpec

    return TableVisionSpec(
        table_id="T1",
        pdf_path=Path("/tmp/fake.pdf"),
        page_num=1,
        bbox=(0.0, 0.0, 100.0, 100.0),
        raw_text="header a b c",
        caption="Table 1. Test caption",
        garbled=False,
    )


class TestVisionAPIRequestBudget:
    def test_default_request_uses_compact_prompt_and_lower_max_tokens(self):
        from zotpilot.feature_extraction.vision_api import VisionAPI
        from zotpilot.feature_extraction.vision_extract import (
            EXTRACTION_EXAMPLES,
            VISION_COMPACT_SYSTEM,
        )

        api = VisionAPI.__new__(VisionAPI)
        api._model = "claude-haiku-4-5-20251001"
        api._cache = False
        api._prompt_mode = "compact"
        api._max_output_tokens = 1536
        request = api._build_request(_make_spec(), [("ZmFrZQ==", "image/png")])

        system_text = request["params"]["system"][0]["text"]
        assert request["params"]["max_tokens"] == 1536
        assert system_text == VISION_COMPACT_SYSTEM
        assert EXTRACTION_EXAMPLES not in system_text

    def test_full_prompt_mode_keeps_examples(self):
        from zotpilot.feature_extraction.vision_api import VisionAPI
        from zotpilot.feature_extraction.vision_extract import EXTRACTION_EXAMPLES

        api = VisionAPI.__new__(VisionAPI)
        api._model = "claude-haiku-4-5-20251001"
        api._cache = False
        api._prompt_mode = "full"
        api._max_output_tokens = 2048
        request = api._build_request(_make_spec(), [("ZmFrZQ==", "image/png")])

        system_text = request["params"]["system"][0]["text"]
        assert request["params"]["max_tokens"] == 2048
        assert EXTRACTION_EXAMPLES in system_text


class TestLocalVisionAPIPromptMode:
    def test_default_messages_use_compact_prompt(self):
        from zotpilot.feature_extraction.vision_extract import (
            EXTRACTION_EXAMPLES,
            VISION_COMPACT_SYSTEM,
        )

        fake_openai = MagicMock()
        fake_openai.OpenAI.return_value = MagicMock()
        with patch.dict(sys.modules, {"openai": fake_openai}):
            from zotpilot.feature_extraction.local_vision_api import LocalVisionAPI
            api = LocalVisionAPI()
        messages = api._build_messages(_make_spec(), [("ZmFrZQ==", "image/png")])

        assert api._max_tokens == 1536
        assert messages[0]["content"] == VISION_COMPACT_SYSTEM
        assert EXTRACTION_EXAMPLES not in messages[0]["content"]

    def test_full_prompt_mode_keeps_examples(self):
        from zotpilot.feature_extraction.vision_extract import EXTRACTION_EXAMPLES

        fake_openai = MagicMock()
        fake_openai.OpenAI.return_value = MagicMock()
        with patch.dict(sys.modules, {"openai": fake_openai}):
            from zotpilot.feature_extraction.local_vision_api import LocalVisionAPI
            api = LocalVisionAPI(prompt_mode="full", max_tokens=2048)
        messages = api._build_messages(_make_spec(), [("ZmFrZQ==", "image/png")])

        assert api._max_tokens == 2048
        assert EXTRACTION_EXAMPLES in messages[0]["content"]
