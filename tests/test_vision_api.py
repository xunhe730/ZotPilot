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


class TestDashScopeVisionAPI:
    def test_messages_use_openai_compatible_image_url(self):
        from zotpilot.feature_extraction.dashscope_vision_api import DashScopeVisionAPI
        from zotpilot.feature_extraction.vision_extract import EXTRACTION_EXAMPLES, VISION_COMPACT_SYSTEM

        api = DashScopeVisionAPI(api_key="dashscope-key")
        messages = api._build_messages(_make_spec(), [("ZmFrZQ==", "image/png")])

        assert api._max_tokens == 1536
        assert messages[0] == {"role": "system", "content": VISION_COMPACT_SYSTEM}
        assert EXTRACTION_EXAMPLES not in messages[0]["content"]
        content = messages[1]["content"]
        assert content[0]["type"] == "image_url"
        assert content[0]["image_url"]["url"] == "data:image/png;base64,ZmFrZQ=="
        assert content[-1]["type"] == "text"

    def test_extract_one_posts_dashscope_payload_and_tracks_usage(self):
        from zotpilot.feature_extraction.dashscope_vision_api import DashScopeVisionAPI

        api = DashScopeVisionAPI(api_key="dashscope-key", model="qwen3-vl-flash")
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"table_label":"Table 1","caption":"Table 1. Test",'
                            '"is_incomplete":false,"incomplete_reason":"",'
                            '"headers":["A"],"rows":[["1"]],"footnotes":""}'
                        )
                    }
                }
            ],
        }

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value = mock_response
            result = api._extract_one(_make_spec(), [("ZmFrZQ==", "image/png")])

        payload = mock_client.return_value.__enter__.return_value.post.call_args.kwargs["json"]
        assert payload["model"] == "qwen3-vl-flash"
        assert payload["messages"][1]["content"][0]["type"] == "image_url"
        assert result.parse_success is True
        assert result.headers == ["A"]
        assert api.total_input_tokens == 11
        assert api.total_output_tokens == 7
