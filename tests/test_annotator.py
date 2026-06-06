"""Unit tests for the ztp-tutor annotator module (Phase 1).

Covers US-001 through US-006 in .omc/prd.json — pure helpers, write envelope,
placement, idempotency, smart-merge with foreign annots, and fixtures.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from zotpilot.pdf import annotator as ann
from zotpilot.pdf.annotator import (
    DIMENSION_RGB,
    MAX_ANNOTATION_COUNT,
    ZOTPILOT_MARKER,
    AnnotationSpec,
    ExistingAnnot,
    PlacementReport,
    ScannedPdfError,
    _place_overview_note,
    _place_region_annotation,
    _place_single_annotation,
    annotate_pdf_file,
    backup_pdf,
    build_overview_text,
    clear_zotpilot_annotations,
    has_text_layer,
    map_dimension_to_rgb,
    normalize_quote_for_pdf,
    preflight_write_access,
    re_ligate_quote,
    read_existing_annotations,
    validate_annotation_specs,
    verify_annotated_pdf,
)
from zotpilot.state import ToolError

# ---------------------------------------------------------------------------
# Fixture generators
# ---------------------------------------------------------------------------


def _make_text_pdf(path: Path, lines: list[tuple[float, float, str]]) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    for x, y, text in lines:
        page.insert_text((x, y), text)
    doc.save(str(path))
    doc.close()


def _make_multipage_pdf(path: Path, pages: list[list[tuple[float, float, str]]]) -> None:
    doc = pymupdf.open()
    for plines in pages:
        page = doc.new_page()
        for x, y, text in plines:
            page.insert_text((x, y), text)
    doc.save(str(path))
    doc.close()


def _make_scanned_pdf(path: Path) -> None:
    """Empty / image-only pdf (no text-extraction)."""
    doc = pymupdf.open()
    page = doc.new_page()
    # draw a rectangle so the page is not empty; no text inserted -> no text layer
    page.draw_rect((50, 50, 200, 200), color=(0, 0, 0))
    doc.save(str(path))
    doc.close()


def _make_repeated_phrase_pdf(path: Path) -> None:
    """Phrase appears three times on one page."""
    _make_text_pdf(
        path,
        [
            (50, 80, "Repeated marker phrase here for testing."),
            (50, 120, "Another line. Repeated marker phrase here for testing."),
            (50, 160, "Repeated marker phrase here for testing again."),
        ],
    )


def _make_hyphenated_pdf(path: Path) -> None:
    _make_text_pdf(
        path,
        [
            (50, 80, "We propose a new meth-"),
            (50, 100, "od that improves accuracy."),
        ],
    )


@pytest.fixture
def text_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "text.pdf"
    _make_text_pdf(
        p,
        [
            (50, 80, "Our efficient method outperforms the baseline by a wide margin."),
            (50, 120, "The final results show consistent improvements across tasks."),
        ],
    )
    return p


@pytest.fixture
def scanned_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "scanned.pdf"
    _make_scanned_pdf(p)
    return p


@pytest.fixture
def repeated_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "repeated.pdf"
    _make_repeated_phrase_pdf(p)
    return p


@pytest.fixture
def hyphenated_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "hyphen.pdf"
    _make_hyphenated_pdf(p)
    return p


# Note: the raw-ligature codepoint (U+FB01) PDF is skipped because base-14 fitz
# fonts render notdef for it. The §7.10 #1 region-offset test still runs.

# ---------------------------------------------------------------------------
# US-001 — Pure helpers / constants / validation
# ---------------------------------------------------------------------------


def test_constants_present():
    assert ZOTPILOT_MARKER == "ZotPilot 导读"
    assert set(DIMENSION_RGB.keys()) == {"thesis", "concept", "evidence", "rebuttal", "method"}
    assert ann.MAX_COMMENT_BYTES == 500
    assert ann.MAX_QUOTE_BYTES == 1000
    assert ann.MAX_ANNOTATION_COUNT == 200
    assert ann.MAX_OVERVIEW_BYTES == 2000


def test_reverse_ligature_map_longest_first_application():
    # ffi should map to ﬃ even though "fi" is also a key
    assert re_ligate_quote("efficient") == "eﬃcient"
    assert re_ligate_quote("final") == "ﬁnal"
    assert re_ligate_quote("flow") == "ﬂow"
    assert re_ligate_quote("affluent") == "aﬄuent"
    assert re_ligate_quote("offer") == "oﬀer"


def test_map_dimension_to_rgb_known_and_unknown():
    for k, v in DIMENSION_RGB.items():
        assert map_dimension_to_rgb(k) == v
    with pytest.raises(ToolError):
        map_dimension_to_rgb("nope")


def test_normalize_idempotent_and_de_hyphenation():
    text = "We  use\nﬁnal “quotes” – and meth-\nod stuff."
    once = normalize_quote_for_pdf(text)
    twice = normalize_quote_for_pdf(once)
    assert once == twice  # idempotent
    assert "method" in once  # de-hyphenation
    assert "final" in once  # ligature normalized
    assert "\"quotes\"" in once  # unicode quote -> ASCII


def test_normalize_strips_markdown_emphasis_and_markers():
    # pymupdf4llm leaks Markdown into page text; search_for queries raw PDF text.
    assert normalize_quote_for_pdf("**Figure 3:** Velocity fields") == "Figure 3: Velocity fields"
    assert normalize_quote_for_pdf("**Table 1**   The AEE results") == "Table 1 The AEE results"
    assert normalize_quote_for_pdf("## Methods and setup") == "Methods and setup"
    assert normalize_quote_for_pdf("- bullet item text") == "bullet item text"
    assert normalize_quote_for_pdf("1. first numbered item") == "first numbered item"
    assert normalize_quote_for_pdf("use `code` inline here") == "use code inline here"
    # _ / * are stripped only as emphasis DELIMITERS (not flanked by alnum on both
    # sides); intra-word _ / * (snake_case, subscripts, a*b) are preserved.
    assert normalize_quote_for_pdf("snake_case_id intact") == "snake_case_id intact"
    assert normalize_quote_for_pdf("a*b kept") == "a*b kept"
    assert normalize_quote_for_pdf("the variable x_t here") == "the variable x_t here"
    # single-underscore italics (pymupdf4llm) ARE stripped so they match the PDF text layer
    assert normalize_quote_for_pdf("JEPA is _not generative_ here") == "JEPA is not generative here"
    assert normalize_quote_for_pdf("we advocate _against_ it") == "we advocate against it"
    assert normalize_quote_for_pdf("*italic* lead") == "italic lead"
    # idempotent with markdown present
    s = "**bold** ## h `c` text_id"
    assert normalize_quote_for_pdf(normalize_quote_for_pdf(s)) == normalize_quote_for_pdf(s)


def test_validate_annotation_specs_caps():
    base = AnnotationSpec(quote="x" * 14, dimension="thesis", comment="ok")
    validate_annotation_specs([base], None)

    # comment > 500B
    with pytest.raises(ToolError, match="comment exceeds"):
        validate_annotation_specs(
            [AnnotationSpec(quote="x" * 14, dimension="thesis", comment="y" * 501)],
            None,
        )
    # quote > 1000B
    with pytest.raises(ToolError, match="quote exceeds"):
        validate_annotation_specs(
            [AnnotationSpec(quote="x" * 1001, dimension="thesis", comment="ok")],
            None,
        )
    # count > 200
    with pytest.raises(ToolError, match="exceeds cap"):
        validate_annotation_specs(
            [base] * (MAX_ANNOTATION_COUNT + 1), None
        )
    # overview > 2000B (build_overview_text composes; ensure threshold)
    big = {"thesis": "x" * 2100}
    with pytest.raises(ToolError, match="overview exceeds"):
        validate_annotation_specs([], big)


def test_validate_region_requires_page_and_bbox():
    bad = AnnotationSpec(
        quote="x" * 14, dimension="thesis", comment="c", kind="region"
    )
    with pytest.raises(ToolError, match="requires non-null page and bbox"):
        validate_annotation_specs([bad], None)
    # inverted bbox
    inv = AnnotationSpec(
        quote="x" * 14, dimension="thesis", comment="c", kind="region",
        page=1, bbox=(100.0, 100.0, 10.0, 10.0),
    )
    with pytest.raises(ToolError, match="inverted"):
        validate_annotation_specs([inv], None, page_count=10)
    # non-finite
    nan = AnnotationSpec(
        quote="x" * 14, dimension="thesis", comment="c", kind="region",
        page=1, bbox=(float("nan"), 0.0, 10.0, 10.0),
    )
    with pytest.raises(ToolError, match="finite"):
        validate_annotation_specs([nan], None, page_count=10)
    # page out of range
    oor = AnnotationSpec(
        quote="x" * 14, dimension="thesis", comment="c", kind="region",
        page=99, bbox=(0.0, 0.0, 10.0, 10.0),
    )
    with pytest.raises(ToolError, match="out of range"):
        validate_annotation_specs([oor], None, page_count=10)


def test_scanned_pdf_error_importable():
    assert issubclass(ScannedPdfError, Exception)


# ---------------------------------------------------------------------------
# US-002 — Preflight + backup + has_text_layer
# ---------------------------------------------------------------------------


def test_preflight_missing_file(tmp_path: Path):
    with pytest.raises(ToolError, match="not found"):
        preflight_write_access(tmp_path / "nope.pdf")


def test_preflight_readonly_file(tmp_path: Path, text_pdf: Path):
    # make read-only — skip on Windows where chmod has limited effect
    text_pdf.chmod(0o444)
    try:
        with pytest.raises(ToolError, match="not writable"):
            preflight_write_access(text_pdf)
    finally:
        text_pdf.chmod(0o644)


def test_preflight_readonly_parent(tmp_path: Path):
    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    p = ro_dir / "f.pdf"
    _make_text_pdf(p, [(50, 80, "hello world this is a sample sentence.")])
    ro_dir.chmod(0o555)
    try:
        with pytest.raises(ToolError, match="parent dir not writable"):
            preflight_write_access(p)
    finally:
        ro_dir.chmod(0o755)


def test_backup_creates_and_does_not_clobber(text_pdf: Path):
    bak = backup_pdf(text_pdf)
    assert bak.exists()
    assert bak.stat().st_size == text_pdf.stat().st_size
    # second call must NOT overwrite
    original_mtime = bak.stat().st_mtime
    # modify source so a clobber would change size
    with open(text_pdf, "ab") as f:
        f.write(b"%comment\n")
    bak2 = backup_pdf(text_pdf)
    assert bak2 == bak
    assert bak2.stat().st_mtime == original_mtime


def test_backup_missing_source(tmp_path: Path):
    with pytest.raises(ToolError, match="source not found"):
        backup_pdf(tmp_path / "nope.pdf")


def test_has_text_layer_true(text_pdf: Path):
    assert has_text_layer(text_pdf) is True


def test_has_text_layer_false(scanned_pdf: Path):
    assert has_text_layer(scanned_pdf) is False


# ---------------------------------------------------------------------------
# US-003 — Placement: candidates, uniqueness gate, word-index, region
# ---------------------------------------------------------------------------


def _open(p: Path):
    return pymupdf.open(str(p))


def test_plain_fi_placement_no_religation_needed(text_pdf: Path):
    doc = _open(text_pdf)
    try:
        spec = AnnotationSpec(
            quote="efficient method outperforms",
            dimension="evidence",
            comment="实证证据。",
        )
        reason = _place_single_annotation(doc, spec)
        assert reason is None
        # verify color stored (held page)
        page = doc[0]
        ours = [a for a in (page.annots() or [])
                if (a.info or {}).get("title") == ZOTPILOT_MARKER]
        assert len(ours) == 1
        stroke = ours[0].colors.get("stroke")
        assert stroke is not None
        for a, b in zip(stroke, DIMENSION_RGB["evidence"]):
            assert abs(a - b) < 0.02
    finally:
        doc.close()


def test_repeated_phrase_ambiguous_multi_match(repeated_pdf: Path):
    doc = _open(repeated_pdf)
    try:
        spec = AnnotationSpec(
            quote="Repeated marker phrase here",
            dimension="thesis",
            comment="x",
        )
        reason = _place_single_annotation(doc, spec)
        assert reason == "ambiguous_multi_match"
        # nothing placed
        annots = list(doc[0].annots() or [])
        assert all(
            (a.info or {}).get("title") != ZOTPILOT_MARKER for a in annots
        )
    finally:
        doc.close()


def test_too_short_quote(text_pdf: Path):
    doc = _open(text_pdf)
    try:
        spec = AnnotationSpec(quote="short", dimension="thesis", comment="c")
        assert _place_single_annotation(doc, spec) == "too_short"
    finally:
        doc.close()


def test_no_match_returns_no_match(text_pdf: Path):
    doc = _open(text_pdf)
    try:
        spec = AnnotationSpec(
            quote="this text definitely is not present anywhere here",
            dimension="thesis",
            comment="c",
        )
        assert _place_single_annotation(doc, spec) == "no_match"
    finally:
        doc.close()


def test_word_index_fallback_de_hyphenated(hyphenated_pdf: Path):
    doc = _open(hyphenated_pdf)
    try:
        spec = AnnotationSpec(
            quote="propose a new method that improves",
            dimension="method",
            comment="方法",
        )
        reason = _place_single_annotation(doc, spec)
        assert reason is None
        page = doc[0]
        ours = [
            a for a in (page.annots() or [])
            if (a.info or {}).get("title") == ZOTPILOT_MARKER
        ]
        assert len(ours) == 1
        # multi-line: should cover at least 2 y-rows
        y0 = float(ours[0].rect.y0)
        y1 = float(ours[0].rect.y1)
        assert y1 - y0 > 10  # rough multi-line check
    finally:
        doc.close()


def test_region_annotation_offset_and_marker_round_trip(text_pdf: Path):
    """§7.10 #1: region icon offset = (max(0.0, x0-16), y0)."""
    out = text_pdf.with_suffix(".out.pdf")
    doc = _open(text_pdf)
    try:
        reason = _place_region_annotation(
            doc, page=1, bbox=(100.0, 80.0, 200.0, 120.0),
            comment="图说明", subtype="figure",
        )
        assert reason is None
        page0 = doc[0]
        ours = [a for a in (page0.annots() or [])
                if (a.info or {}).get("title") == ZOTPILOT_MARKER]
        assert len(ours) == 1
        rx0 = float(ours[0].rect.x0)
        ry0 = float(ours[0].rect.y0)
        # icon should be at left gutter: x0 - 16 = 84
        assert abs(rx0 - 84.0) < 4.0
        assert abs(ry0 - 80.0) < 4.0
        doc.save(str(out), garbage=3, deflate=True)
    finally:
        doc.close()
    d2 = _open(out)
    try:
        a = list(d2[0].annots() or [])[0]
        info = a.info or {}
        assert info.get("title") == ZOTPILOT_MARKER
        assert "图说明" in info.get("content", "")
    finally:
        d2.close()


