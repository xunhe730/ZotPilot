from zotpilot.pdf.extractor import _should_run_full_document_ocr


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
