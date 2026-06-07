from zotpilot.pdf.extractor import (
    _native_page_chunks,
    _should_prefer_native,
    _should_run_full_document_ocr,
)


def test_ocr_policy_skips_text_rich_documents():
    assert _should_run_full_document_ocr(
        total_chars=5000,
        page_count=10,
        near_empty_pages=0,
    ) is False


def test_ocr_policy_runs_for_scan_like_documents():
    assert _should_run_full_document_ocr(
        total_chars=50,
        page_count=10,
        near_empty_pages=9,
    ) is True


def test_ocr_policy_skips_mixed_documents_with_enough_native_text():
    assert _should_run_full_document_ocr(
        total_chars=200,
        page_count=4,
        near_empty_pages=2,
    ) is False




class TestNativeFloor:
    """pymupdf4llm's internal OCR can clobber a good text layer with near-empty
    output; we fall back to native text when it under-extracts."""

    def test_prefers_native_when_md_clobbered(self):
        # to_markdown returned 0 chars but native layer has 5037 → use native
        assert _should_prefer_native(md_chars=0, native_total=5037) is True
        assert _should_prefer_native(md_chars=50, native_total=5037) is True

    def test_keeps_markdown_when_comparable(self):
        # to_markdown stripped headers/footers but kept most text → keep markdown
        assert _should_prefer_native(md_chars=4000, native_total=5000) is False
        assert _should_prefer_native(md_chars=2600, native_total=5000) is False  # 52% > 50%

    def test_does_not_fire_for_scanned_pdf(self):
        # native layer itself near-empty (scanned) → leave to the gated OCR fallback
        assert _should_prefer_native(md_chars=10, native_total=150) is False
        assert _should_prefer_native(md_chars=0, native_total=0) is False

    def test_native_page_chunks_shape(self):
        chunks = _native_page_chunks(["page one", "page two"])
        assert len(chunks) == 2
        assert chunks[0]["text"] == "page one"
        assert chunks[0]["metadata"]["page_number"] == 1
        assert chunks[1]["metadata"]["page_number"] == 2
        assert chunks[0]["page_boxes"] == []


class TestNativeFloorGarble:
    """pymupdf4llm's partial internal OCR can inject U+FFFD into an otherwise good
    doc; prefer the clean native layer when it does."""

    def test_prefers_native_when_md_injects_garble(self):
        # native clean (0 fffd), md introduced 278 fffd over 11001 chars (2.5%)
        assert _should_prefer_native(md_chars=11001, native_total=10177, md_fffd=278, native_fffd=0) is True

    def test_keeps_markdown_when_few_stray_fffd(self):
        # a couple stray replacement chars (<0.5%) is not worth losing structure
        assert _should_prefer_native(md_chars=11000, native_total=10000, md_fffd=5, native_fffd=0) is False

    def test_keeps_markdown_when_native_also_garbled(self):
        # native layer itself has replacement chars (genuine bad font) -> no gain
        assert _should_prefer_native(md_chars=11000, native_total=10000, md_fffd=300, native_fffd=250) is False


class TestInternalOcrDisabled:
    """The internal-OCR disable must patch get_textpage_ocr and always restore it."""

    def test_patches_then_restores(self):
        import pymupdf

        from zotpilot.pdf.extractor import _internal_ocr_disabled
        orig = pymupdf.Page.get_textpage_ocr
        with _internal_ocr_disabled():
            assert pymupdf.Page.get_textpage_ocr is not orig
        assert pymupdf.Page.get_textpage_ocr is orig

    def test_restores_on_exception(self):
        import pymupdf
        import pytest

        from zotpilot.pdf.extractor import _internal_ocr_disabled
        orig = pymupdf.Page.get_textpage_ocr
        with pytest.raises(ValueError):
            with _internal_ocr_disabled():
                raise ValueError("boom")
        assert pymupdf.Page.get_textpage_ocr is orig