def test_region_page_one_based_to_zero_based(tmp_path: Path):
    """page=2 must land on doc[1], not doc[2]."""
    p = tmp_path / "multi.pdf"
    _make_multipage_pdf(
        p,
        [
            [(50, 80, "Page one has its own content.")],
            [(50, 80, "Page two is the target page.")],
            [(50, 80, "Page three contains other text.")],
        ],
    )
    doc = _open(p)
    try:
        reason = _place_region_annotation(
            doc, page=2, bbox=(100.0, 80.0, 200.0, 120.0),
            comment="第二页",
        )
        assert reason is None
        # doc[1] has our annot; doc[0]/doc[2] do not
        for pno, expected in [(0, 0), (1, 1), (2, 0)]:
            ours = [
                a for a in (doc[pno].annots() or [])
                if (a.info or {}).get("title") == ZOTPILOT_MARKER
            ]
            assert len(ours) == expected, f"page {pno}: {len(ours)} != {expected}"
    finally:
        doc.close()


def test_region_page_out_of_range(text_pdf: Path):
    doc = _open(text_pdf)
    try:
        assert _place_region_annotation(
            doc, page=999, bbox=(0.0, 0.0, 10.0, 10.0), comment="x"
        ) == "page_out_of_range"
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# US-004 — clear / verify / overview / orchestrator
# ---------------------------------------------------------------------------


