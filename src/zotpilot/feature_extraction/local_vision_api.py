"""Local vision API: vLLM-backed table extraction via OpenAI-compatible endpoint.

Drop-in replacement for VisionAPI that targets a local vLLM server
(e.g. Qwen2.5-VL-7B) instead of the Anthropic Batch API.  Reuses the
same prompt, rendering, and parsing utilities from vision_extract.py.
"""

from __future__ import annotations

import base64
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

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


class LocalVisionAPI:
    """Vision table extraction backed by a local vLLM server.

    Uses the OpenAI-compatible ``/v1/chat/completions`` endpoint exposed
    by vLLM.  Concurrent requests are managed via a thread pool — there
    is no batch API; each table is a separate HTTP call.

    Parameters
    ----------
    base_url:
        vLLM OpenAI-compatible endpoint (default ``http://localhost:8118/v1``).
        Override with ``LOCAL_VISION_URL`` env var.
    model:
        Model name served by vLLM (default ``Qwen/Qwen2.5-VL-7B-Instruct``).
        Override with ``LOCAL_VISION_MODEL`` env var.
    api_key:
        API key for the endpoint.  vLLM doesn't require a real key.
    max_tokens:
        Maximum tokens per response.
    max_workers:
        Concurrent requests to vLLM.
    timeout:
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8118/v1",
        model: str = "Qwen/Qwen2-VL-2B-Instruct",
        api_key: str = "EMPTY",
        max_tokens: int = 1536,
        max_workers: int = 4,
        timeout: float = 120.0,
        prompt_mode: str = "compact",
    ) -> None:
        try:
            import openai as _openai  # type: ignore[import-not-found]  # no stubs available
        except ImportError:
            raise ImportError(
                "openai package required for LocalVisionAPI: "
                "pip install 'zotpilot[vision]'"
            )

        self._base_url = os.environ.get("LOCAL_VISION_URL", base_url)
        self._model = os.environ.get("LOCAL_VISION_MODEL", model)
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._max_workers = max_workers
        self._timeout = timeout
        self._prompt_mode = prompt_mode

        self._client = _openai.OpenAI(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=self._timeout,
        )

        self._total_input_tokens = 0
        self._total_output_tokens = 0

    def _system_prompt(self) -> str:
        """Return the configured system prompt variant."""
        if self._prompt_mode == "full":
            return VISION_FIRST_SYSTEM
        return VISION_COMPACT_SYSTEM

    # ------------------------------------------------------------------
    # Token accounting (no USD cost — local inference)
    # ------------------------------------------------------------------

    @property
    def total_input_tokens(self) -> int:
        return self._total_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self._total_output_tokens

    # ------------------------------------------------------------------
    # Table rendering (same as VisionAPI._prepare_table)
    # ------------------------------------------------------------------

    @staticmethod
    def _prepare_table(
        spec: TableVisionSpec,
    ) -> list[tuple[str, str]]:
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

    # ------------------------------------------------------------------
    # Request building (OpenAI chat format)
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        spec: TableVisionSpec,
        images: list[tuple[str, str]],
    ) -> list[dict]:
        """Build OpenAI-format messages for a single table extraction."""
        ctx = build_common_ctx(spec.raw_text, spec.caption, spec.garbled)

        user_content: list[dict] = [{"type": "text", "text": ctx}]
        for b64, media_type in images:
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{media_type};base64,{b64}",
                },
            })

        return [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": user_content},
        ]

    # ------------------------------------------------------------------
    # Single-table extraction
    # ------------------------------------------------------------------

    def _extract_one(
        self,
        spec: TableVisionSpec,
        images: list[tuple[str, str]],
    ) -> AgentResponse:
        """Send one table to the vLLM server and parse the response."""
        messages = self._build_messages(spec, images)

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=self._max_tokens,
                temperature=0.0,
            )

            raw_text = response.choices[0].message.content or ""

            usage = response.usage
            if usage:
                self._total_input_tokens += usage.prompt_tokens or 0
                self._total_output_tokens += usage.completion_tokens or 0

            return parse_agent_response(raw_text, "local-vision")

        except Exception as exc:
            logger.error(
                "LocalVisionAPI request failed for %s: %s", spec.table_id, exc,
            )
            return AgentResponse(
                headers=[], rows=[], footnotes="",
                table_label=None, caption="",
                is_incomplete=False, incomplete_reason="",
                raw_shape=(0, 0), parse_success=False,
                raw_response=str(exc),
                recrop_needed=False, recrop_bbox_pct=None,
            )

    # ------------------------------------------------------------------
    # Main entry point (same signature as VisionAPI.extract_tables_batch)
    # ------------------------------------------------------------------

    def extract_tables_batch(
        self,
        specs: list[TableVisionSpec],
    ) -> list[AgentResponse]:
        """Extract tables via concurrent requests to a local vLLM server.

        Drop-in replacement for ``VisionAPI.extract_tables_batch()``.
        Renders each table as PNG(s), sends concurrent requests via a
        thread pool, and parses responses with the shared parser.

        Args:
            specs: Table vision specs to extract.

        Returns:
            AgentResponse per spec, in the same order as input.
        """
        if not specs:
            return []

        # Pre-render all tables (CPU work — do sequentially)
        prepared: list[tuple[TableVisionSpec, list[tuple[str, str]]]] = []
        for spec in specs:
            images = self._prepare_table(spec)
            prepared.append((spec, images))

        # Submit concurrent requests to vLLM
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
                    logger.error(
                        "Unexpected error for spec %d (%s): %s",
                        idx, specs[idx].table_id, exc,
                    )
                    responses[idx] = AgentResponse(
                        headers=[], rows=[], footnotes="",
                        table_label=None, caption="",
                        is_incomplete=False, incomplete_reason="",
                        raw_shape=(0, 0), parse_success=False,
                        raw_response=str(exc),
                        recrop_needed=False, recrop_bbox_pct=None,
                    )

        # Return in input order
        return [responses[i] for i in range(len(specs))]
