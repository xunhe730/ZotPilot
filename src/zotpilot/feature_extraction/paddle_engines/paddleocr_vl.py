"""PaddleOCR-VL-1.5 engine: VLM-based PDF table extraction.

The VL model outputs OTSL (table structure language) which the PaddleOCR
pipeline internally converts to HTML via ``convert_otsl_to_html()``.  Table
block content is therefore HTML, not markdown.  We reuse the HTML parser from
the PP-StructureV3 engine to handle colspan/rowspan correctly.

Backend selection is CC-gated:

- **CC >= 8.0** — native vLLM backend (inline, gets Flash Attention 2)
- **CC 7.0–7.9** — vLLM-server backend via Docker container
- **No GPU** — raises ``RuntimeError``

Override via env vars:

- ``PADDLEOCR_VL_SERVER_URL`` — server endpoint (default ``http://localhost:8118/v1``)
- ``PADDLEOCR_VL_BACKEND``   — force ``"native"`` or ``"server"``
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

_log = logging.getLogger(__name__)
_VL_SERVER_URL_DEFAULT = "http://localhost:8118/v1"


def _get_compute_capability() -> tuple[int, int] | None:
    """Return (major, minor) CC of GPU 0, or None if no CUDA GPU."""
    try:
        import paddle

        if not paddle.device.is_compiled_with_cuda():
            return None
        if paddle.device.cuda.device_count() < 1:
            return None
        return paddle.device.cuda.get_device_capability()
    except Exception:
        return None


def _check_vllm_server(url: str, timeout: float = 5.0) -> bool:
    """Return True if vLLM server responds at *url*/models."""
    models_url = url.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(models_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def _compose_file() -> Path:
    """Return the path to the Docker Compose file shipped with this package."""
    return Path(__file__).resolve().parents[4] / "tools" / "docker" / "docker-compose.paddleocr-vl.yml"


def _start_vllm_server(
    url: str,
    *,
    startup_timeout: float = 300.0,
    poll_interval: float = 3.0,
) -> None:
    """Start the vLLM Docker container and wait until it responds.

    Raises ``RuntimeError`` if Docker is not installed, the compose file is
    missing, or the server does not become healthy within *startup_timeout*
    seconds.
    """
    compose = _compose_file()
    if not compose.exists():
        raise RuntimeError(
            f"Docker Compose file not found at {compose}.\n"
            f"Expected: tools/docker/docker-compose.paddleocr-vl.yml"
        )

    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError(
            "Docker is not installed or not on PATH.\n"
            "Install Docker Desktop and try again, or start the vLLM "
            "server manually and set PADDLEOCR_VL_SERVER_URL."
        )

    _log.info("Starting vLLM Docker container via %s ...", compose)
    print(f"[paddleocr-vl] Starting vLLM Docker container...", flush=True)

    # Pull image first (can take minutes for multi-GB images).
    _log.info("Pulling Docker image (this may take several minutes on first run)...")
    print("[paddleocr-vl] Pulling Docker image (may take several minutes on first run)...", flush=True)
    pull = subprocess.run(
        [docker, "compose", "-f", str(compose), "pull"],
        capture_output=True,
        text=True,
        timeout=600,  # 10 min for large image pulls
    )
    if pull.returncode != 0:
        raise RuntimeError(
            f"docker compose pull failed (exit {pull.returncode}):\n"
            f"{pull.stderr.strip()}"
        )

    result = subprocess.run(
        [docker, "compose", "-f", str(compose), "up", "-d"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker compose up failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}"
        )

    _log.info("Waiting up to %.0fs for vLLM server at %s ...", startup_timeout, url)
    print(f"[paddleocr-vl] Waiting up to {startup_timeout:.0f}s for server at {url} ...", flush=True)
    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if _check_vllm_server(url, timeout=3.0):
            _log.info("vLLM server is ready.")
            return
        time.sleep(poll_interval)

    raise RuntimeError(
        f"vLLM server did not become ready at {url} within "
        f"{startup_timeout:.0f}s.\n\n"
        f"Check container logs:\n"
        f"  docker compose -f {compose} logs"
    )


def _patch_paddle_int() -> None:
    """Fix Paddle's ``int(tensor)`` for numpy >= 1.24.

    Paddle's ``_int_`` does ``int(np.array(var))`` which fails when the tensor
    has shape ``(1,)`` instead of ``()``.  Newer numpy versions reject
    ``int()`` on arrays with ``ndim > 0``.  We replace
    ``paddle.Tensor.__int__`` with a version that calls ``.item()`` on the
    numpy array so the conversion works regardless of tensor shape (as long as
    the tensor has exactly one element, which the existing assert already
    guarantees).
    """
    try:
        import numpy as _np
        import paddle

        if getattr(paddle.Tensor, "_int_patched_by_zcr", False):
            return  # already patched

        def _safe_int(var):  # type: ignore[no-untyped-def]
            numel = _np.prod(var.shape)
            assert numel == 1, "only one element variable can be converted to int."
            assert var._is_initialized(), "variable's tensor is not initialized"
            return int(_np.array(var).item())

        paddle.Tensor.__int__ = _safe_int
        paddle.Tensor._int_patched_by_zcr = True  # type: ignore[attr-defined]
    except Exception:
        pass  # non-critical — let the original code run


_patch_paddle_int()

from ..paddle_extract import RawPaddleTable  # noqa: E402
from .pp_structure import _parse_html_table  # noqa: E402


class PaddleOCRVLEngine:
    """PaddleOCR-VL-1.5 engine for extracting tables from PDF files.

    Initialises the VLM pipeline once at construction time.  Each call to
    ``extract_tables`` runs a full-document predict pass and returns all
    detected table blocks as ``RawPaddleTable`` instances.
    """
    def __init__(self) -> None:
        from paddleocr import PaddleOCRVL

        cc = _get_compute_capability()
        server_url = os.environ.get("PADDLEOCR_VL_SERVER_URL", _VL_SERVER_URL_DEFAULT)
        backend_override = os.environ.get("PADDLEOCR_VL_BACKEND")  # "native" | "server"

        if cc is None:
            raise RuntimeError(
                "PaddleOCR-VL-1.5 requires a CUDA GPU (CC >= 7.0). "
                "No CUDA device detected."
            )

        use_native = (cc[0] >= 8) or (backend_override == "native")

        if use_native and backend_override != "server":
            _log.info("VL-1.5: CC %d.%d >= 8.0 — using native vLLM backend", *cc)
            self._pipeline = PaddleOCRVL(
                pipeline_version="v1.5",
                device="gpu:0",
                vl_rec_backend="vllm",
            )
        elif cc[0] >= 7 or backend_override == "server":
            if not _check_vllm_server(server_url):
                print(
                    f"[paddleocr-vl] vLLM server not reachable at {server_url}"
                    f" — attempting auto-start",
                    flush=True,
                )
                _start_vllm_server(server_url)
            print(
                f"[paddleocr-vl] CC {cc[0]}.{cc[1]} < 8.0"
                f" — using vLLM server at {server_url}",
                flush=True,
            )
            self._pipeline = PaddleOCRVL(
                pipeline_version="v1.5",
                device="gpu:0",
                vl_rec_backend="vllm-server",
                vl_rec_server_url=server_url,
            )
        else:
            raise RuntimeError(
                f"PaddleOCR-VL-1.5 requires CC >= 7.0. Detected CC {cc[0]}.{cc[1]}."
            )

    def extract_tables(self, pdf_path: Path) -> list[RawPaddleTable]:
        """Extract all tables from *pdf_path* using PaddleOCR-VL-1.5.

        The pipeline runs predict on the full PDF, then ``restructure_pages``
        merges cross-page tables before the results are iterated.

        Args:
            pdf_path: Absolute path to the PDF file.

        Returns:
            One ``RawPaddleTable`` per detected table block.

        The predict output is a list of ``PaddleOCRVLResult`` objects (dict-like)
        with keys ``page_index`` (0-indexed), ``width``, ``height``, and
        ``parsing_res_list`` containing ``PaddleOCRVLBlock`` objects with
        attributes ``label``, ``content``, ``bbox``.
        """
        pages_res = self._pipeline.predict(str(pdf_path))
        restructured = self._pipeline.restructure_pages(pages_res, merge_tables=True)

        tables: list[RawPaddleTable] = []
        for page_result in restructured:
            # PaddleOCRVLResult uses page_index (0-indexed); convert to 1-indexed
            page_num: int = page_result.get("page_index", 0) + 1
            page_width: int = page_result.get("width", 0)
            page_height: int = page_result.get("height", 0)
            page_size: tuple[int, int] = (page_width, page_height)

            for block in page_result.get("parsing_res_list", []):
                # PaddleOCRVLBlock exposes .label / .content / .bbox attrs
                block_label: str = getattr(block, "label", "") or ""
                if "table" not in block_label.lower():
                    continue

                content_str: str = getattr(block, "content", "") or ""
                bbox_raw = getattr(block, "bbox", [0.0, 0.0, 0.0, 0.0])
                bbox: tuple[float, float, float, float] = (
                    float(bbox_raw[0]),
                    float(bbox_raw[1]),
                    float(bbox_raw[2]),
                    float(bbox_raw[3]),
                )

                # VL pipeline converts OTSL → HTML; parse as HTML.
                headers, rows, footnotes = _parse_html_table(content_str)

                tables.append(
                    RawPaddleTable(
                        page_num=page_num,
                        bbox=bbox,
                        page_size=page_size,
                        headers=headers,
                        rows=rows,
                        footnotes=footnotes,
                        engine_name="paddleocr_vl_1.5",
                        raw_output=content_str,
                    )
                )

        return tables