def test_clear_zotpilot_annotations_snapshot(text_pdf: Path):
    doc = _open(text_pdf)
    try:
        # seed 3 marker annots + 1 foreign annot
        page = doc[0]
        quads = page.search_for("efficient method outperforms", quads=True)
        for _ in range(3):
            a = page.add_highlight_annot(quads)
            info = a.info
            info["title"] = ZOTPILOT_MARKER
            a.set_info(info)
            a.update()
        # foreign
        fa = page.add_text_annot((300, 300), "foreign note")
        fi = fa.info
        fi["title"] = "Other tool"
        fa.set_info(fi)
        fa.update()
        removed = clear_zotpilot_annotations(doc)
        assert removed == 3
        remaining = list(doc[0].annots() or [])
        assert len(remaining) == 1
        assert (remaining[0].info or {}).get("title") == "Other tool"
    finally:
        doc.close()


def test_build_overview_text_under_cap():
    overview = {
        "thesis": "Transformers scale linearly with data.",
        "skeleton": {
            "question": "Can attention replace recurrence?",
            "claim": "Yes, with proper masking.",
            "evidence": "Benchmark improvements on WMT.",
            "rebuttal": "Compute cost is higher.",
            "conclusion": "Attention is all you need.",
        },
        "strongest": "Empirical SOTA across translation.",
        "weakest": "No long-context evaluation.",
    }
    t = build_overview_text(overview)
    assert "核心论点" in t and "问题" in t and "结论" in t
    assert len(t.encode("utf-8")) <= 2000


