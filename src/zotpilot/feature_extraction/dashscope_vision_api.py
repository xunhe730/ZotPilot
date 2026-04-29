"""DashScope/Qwen vision API for table extraction."""

from __future__ import annotations

import base64
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import pymupdf

from .vision_api import TableVisionSpec
from .vision_extract import (
    VISION_COMPACT_SYSTEM,
    VISION_FIRST_SYSTEM,
    AgentResponse,
    build_common_ctx,
    parse_agent_response,
    render_table_region,
)

logger = logging.getLogger(__name__)

DEFAULT_DASHSCOPE_VISION_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_DASHSCOPE_VISION_MODEL = "qwen3-vl-flash"
DEFAULT_DASHSCOPE_VISION_MAX_TOKENS = 1536


def _failed_response(message: str) -> AgentResponse:
    """Build a parse-failure response compatible with the extraction pipeline."""
    return AgentResponse(
        headers=[],
        rows=[],
        footnotes="",
        table_label=None,
        caption="",
        is_incomplete=False,
        incomplete_reason="",
        raw_shape=(0, 0),
        parse_success=False,
        raw_response=message,
        recrop_needed=False,
        recrop_bbox_pct=None,
    )


class DashScopeVisionAPI:
    """Vision table extraction backed by DashScope/Qwen-VL.

    This adapter keeps the same ``extract_tables_batch`` contract as the
    Anthropic-backed ``VisionAPI`` so PDF extraction can switch providers
    without changing table post-processing.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_DASHSCOPE_VISION_MODEL,
        base_url: str = DEFAULT_DASHSCOPE_VISION_URL,
        max_tokens: int = DEFAULT_DASHSCOPE_VISION_MAX_TOKENS,
        max_workers: int = 3,
        timeout: float = 120.0,
        prompt_mode: str = "compact",
    ) -> None:
        self._api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        if not self._api_key:
            raise ValueError("DASHSCOPE_API_KEY not set")
        self._model = model
        self._base_url = base_url
        self._max_tokens = max_tokens
        self._max_workers = max_workers
        self._timeout = timeout
        self._prompt_mode = prompt_mode
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    @property
    def total_input_tokens(self) -> int:
        return self._total_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self._total_output_tokens

    @property
    def session_cost(self) -> float:
        """Return zero because pricing varies by Qwen-VL model and region."""
        return 0.0

    def _system_prompt(self) -> str:
        if self._prompt_mode == "full":
            return VISION_FIRST_SYSTEM
        return VISION_COMPACT_SYSTEM

    @staticmethod
    def _prepare_table(spec: TableVisionSpec) -> list[tuple[str, str]]:
        """Render PNG(s) for a table spec, return (base64, media_type) pairs."""
        doc = pymupdf.open(str(spec.pdf_path))
        try:
            page = doc[spec.page_num - 1]
            strips = render_table_region(page, spec.bbox)
            return [
                (base64.b64encode(png_bytes).decode("ascii"), media_type)
                for png_bytes, media_type in strips
            ]
        finally:
            doc.close()

    def _build_messages(
        self,
        spec: TableVisionSpec,
        images: list[tuple[str, str]],
    ) -> list[dict]:
        """Build OpenAI-compatible messages for one table extraction."""
        ctx = build_common_ctx(spec.raw_text, spec.caption, spec.garbled)

        user_content: list[dict] = []
        for b64, media_type in images:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{b64}"},
            })
        user_content.append({"type": "text", "text": ctx})

        return [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": user_content},
        ]

    def _extract_one(
        self,
        spec: TableVisionSpec,
        images: list[tuple[str, str]],
    ) -> AgentResponse:
        payload = {
            "model": self._model,
            "messages": self._build_messages(spec, images),
            "temperature": 0,
            "max_tokens": self._max_tokens,
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    self._base_url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()

            usage = data.get("usage") or {}
            self._total_input_tokens += usage.get("prompt_tokens", 0) or 0
            self._total_output_tokens += usage.get("completion_tokens", 0) or 0
            raw_text = data["choices"][0]["message"].get("content") or ""
            return parse_agent_response(raw_text, "dashscope-vision")
        except Exception as exc:
            logger.error("DashScope vision request failed for %s: %s", spec.table_id, exc)
            return _failed_response(str(exc))

    def extract_tables_batch(self, specs: list[TableVisionSpec]) -> list[AgentResponse]:
        """Extract tables via concurrent DashScope/Qwen-VL requests."""
        if not specs:
            return []

        prepared: list[tuple[TableVisionSpec, list[tuple[str, str]]]] = []
        for spec in specs:
            prepared.append((spec, self._prepare_table(spec)))

        responses: dict[int, AgentResponse] = {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {
                pool.submit(self._extract_one, spec, images): idx
                for idx, (spec, images) in enumerate(prepared)
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    responses[idx] = future.result()
                except Exception as exc:
                    logger.error("Unexpected DashScope vision error for %s: %s", specs[idx].table_id, exc)
                    responses[idx] = _failed_response(str(exc))

        return [responses[i] for i in range(len(specs))]
