"""E2E test for ztp-tutor.

Gated by env var ZTP_TUTOR_E2E_PDF (path to a real text-PDF in the user's
local library). When unset, all tests in this module are skipped — so CI
without a real library is unaffected.

Manual Zotero legibility spike (RM-2): after this test passes, open the
annotated PDF in the actual Zotero reader and confirm the page-1 sticky-note
overview and the 5-color highlights render with legible CJK. This is a
manual step, not automated.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pymupdf
import pytest

from zotpilot.pdf.annotator import (
    ZOTPILOT_MARKER,
    AnnotationSpec,
    annotate_pdf_file,
)

E2E_PDF = os.environ.get("ZTP_TUTOR_E2E_PDF")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not E2E_PDF or not Path(E2E_PDF).exists(),
        reason="ZTP_TUTOR_E2E_PDF not set or file missing — skipping E2E",
    ),
]


@pytest.fixture
def working_pdf(tmp_path: Path) -> Path:
    """Copy the E2E PDF into a tmp location so the test never mutates the
    user's real library file."""
    src = Path(E2E_PDF)  # type: ignore[arg-type]
    dst = tmp_path / src.name
    shutil.copy2(src, dst)
    return dst


def _extract_first_sentences(pdf_path: Path, n: int = 5) -> list[tuple[int, str]]:
    """Pick (page_num, sentence) pairs from the first few pages for the
    five-dim annotation test."""
    doc = pymupdf.open(str(pdf_path))
    out: list[tuple[int, str]] = []
    for pno in range(min(3, doc.page_count)):
        page = doc[pno]
        text = page.get_text()
        for line in text.split("\n"):
            stripped = line.strip()
            if 20 <= len(stripped) <= 200:
                out.append((pno + 1, stripped))
                if len(out) >= n:
                    doc.close()
                    return out
    doc.close()
    return out


def test_e2e_five_dim_annotation_with_overview(working_pdf: Path) -> None:
    """End-to-end: build 5-dim specs from real text, write+verify+swap,
    reopen and confirm marker count, warnings empty, is_repaired False,
    overview round-trips CJK."""
    sentences = _extract_first_sentences(working_pdf, n=5)
    if len(sentences) < 5:
        pytest.skip(f"Could not extract 5 candidate sentences from {working_pdf}")

    dims = ["thesis", "concept", "evidence", "rebuttal", "method"]
    specs = [
        AnnotationSpec(
            quote=text,
            dimension=dim,
            comment=f"中文导读：第{i + 1}维 {dim} 的解读。",
            page_hint=page,
        )
        for i, ((page, text), dim) in enumerate(zip(sentences, dims))
    ]

    overview = {
        "thesis": "本文核心论点：测试 ztp-tutor 端到端写入与验证。",
        "skeleton": {
            "question": "PDF 标注是否能在 Zotero 中正确显示？",
            "claim": "通过 PyMuPDF 直接写入高亮和便签。",
            "evidence": "回读断言 marker 计数与无 mupdf 警告。",
            "rebuttal": "存在原始 PDF 损坏的风险，由备份和原子替换缓解。",
            "conclusion": "5 维高亮 + 第 1 页便签概览。",
        },
        "strongest": "原子替换与非消耗回滚保证数据安全。",
        "weakest": "扫描型 PDF 无法处理。",
    }

    report = annotate_pdf_file(working_pdf, specs, overview)

    assert len(report.placed) >= 1, "expected at least one annotation placed"
    assert report.overview_placed is True, "page-1 overview must be placed"
    assert report.verified is True, f"verification failed: {report.verification_details}"
    assert report.verification_details["warnings_empty"] is True
    assert report.verification_details["not_repaired"] is True
    assert report.verification_details["annot_count_match"] is True

    # backup must exist alongside the original
    backup = Path(report.backup_path)
    assert backup.exists() and backup.stat().st_size > 0

    # reopen and confirm marker count + overview content round-trips CJK
    doc = pymupdf.open(str(working_pdf))
    marker_count = 0
    overview_contents: list[str] = []
    for page in doc:
        for annot in page.annots() or []:
            try:
                title = annot.info.get("title", "") or ""
            except Exception:
                continue
            if title.startswith(ZOTPILOT_MARKER):
                marker_count += 1
                content = annot.info.get("content", "") or ""
                if content and "本文核心论点" in content:
                    overview_contents.append(content)
    doc.close()

    expected = len(report.placed) + (1 if report.overview_placed else 0)
    assert marker_count == expected, (
        f"marker count {marker_count} != expected {expected}"
    )
    assert overview_contents, "page-1 overview CJK content did not round-trip"


def test_e2e_idempotent_rerun(working_pdf: Path) -> None:
    """Re-run on the same PDF: prior ZotPilot annotations cleared,
    file size stays within 1.05x of single-annotated size."""
    sentences = _extract_first_sentences(working_pdf, n=3)
    if len(sentences) < 3:
        pytest.skip("not enough sentences for re-run test")

    specs = [
        AnnotationSpec(quote=text, dimension="thesis", comment="一", page_hint=page)
        for page, text in sentences
    ]
    overview = {"thesis": "test", "skeleton": {}, "strongest": "", "weakest": ""}

    r1 = annotate_pdf_file(working_pdf, specs, overview)
    size_after_1 = working_pdf.stat().st_size

    r2 = annotate_pdf_file(working_pdf, specs, overview)
    size_after_2 = working_pdf.stat().st_size

    assert r1.verified and r2.verified
    assert size_after_2 <= size_after_1 * 1.05, (
        f"file grew unboundedly: {size_after_1} -> {size_after_2}"
    )