def test_place_overview_note(text_pdf: Path):
    doc = _open(text_pdf)
    try:
        ok = _place_overview_note(doc, {"thesis": "核心论点测试 CJK content."})
        assert ok is True
        # marker present on page 0
        annots = list(doc[0].annots() or [])
        ours = [a for a in annots if (a.info or {}).get("title") == ZOTPILOT_MARKER]
        assert len(ours) == 1
        assert "核心论点" in (ours[0].info or {}).get("content", "")
    finally:
        doc.close()


def test_verify_passes_on_clean_write(text_pdf: Path):
    spec = AnnotationSpec(
        quote="efficient method outperforms",
        dimension="evidence", comment="评论",
    )
    report = annotate_pdf_file(text_pdf, [spec], {"thesis": "thesis test"})
    assert report.verified is True
    assert report.verification_details["annot_count_match"]
    assert report.verification_details["warnings_empty"]
    assert report.verification_details["not_repaired"]
    assert report.overview_placed is True
    assert len(report.placed) == 1


def test_verify_fails_on_truncation(text_pdf: Path, tmp_path: Path):
    # Produce a valid annotated file then truncate it
    spec = AnnotationSpec(
        quote="efficient method outperforms",
        dimension="evidence", comment="c",
    )
    annotate_pdf_file(text_pdf, [spec], None)
    # Now truncate last 100 bytes
    data = text_pdf.read_bytes()
    text_pdf.write_bytes(data[:-100])
    v = verify_annotated_pdf(text_pdf, expected_marker_count=1)
    # Truncation triggers is_repaired True OR warnings non-empty OR count mismatch
    assert v["verified"] is False


def test_orchestrator_end_to_end_basic(text_pdf: Path):
    spec = AnnotationSpec(
        quote="final results show consistent improvements",
        dimension="thesis",
        comment="导读评论。",
    )
    report = annotate_pdf_file(
        text_pdf, [spec], {"thesis": "测试论点", "skeleton": {"claim": "claim"}}
    )
    assert isinstance(report, PlacementReport)
    assert report.verified is True
    bak = text_pdf.with_suffix(text_pdf.suffix + ".ztpbak")
    assert bak.exists()
    # tmp/out cleaned up
    assert not text_pdf.with_suffix(text_pdf.suffix + ".ztptmp").exists()
    assert not text_pdf.with_suffix(text_pdf.suffix + ".ztpout").exists()


def test_orchestrator_scanned_guard(scanned_pdf: Path):
    spec = AnnotationSpec(quote="x" * 14, dimension="thesis", comment="c")
    with pytest.raises(ScannedPdfError):
        annotate_pdf_file(scanned_pdf, [spec], None)
    # no .ztpbak created either way (preflight passes but scanned_guard fires after)
    # We do not assert .ztpbak absence because backup happens AFTER scanned guard.


def test_orchestrator_rollback_preserves_backup(text_pdf: Path, monkeypatch):
    """Inject a save fault — original must be restored AND .ztpbak preserved."""
    spec = AnnotationSpec(
        quote="efficient method outperforms",
        dimension="evidence", comment="c",
    )
    original_bytes = text_pdf.read_bytes()

    calls = {"n": 0}

    def boom_save(self, *args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(pymupdf.Document, "save", boom_save)
    with pytest.raises(ToolError):
        annotate_pdf_file(text_pdf, [spec], None)
    # original restored
    assert text_pdf.read_bytes() == original_bytes
    # .ztpbak preserved
    bak = text_pdf.with_suffix(text_pdf.suffix + ".ztpbak")
    assert bak.exists()
    assert bak.read_bytes() == original_bytes


def test_orchestrator_rollback_uses_prerun_snapshot_not_stale_backup(
    text_pdf: Path, monkeypatch
):
    """A failed RE-RUN must restore the pre-run state (pristine + run-A
    annotations), NOT the stale .ztpbak from run A (pristine only). Guards the
    data-loss bug where rollback restored from a stale backup."""
    spec = AnnotationSpec(
        quote="efficient method outperforms", dimension="evidence", comment="c",
    )
    pristine = text_pdf.read_bytes()

    # Run A succeeds: .ztpbak becomes the pristine archive; live PDF gets annots.
    rep_a = annotate_pdf_file(text_pdf, [spec], None)
    assert rep_a.verified
    state_after_a = text_pdf.read_bytes()
    assert state_after_a != pristine  # annotations actually written
    bak = text_pdf.with_suffix(text_pdf.suffix + ".ztpbak")
    assert bak.read_bytes() == pristine

    # Run B fails mid-save: must roll back to state_after_a (the pre-run-B
    # snapshot), not to the stale pristine .ztpbak.
    def boom_save(self, *a, **k):
        raise RuntimeError("disk full mid re-run")

    monkeypatch.setattr(pymupdf.Document, "save", boom_save)
    with pytest.raises(ToolError):
        annotate_pdf_file(text_pdf, [spec], None)
    assert text_pdf.read_bytes() == state_after_a  # pre-run state restored
    assert bak.read_bytes() == pristine  # .ztpbak never consumed


def test_orchestrator_handles_space_and_unicode_path(tmp_path: Path):
    """Core write path works when the PDF filename has a space + non-ASCII char
    (cross-platform path handling via str(Path) for open/replace/copy2)."""
    awkward = tmp_path / "a paper 论文 v2.pdf"
    _make_text_pdf(
        awkward,
        [
            (50, 80, "Our efficient method outperforms the baseline clearly."),
            (50, 120, "The final results show consistent gains across tasks."),
        ],
    )
    spec = AnnotationSpec(
        quote="efficient method outperforms", dimension="evidence", comment="导读",
    )
    report = annotate_pdf_file(awkward, [spec], None)
    assert report.verified is True
    bak = awkward.with_suffix(awkward.suffix + ".ztpbak")
    assert bak.exists()
    # sibling temp files cleaned up
    for suffix in (".ztptmp", ".ztpout", ".ztptmp_restore"):
        assert not awkward.with_suffix(awkward.suffix + suffix).exists()


def test_orchestrator_swap_lock_raises_actionable_error(text_pdf: Path, monkeypatch):
    """If os.replace fails at the swap (e.g. the PDF is open/locked in Zotero on
    Windows), annotate_pdf_file surfaces an actionable ToolError and leaves the
    original intact (restored from the .ztptmp pre-run snapshot)."""
    import os as _os

    spec = AnnotationSpec(
        quote="efficient method outperforms", dimension="evidence", comment="c",
    )
    original = text_pdf.read_bytes()
    real_replace = _os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:  # the swap out->original
            raise OSError("simulated WinError 32: file in use")
        return real_replace(src, dst, *a, **k)  # rollback restore succeeds

    monkeypatch.setattr("zotpilot.pdf.annotator.os.replace", flaky_replace)
    with pytest.raises(ToolError, match="open in Zotero|close it|replace the original"):
        annotate_pdf_file(text_pdf, [spec], None)
    # original intact (rollback restored from the pre-run snapshot)
    assert text_pdf.read_bytes() == original
    bak = text_pdf.with_suffix(text_pdf.suffix + ".ztpbak")
    assert bak.exists()


def test_orchestrator_transient_temps_are_token_scoped_and_cleaned(text_pdf: Path, monkeypatch):
    """Transient temp files carry a per-run token (so concurrent runs on the same
    PDF don't collide) and are fully cleaned up; only the stable .ztpbak remains."""
    import shutil as _shutil

    spec = AnnotationSpec(
        quote="efficient method outperforms", dimension="evidence", comment="c",
    )
    real_copy2 = _shutil.copy2
    seen: dict[str, str] = {}

    def recording_copy2(src, dst, *a, **k):
        d = str(dst)
        if d.endswith(".ztptmp"):
            seen["tmp"] = d
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr("zotpilot.pdf.annotator.shutil.copy2", recording_copy2)
    report = annotate_pdf_file(text_pdf, [spec], None)
    assert report.verified is True

    # the work-copy name is token-scoped, not the bare ".ztptmp"
    assert seen["tmp"].endswith(".ztptmp")
    assert not seen["tmp"].endswith(text_pdf.suffix + ".ztptmp")  # has a token segment

    # no transient files left behind; only the stable .ztpbak remains
    residue = list(text_pdf.parent.glob("*.ztptmp")) + \
        list(text_pdf.parent.glob("*.ztpout")) + \
        list(text_pdf.parent.glob("*.ztptmp_restore"))
    assert residue == []
    assert text_pdf.with_suffix(text_pdf.suffix + ".ztpbak").exists()


def test_orchestrator_idempotent_file_size_bounded(text_pdf: Path):
    """3x re-run → file size within 1.05x of single-annotated size."""
    spec = AnnotationSpec(
        quote="efficient method outperforms",
        dimension="evidence", comment="c",
    )
    overview = {"thesis": "iter"}
    annotate_pdf_file(text_pdf, [spec], overview)
    size_1 = text_pdf.stat().st_size
    annotate_pdf_file(text_pdf, [spec], overview)
    annotate_pdf_file(text_pdf, [spec], overview)
    size_3 = text_pdf.stat().st_size
    assert size_3 <= size_1 * 1.05, f"size grew {size_1} -> {size_3}"


# ---------------------------------------------------------------------------
# US-005 — §7.11 smart-merge with existing user annotations
# ---------------------------------------------------------------------------


def test_read_existing_annotations_excludes_marker(text_pdf: Path):
    doc = _open(text_pdf)
    try:
        page = doc[0]
        # 1 foreign highlight, 1 foreign sticky, 1 marker highlight
        q = page.search_for("efficient method outperforms", quads=True)
        f1 = page.add_highlight_annot(q)
        i = f1.info
        i["title"] = "User"
        i["content"] = "user highlight"
        f1.set_info(i)
        f1.update()
        f2 = page.add_text_annot((300, 300), "user sticky note")
        i = f2.info
        i["title"] = "User"
        f2.set_info(i)
        f2.update()
        m = page.add_highlight_annot(q)
        i = m.info
        i["title"] = ZOTPILOT_MARKER
        m.set_info(i)
        m.update()
        ex = read_existing_annotations(doc)
        assert len(ex) == 2
        titles = [(e.kind, e.content) for e in ex]
        assert any("user highlight" in c for _, c in titles)
        assert any("user sticky" in c for _, c in titles)
    finally:
        doc.close()


def test_iou_gate_skips_when_user_already_annotated(text_pdf: Path):
    doc = _open(text_pdf)
    try:
        page = doc[0]
        q = page.search_for("efficient method outperforms", quads=True)
        fa = page.add_highlight_annot(q)
        i = fa.info
        i["title"] = "User"
        fa.set_info(i)
        fa.update()
        existing = read_existing_annotations(doc)
        spec = AnnotationSpec(
            quote="efficient method outperforms",
            dimension="evidence", comment="x",
        )
        reason = _place_single_annotation(doc, spec, existing=existing)
        assert reason == "user_already_annotated"
    finally:
        doc.close()


def test_iou_low_overlap_still_places(text_pdf: Path):
    """A tangential foreign rect (IoU < 0.5) must not block placement."""
    doc = _open(text_pdf)
    try:
        # simulate a tiny far-away foreign rect via a synthetic ExistingAnnot
        existing = [ExistingAnnot(
            page_num=1, kind="highlight",
            rect=(0.0, 0.0, 5.0, 5.0), color=None, content="", comment="",
        )]
        spec = AnnotationSpec(
            quote="efficient method outperforms",
            dimension="evidence", comment="x",
        )
        reason = _place_single_annotation(doc, spec, existing=existing)
        assert reason is None
    finally:
        doc.close()


def test_region_neighborhood_offsets(text_pdf: Path):
    """A foreign rect in the left gutter should push the region offset elsewhere."""
    doc = _open(text_pdf)
    try:
        # plant a foreign rect at the left-gutter point (x0-16=84, y0=80)
        existing = [ExistingAnnot(
            page_num=1, kind="text",
            rect=(80.0, 75.0, 90.0, 90.0), color=None, content="", comment="",
        )]
        reason = _place_region_annotation(
            doc, page=1, bbox=(100.0, 80.0, 200.0, 120.0),
            comment="c", existing=existing,
        )
        assert reason is None
        # the placed annot should NOT be in the left gutter — its x0 should be
        # somewhere other than ~84
        page0 = doc[0]
        a = [x for x in (page0.annots() or [])
             if (x.info or {}).get("title") == ZOTPILOT_MARKER][0]
        x0_val = float(a.rect.x0)
        assert not (82.0 <= x0_val <= 86.0)
    finally:
        doc.close()


def test_region_all_blocked_returns_region_clustered(text_pdf: Path):
    doc = _open(text_pdf)
    try:
        # cover the entire page so every candidate point is blocked
        existing = [ExistingAnnot(
            page_num=1, kind="text",
            rect=(0.0, 0.0, 1000.0, 1000.0), color=None, content="", comment="",
        )]
        reason = _place_region_annotation(
            doc, page=1, bbox=(100.0, 80.0, 200.0, 120.0),
            comment="c", existing=existing,
        )
        assert reason == "region_clustered"
    finally:
        doc.close()


def test_orchestrator_preserves_foreign_count(text_pdf: Path):
    """Foreign annot count must be identical before/after a successful run."""
    # seed a foreign highlight on the source PDF first
    doc = _open(text_pdf)
    page = doc[0]
    q = page.search_for("final results show consistent improvements", quads=True)
    fa = page.add_highlight_annot(q)
    i = fa.info
    i["title"] = "User"
    i["content"] = "user"
    fa.set_info(i)
    fa.update()
    doc.save(str(text_pdf), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    doc.close()

    spec = AnnotationSpec(
        quote="efficient method outperforms",
        dimension="evidence", comment="c",
    )
    report = annotate_pdf_file(text_pdf, [spec], {"thesis": "t"})
    assert report.verified is True
    # reopen and count foreign annots
    d2 = _open(text_pdf)
    try:
        foreign = read_existing_annotations(d2)
    finally:
        d2.close()
    assert len(foreign) == 1
    assert foreign[0].content.startswith("user") or foreign[0].comment.startswith("user")


# ---------------------------------------------------------------------------
# US-006 — coverage / smoke / extras
# ---------------------------------------------------------------------------


def test_coverage_report_shape(text_pdf: Path):
    specs = [
        AnnotationSpec(
            quote="efficient method outperforms",
            dimension="evidence", comment="c", subtype="dim",
        ),
        AnnotationSpec(
            quote="final results show consistent improvements",
            dimension="thesis", comment="c", subtype="dim",
        ),
    ]
    report = annotate_pdf_file(text_pdf, specs, {"thesis": "t"})
    assert "placed_counts" in report.coverage
    assert "unplaced_counts" in report.coverage
    assert "respected" in report.coverage
    assert report.coverage["placed_counts"].get("dim") == 2


def test_verify_returns_error_on_missing_file(tmp_path: Path):
    v = verify_annotated_pdf(tmp_path / "nope.pdf", expected_marker_count=0)
    assert v["verified"] is False


def test_normalize_punctuation_spacing():
    # page_texts (pymupdf4llm) spaces out punctuation; the PDF text layer attaches it.
    assert normalize_quote_for_pdf("world models , internal models") == "world models, internal models"
    assert normalize_quote_for_pdf("predict y from x .") == "predict y from x."
    assert normalize_quote_for_pdf("depends on x , y , and z") == "depends on x, y, and z"
    assert normalize_quote_for_pdf("function F ( x, y ) that") == "function F (x, y) that"
    assert normalize_quote_for_pdf("the (JEPA) .") == "the (JEPA)."
    # a normal prose parenthetical KEEPS its leading space (we only fix inner spacing,
    # never strip space before "(" — that would break "architecture (JEPA)")
    assert normalize_quote_for_pdf("the model (JEPA) here") == "the model (JEPA) here"
    # normal prose and snake_case untouched
    assert normalize_quote_for_pdf("self_supervised learning here") == "self_supervised learning here"


def test_normalize_empty_string():
    assert normalize_quote_for_pdf("") == ""
    assert re_ligate_quote("") == ""


def _pdf_with_text(blocks: list[tuple[float, str]]) -> pymupdf.Document:
    """1-page in-memory PDF; each block = (textbox_width, text). A narrow width
    forces long strings to wrap across multiple lines."""
    doc = pymupdf.open()
    page = doc.new_page(width=320, height=480)
    y = 30.0
    for width, text in blocks:
        page.insert_textbox(pymupdf.Rect(20, y, 20 + width, y + 190), text, fontsize=11)
        y += 210.0
    return doc


def test_multiline_quote_is_placed_not_ambiguous():
    # Regression: page.search_for(quads=True) returns one quad PER WRAPPED LINE,
    # so a single sentence spanning 2+ lines must NOT be rejected as
    # ambiguous_multi_match. Ambiguity is judged by true occurrence count.
    sentence = (
        "the joint embedding predictive architecture learns useful "
        "representations from data without any contrastive training"
    )
    doc = _pdf_with_text([(170.0, sentence)])  # narrow box -> wraps to >1 line
    spec = AnnotationSpec(
        quote=sentence, dimension="thesis", comment="x",
        page_hint=1, kind="highlight", subtype="dim",
    )
    reason = _place_single_annotation(doc, spec)
    doc.close()
    assert reason is None, f"multi-line quote should place, got {reason!r}"


def test_genuinely_duplicated_quote_is_ambiguous():
    # Flip side: a quote that truly occurs twice MUST still be ambiguous.
    phrase = "the world model predicts the next state of the system"
    doc = _pdf_with_text([(280.0, phrase), (280.0, phrase)])
    spec = AnnotationSpec(
        quote=phrase, dimension="concept", comment="x",
        page_hint=1, kind="highlight", subtype="dim",
    )
    reason = _place_single_annotation(doc, spec)
    doc.close()
    assert reason == "ambiguous_multi_match", f"expected ambiguous, got {reason!r}"
