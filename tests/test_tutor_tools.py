"""Integration tests for the ztp-tutor MCP tools (Phase 2).

Covers US-007 in .omc/prd.json — fuzzy resolve, disambiguation, persona slice,
existing-annot scan, annotate_pdf end-to-end, idempotency, rollback, token caps,
and tool registration.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from zotpilot.pdf import annotator as ann
from zotpilot.state import ToolError
from zotpilot.tools import tutor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeItem:
    item_key: str
    title: str
    authors: str
    year: int | None
    pdf_path: Path | None
    publication: str = ""
    doi: str = ""
    tags: str = ""
    collections: str = ""


class FakeZoteroClient:
    def __init__(self, items: dict[str, FakeItem], search_results: list[Any]):
        self._items = items
        self._search_results = search_results
        self.advanced_search_calls: list[dict] = []

    def get_item(self, item_key: str):
        return self._items.get(item_key)

    def advanced_search(self, *, conditions, match, sort_by, sort_dir, limit):
        self.advanced_search_calls.append({
            "conditions": conditions,
            "match": match,
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            "limit": limit,
        })
        return list(self._search_results)


def _make_text_pdf(path: Path, pages: list[list[tuple[float, float, str]]]) -> None:
    doc = pymupdf.open()
    for plines in pages:
        page = doc.new_page()
        for x, y, text in plines:
            page.insert_text((x, y), text)
    doc.save(str(path))
    doc.close()


@pytest.fixture
def fixture_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "paper.pdf"
    _make_text_pdf(
        p,
        [
            [
                (50, 80, "Our efficient method outperforms the baseline by a wide margin."),
                (50, 120, "The final results show consistent improvements across tasks."),
                (50, 160, "We propose a novel architecture that handles long contexts."),
            ],
            [
                (50, 80, "We evaluated on five benchmark datasets covering diverse domains."),
                (50, 120, "Comparisons demonstrate substantial gains over prior work."),
            ],
        ],
    )
    return p


@pytest.fixture
def fake_item(fixture_pdf: Path) -> FakeItem:
    return FakeItem(
        item_key="ABCD1234",
        title="An Efficient Method for Long Context",
        authors="Smith, J. et al.",
        year=2024,
        pdf_path=fixture_pdf,
    )


@pytest.fixture
def single_match_client(fake_item):
    return FakeZoteroClient(
        items={fake_item.item_key: fake_item},
        search_results=[
            {
                "item_key": fake_item.item_key,
                "title": fake_item.title,
                "authors": fake_item.authors,
                "year": fake_item.year,
            }
        ],
    )


@pytest.fixture
def install_client(monkeypatch):
    """Helper factory: install a FakeZoteroClient into tutor._get_zotero."""
    def _install(client):
        monkeypatch.setattr(tutor, "_get_zotero", lambda: client)
    return _install


# ---------------------------------------------------------------------------
# get_paper_for_tutor — resolution
# ---------------------------------------------------------------------------


def test_get_paper_for_tutor_single_match_returns_full_payload(
    install_client, single_match_client, fake_item
):
    install_client(single_match_client)
    out = tutor.get_paper_for_tutor("efficient method")
    assert out["doc_id"] == fake_item.item_key
    assert out["pdf_path"] == str(fake_item.pdf_path)
    for key in (
        "page_texts",
        "sectioned_text",
        "figures",
        "tables",
        "tables_on_page",
        "persona",
        "existing_annotations",
    ):
        assert key in out
    # page_texts have 1-based page_num
    assert out["page_texts"]
    assert out["page_texts"][0]["page_num"] == 1
    assert all("text" in p for p in out["page_texts"])
    # existing_annotations is a list (empty for a fresh PDF)
    assert isinstance(out["existing_annotations"], list)


def test_get_paper_for_tutor_item_key_path(
    install_client, single_match_client, fake_item
):
    install_client(single_match_client)
    out = tutor.get_paper_for_tutor(fake_item.item_key)
    assert out["doc_id"] == fake_item.item_key
    # no advanced_search call when item_key matches directly
    assert single_match_client.advanced_search_calls == []


def test_get_paper_for_tutor_multiple_matches_disambiguation(
    install_client, fake_item
):
    other = {
        "item_key": "OTHER567",
        "title": "Another paper",
        "authors": "Doe, J.",
        "year": 2023,
    }
    client = FakeZoteroClient(
        items={fake_item.item_key: fake_item},
        search_results=[
            {
                "item_key": fake_item.item_key,
                "title": fake_item.title,
                "authors": fake_item.authors,
                "year": fake_item.year,
            },
            other,
        ],
    )
    install_client(client)
    out = tutor.get_paper_for_tutor("paper")
    assert out["needs_disambiguation"] is True
    assert len(out["candidates"]) == 2
    assert {c["doc_id"] for c in out["candidates"]} == {
        fake_item.item_key,
        "OTHER567",
    }


def test_get_paper_for_tutor_no_matches_raises(install_client):
    client = FakeZoteroClient(items={}, search_results=[])
    install_client(client)
    with pytest.raises(ToolError, match="no Zotero item matches"):
        tutor.get_paper_for_tutor("nonexistent paper")


def test_get_paper_for_tutor_missing_pdf_raises(install_client, fake_item):
    fake_item_no_pdf = FakeItem(
        item_key=fake_item.item_key,
        title=fake_item.title,
        authors=fake_item.authors,
        year=fake_item.year,
        pdf_path=None,
    )
    client = FakeZoteroClient(
        items={fake_item_no_pdf.item_key: fake_item_no_pdf},
        search_results=[
            {
                "item_key": fake_item_no_pdf.item_key,
                "title": fake_item_no_pdf.title,
                "authors": fake_item_no_pdf.authors,
                "year": fake_item_no_pdf.year,
            }
        ],
    )
    install_client(client)
    with pytest.raises(ToolError, match="no attached PDF"):
        tutor.get_paper_for_tutor(fake_item_no_pdf.item_key)


def test_get_paper_for_tutor_empty_input_raises(install_client, single_match_client):
    install_client(single_match_client)
    with pytest.raises(ToolError, match="required"):
        tutor.get_paper_for_tutor("   ")


# ---------------------------------------------------------------------------
# Persona slicing
# ---------------------------------------------------------------------------


def _set_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    # also set USERPROFILE for Windows portability inside CI
    monkeypatch.setenv("USERPROFILE", str(home))


def test_persona_returns_section_text(monkeypatch, tmp_path):
    home = tmp_path / "home"
    cfg = home / ".config" / "zotpilot"
    cfg.mkdir(parents=True)
    (cfg / "ZOTPILOT.md").write_text(
        "# ZotPilot Profile\n\n## 阅读画像\n- 英文水平：中等\n- 导读深度：技术细节\n\n## Another section\nfoo\n",
        encoding="utf-8",
    )
    _set_home(monkeypatch, home)
    result = tutor._read_persona()
    assert result is not None
    assert "## 阅读画像" in result
    assert "英文水平" in result
    assert "Another section" not in result


def test_persona_returns_none_when_section_absent(monkeypatch, tmp_path):
    home = tmp_path / "home"
    cfg = home / ".config" / "zotpilot"
    cfg.mkdir(parents=True)
    (cfg / "ZOTPILOT.md").write_text(
        "# ZotPilot Profile\n\n## Other Section\nfoo\n", encoding="utf-8"
    )
    _set_home(monkeypatch, home)
    assert tutor._read_persona() is None


def test_persona_returns_none_when_file_absent(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _set_home(monkeypatch, home)
    assert tutor._read_persona() is None


def test_persona_english_heading_recognized(monkeypatch, tmp_path):
    home = tmp_path / "home"
    cfg = home / ".config" / "zotpilot"
    cfg.mkdir(parents=True)
    (cfg / "ZOTPILOT.md").write_text(
        "# Profile\n\n## Reading Persona\n- English: advanced\n",
        encoding="utf-8",
    )
    _set_home(monkeypatch, home)
    result = tutor._read_persona()
    assert result is not None
    assert "Reading Persona" in result


def test_profile_path_platform_aware_with_legacy_fallback(monkeypatch, tmp_path):
    """config.profile_path: canonical preferred, legacy fallback, else canonical."""
    import zotpilot.config as cfg
    canon_dir = tmp_path / "appdata" / "zotpilot"
    canon_dir.mkdir(parents=True)
    legacy_dir = tmp_path / "home" / ".config" / "zotpilot"
    legacy_dir.mkdir(parents=True)
    monkeypatch.setattr(cfg, "_default_config_dir", lambda: canon_dir)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    canon_md = canon_dir / "ZOTPILOT.md"
    legacy_md = legacy_dir / "ZOTPILOT.md"

    # neither exists -> canonical (for a clean "no profile")
    assert cfg.profile_path() == canon_md
    # only legacy exists -> legacy (existing users keep their file)
    legacy_md.write_text("legacy", encoding="utf-8")
    assert cfg.profile_path() == legacy_md
    # canonical exists -> canonical preferred
    canon_md.write_text("canon", encoding="utf-8")
    assert cfg.profile_path() == canon_md


def test_save_reading_persona_creates_then_read_detects(monkeypatch, tmp_path):
    """save_reading_persona writes a section that _read_persona detects next run."""
    home = tmp_path / "home"
    cfg = home / ".config" / "zotpilot"
    cfg.mkdir(parents=True)
    (cfg / "ZOTPILOT.md").write_text("# Profile\n\n## 打分指南\n\nkeep me\n", encoding="utf-8")
    _set_home(monkeypatch, home)

    out = tutor.save_reading_persona(persona_text="- 英文水平：入门\n- 导读深度：速览")
    assert out["saved"] is True
    assert out["action"] == "created"
    # the unrelated section is preserved
    body = (cfg / "ZOTPILOT.md").read_text(encoding="utf-8")
    assert "## 打分指南" in body and "keep me" in body
    # and the persona round-trips through the reader
    persona = tutor._read_persona()
    assert persona is not None and "英文水平：入门" in persona


def test_save_reading_persona_replaces_existing(monkeypatch, tmp_path):
    home = tmp_path / "home"
    cfg = home / ".config" / "zotpilot"
    cfg.mkdir(parents=True)
    (cfg / "ZOTPILOT.md").write_text(
        "# P\n\n## 阅读画像 (Reading Persona)\n\n- 英文水平：高级\n\n## 别的\n\nkeep\n",
        encoding="utf-8",
    )
    _set_home(monkeypatch, home)
    out = tutor.save_reading_persona(persona_text="- 英文水平：入门")
    assert out["action"] == "replaced"
    persona = tutor._read_persona()
    assert "入门" in persona and "高级" not in persona
    body = (cfg / "ZOTPILOT.md").read_text(encoding="utf-8")
    assert "## 别的" in body and "keep" in body


def test_save_reading_persona_empty_raises(monkeypatch, tmp_path):
    _set_home(monkeypatch, tmp_path)
    with pytest.raises(ToolError, match="persona_text is required"):
        tutor.save_reading_persona(persona_text="   ")


# ---------------------------------------------------------------------------
# annotate_pdf — end to end on fixture
# ---------------------------------------------------------------------------


def _basic_annotations() -> list[dict]:
    return [
        {
            "quote": "Our efficient method outperforms the baseline by a wide margin.",
            "dimension": "thesis",
            "comment": "核心论点：效率方法显著优于基线。",
            "page_hint": 1,
        },
        {
            "quote": "The final results show consistent improvements across tasks.",
            "dimension": "evidence",
            "comment": "证据：跨任务一致提升。",
            "page_hint": 1,
        },
        {
            "quote": "We evaluated on five benchmark datasets covering diverse domains.",
            "dimension": "method",
            "comment": "方法：五个基准数据集。",
            "page_hint": 2,
        },
    ]


def _basic_overview() -> dict:
    return {
        "thesis": "一种更高效的方法",
        "skeleton": {
            "question": "如何提升长上下文效率",
            "claim": "新方法效果更好",
            "evidence": "跨基准评估",
            "rebuttal": "无明显反驳",
            "conclusion": "方法有效",
        },
        "strongest": "实证充分",
        "weakest": "数据集有限",
    }


def test_annotate_pdf_end_to_end(install_client, single_match_client, fake_item):
    install_client(single_match_client)
    before_size = fake_item.pdf_path.stat().st_size
    result = tutor.annotate_pdf(
        doc_id=fake_item.item_key,
        annotations=_basic_annotations(),
        overview=_basic_overview(),
    )
    assert result["verified"] is True
    assert result["overview_placed"] is True
    backup = Path(result["backup_path"])
    assert backup.exists()
    assert backup.stat().st_size == before_size
    # placed list
    assert len(result["placed"]) == 3
    # summary present
    assert "导读完成" in result["summary"]

    # reopen and check marker count
    doc = pymupdf.open(str(fake_item.pdf_path))
    try:
        marker_n = 0
        for i in range(doc.page_count):
            for a in doc[i].annots() or []:
                title = (a.info or {}).get("title", "") or ""
                if title.startswith(ann.ZOTPILOT_MARKER):
                    marker_n += 1
    finally:
        doc.close()
    assert marker_n == len(result["placed"]) + 1  # +1 overview


def test_annotate_pdf_via_specs_path(install_client, single_match_client, fake_item, tmp_path):
    """specs_path JSON file is read in lieu of inline annotations/overview
    (keeps the approval prompt compact)."""
    install_client(single_match_client)
    import json as _json
    specs = tmp_path / "specs.json"
    specs.write_text(
        _json.dumps({"annotations": _basic_annotations(), "overview": _basic_overview()}),
        encoding="utf-8",
    )
    result = tutor.annotate_pdf(doc_id=fake_item.item_key, specs_path=str(specs))
    assert result["verified"] is True
    assert result["overview_placed"] is True
    assert len(result["placed"]) == 3
    assert "导读完成" in result["summary"]


def test_annotate_pdf_specs_path_takes_precedence_over_inline(
    install_client, single_match_client, fake_item, tmp_path
):
    install_client(single_match_client)
    import json as _json
    specs = tmp_path / "specs.json"
    specs.write_text(
        _json.dumps({"annotations": _basic_annotations(), "overview": _basic_overview()}),
        encoding="utf-8",
    )
    # inline annotations is bogus; specs_path should win and succeed
    result = tutor.annotate_pdf(
        doc_id=fake_item.item_key,
        specs_path=str(specs),
        annotations=[{"bogus": True}],
        overview={},
    )
    assert result["verified"] is True
    assert len(result["placed"]) == 3


def test_annotate_pdf_missing_specs_path_raises(install_client, single_match_client, fake_item):
    install_client(single_match_client)
    with pytest.raises(ToolError, match="specs_path does not exist"):
        tutor.annotate_pdf(doc_id=fake_item.item_key, specs_path="/no/such/specs.json")


def test_annotate_pdf_idempotent_rerun(install_client, single_match_client, fake_item):
    install_client(single_match_client)
    first = tutor.annotate_pdf(
        doc_id=fake_item.item_key,
        annotations=_basic_annotations(),
        overview=_basic_overview(),
    )
    size_after_first = fake_item.pdf_path.stat().st_size
    second = tutor.annotate_pdf(
        doc_id=fake_item.item_key,
        annotations=_basic_annotations(),
        overview=_basic_overview(),
    )
    size_after_second = fake_item.pdf_path.stat().st_size
    assert first["verified"] and second["verified"]
    # file size stable (within 1.05x)
    assert size_after_second <= size_after_first * 1.05
    # marker count matches the second run (prior cleared)
    doc = pymupdf.open(str(fake_item.pdf_path))
    try:
        marker_n = 0
        for i in range(doc.page_count):
            for a in doc[i].annots() or []:
                title = (a.info or {}).get("title", "") or ""
                if title.startswith(ann.ZOTPILOT_MARKER):
                    marker_n += 1
    finally:
        doc.close()
    expected = len(second["placed"]) + (1 if second["overview_placed"] else 0)
    assert marker_n == expected


def test_annotate_pdf_missing_pdf_raises(install_client, fake_item):
    item_no_pdf = FakeItem(
        item_key=fake_item.item_key,
        title=fake_item.title,
        authors=fake_item.authors,
        year=fake_item.year,
        pdf_path=None,
    )
    client = FakeZoteroClient(items={item_no_pdf.item_key: item_no_pdf}, search_results=[])
    install_client(client)
    with pytest.raises(ToolError, match="no attached PDF"):
        tutor.annotate_pdf(
            doc_id=item_no_pdf.item_key,
            annotations=_basic_annotations(),
            overview=_basic_overview(),
        )


def test_annotate_pdf_unknown_item_raises(install_client):
    client = FakeZoteroClient(items={}, search_results=[])
    install_client(client)
    with pytest.raises(ToolError, match="no Zotero item with key"):
        tutor.annotate_pdf(
            doc_id="DOES_NOT_EXIST",
            annotations=_basic_annotations(),
            overview=_basic_overview(),
        )


def test_annotate_pdf_oversized_comment_rejected(
    install_client, single_match_client, fake_item
):
    install_client(single_match_client)
    bad = _basic_annotations()
    bad[0]["comment"] = "y" * 501
    before_size = fake_item.pdf_path.stat().st_size
    with pytest.raises(ToolError, match="comment exceeds"):
        tutor.annotate_pdf(
            doc_id=fake_item.item_key,
            annotations=bad,
            overview=_basic_overview(),
        )
    # PDF untouched
    assert fake_item.pdf_path.stat().st_size == before_size


def test_annotate_pdf_empty_annotations_rejected(
    install_client, single_match_client, fake_item
):
    install_client(single_match_client)
    with pytest.raises(ToolError, match="non-empty list"):
        tutor.annotate_pdf(
            doc_id=fake_item.item_key,
            annotations=[],
            overview=_basic_overview(),
        )


def test_annotate_pdf_missing_required_field(
    install_client, single_match_client, fake_item
):
    install_client(single_match_client)
    with pytest.raises(ToolError, match="missing required field"):
        tutor.annotate_pdf(
            doc_id=fake_item.item_key,
            annotations=[{"quote": "x" * 20, "dimension": "thesis"}],  # no comment
            overview=_basic_overview(),
        )


def test_annotate_pdf_non_consuming_rollback(
    install_client, single_match_client, fake_item, monkeypatch
):
    """Inject a save failure mid-run; original restored and .ztpbak preserved."""
    install_client(single_match_client)
    original_bytes = fake_item.pdf_path.read_bytes()

    real_save = pymupdf.Document.save

    def fail_save(self, *args, **kwargs):  # noqa: ANN001
        raise RuntimeError("simulated save failure")

    monkeypatch.setattr(pymupdf.Document, "save", fail_save)
    try:
        with pytest.raises(ToolError):
            tutor.annotate_pdf(
                doc_id=fake_item.item_key,
                annotations=_basic_annotations(),
                overview=_basic_overview(),
            )
    finally:
        monkeypatch.setattr(pymupdf.Document, "save", real_save)

    # Original restored byte-for-byte
    assert fake_item.pdf_path.read_bytes() == original_bytes
    # Backup preserved
    bak = fake_item.pdf_path.with_suffix(fake_item.pdf_path.suffix + ".ztpbak")
    assert bak.exists()


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_tools_importable_and_callable():
    from zotpilot.tools import tutor as t
    assert callable(t.get_paper_for_tutor)
    assert callable(t.annotate_pdf)


def test_coerce_list_from_json_string():
    assert tutor._coerce_list('[1, 2, 3]') == [1, 2, 3]
    assert tutor._coerce_list("not json") == []
    assert tutor._coerce_list('{"a": 1}') == []
    assert tutor._coerce_list([1, 2]) == [1, 2]


def test_coerce_dict_variants():
    assert tutor._coerce_dict({"a": 1}) == {"a": 1}
    assert tutor._coerce_dict('{"a": 1}') == {"a": 1}
    assert tutor._coerce_dict("bad json") == {}
    assert tutor._coerce_dict('[1, 2]') == {}


def test_trim_sections_drops_references_and_appendix():
    from zotpilot.models import SectionSpan

    full = "INTRO body. REFS body. METHODS body. APP body."
    intro_start = 0
    refs_start = full.index("REFS")
    refs_end = full.index("METHODS")
    app_start = full.index("APP")
    app_end = len(full)
    sections = [
        SectionSpan(label="introduction", char_start=intro_start, char_end=refs_start,
                    heading_text="", confidence=1.0),
        SectionSpan(label="references", char_start=refs_start, char_end=refs_end,
                    heading_text="", confidence=1.0),
        SectionSpan(label="appendix", char_start=app_start, char_end=app_end,
                    heading_text="", confidence=1.0),
    ]
    out = tutor._trim_sections(full, sections, tutor._TRIMMED_SECTION_LABELS)
    assert "REFS" not in out
    assert "APP" not in out
    assert "INTRO" in out
    assert "METHODS" in out


def test_trim_sections_empty_inputs():
    assert tutor._trim_sections("", [], tutor._TRIMMED_SECTION_LABELS) == ""
    assert tutor._trim_sections("body", [], tutor._TRIMMED_SECTION_LABELS) == "body"


def test_spec_from_dict_region_kind_and_bbox():
    spec = tutor._spec_from_dict(
        0,
        {
            "quote": "x" * 14,
            "dimension": "concept",
            "comment": "ok",
            "kind": "region",
            "page": 2,
            "bbox": [10.0, 20.0, 100.0, 200.0],
            "subtype": "figure",
            "page_hint": 2,
        },
    )
    assert spec.kind == "region"
    assert spec.bbox == (10.0, 20.0, 100.0, 200.0)
    assert spec.page == 2
    assert spec.subtype == "figure"


def test_spec_from_dict_invalid_bbox():
    with pytest.raises(ToolError, match="bbox invalid"):
        tutor._spec_from_dict(
            0,
            {
                "quote": "x" * 14,
                "dimension": "thesis",
                "comment": "c",
                "bbox": [1, 2, 3],  # only 3 elements
            },
        )


def test_spec_from_dict_rejects_non_dict():
    with pytest.raises(ToolError, match="must be a dict"):
        tutor._spec_from_dict(0, "not a dict")


def test_annotate_pdf_overview_json_string_accepted(
    install_client, single_match_client, fake_item
):
    """overview passed as a JSON string should be parsed."""
    install_client(single_match_client)
    import json as _json

    out = tutor.annotate_pdf(
        doc_id=fake_item.item_key,
        annotations=_basic_annotations(),
        overview=_json.dumps(_basic_overview()),  # type: ignore[arg-type]
    )
    assert out["verified"] is True


def test_resolve_item_advanced_search_failure(install_client, monkeypatch):
    class FailingClient(FakeZoteroClient):
        def advanced_search(self, **_kw):
            raise RuntimeError("boom")

    install_client(FailingClient(items={}, search_results=[]))
    with pytest.raises(ToolError, match="advanced_search failed"):
        tutor.get_paper_for_tutor("anything")


def test_get_paper_for_tutor_item_key_not_in_db_falls_back(install_client, fake_item):
    """Item-key-shaped string that isn't found falls back to advanced_search."""
    client = FakeZoteroClient(
        items={fake_item.item_key: fake_item},  # different key in store
        search_results=[
            {
                "item_key": fake_item.item_key,
                "title": fake_item.title,
                "authors": fake_item.authors,
                "year": fake_item.year,
            }
        ],
    )
    install_client(client)
    # XYZ99999 is item-key-shaped but not in store; falls back to search
    out = tutor.get_paper_for_tutor("XYZ99999")
    assert out["doc_id"] == fake_item.item_key


def test_existing_annotations_serialized_when_present(
    install_client, single_match_client, fake_item
):
    """Seed a foreign annot, then verify get_paper_for_tutor returns it."""
    # seed
    doc = pymupdf.open(str(fake_item.pdf_path))
    try:
        page = doc[0]
        a = page.add_highlight_annot(pymupdf.Rect(48, 75, 400, 95))
        info = a.info
        info["title"] = "User"
        info["content"] = "user note"
        a.set_info(info)
        a.update()
        doc.save(str(fake_item.pdf_path), incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    finally:
        doc.close()

    install_client(single_match_client)
    out = tutor.get_paper_for_tutor(fake_item.item_key)
    assert len(out["existing_annotations"]) >= 1
    ea = out["existing_annotations"][0]
    assert "page_num" in ea
    assert "kind" in ea
    assert "rect" in ea
    assert isinstance(ea["rect"], list)


# ---------------------------------------------------------------------------
# Multi-strategy resolver (US-R1)
# ---------------------------------------------------------------------------


class PhasedZoteroClient(FakeZoteroClient):
    """Returns different results for the strict (match='all') vs tokenized
    (match='any') advanced_search phases, so the two-strategy resolver can be
    exercised distinctly."""

    def __init__(self, items, strict_results, token_results):
        super().__init__(items=items, search_results=[])
        self._strict_results = strict_results
        self._token_results = token_results

    def advanced_search(self, *, conditions, match, sort_by, sort_dir, limit):
        self.advanced_search_calls.append({
            "conditions": conditions,
            "match": match,
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            "limit": limit,
        })
        return list(self._strict_results if match == "all" else self._token_results)


def test_tokenize_title_drops_stopwords_and_short_tokens():
    toks = tutor._tokenize_title("A Model for the PIV of Flow")
    assert "the" not in toks and "for" not in toks and "of" not in toks
    assert "model" not in toks  # stopword
    assert "piv" in toks and "flow" in toks
    # de-duplicated, order-preserving
    assert tutor._tokenize_title("flow flow flow") == ["flow"]


def test_title_overlap_counts_distinct_tokens():
    tokens = ["piv", "diffusion", "transfer", "velocimetry"]
    assert tutor._title_overlap("PIV diffusion transfer model", tokens) == 3
    assert tutor._title_overlap("unrelated text", tokens) == 0


def test_resolver_dominant_winner_auto_selects_real_paper(install_client):
    """The exact query that failed in the live dry-run must now resolve to the
    PIV-FlowDiffuser paper via tokenized search + dominant-winner auto-select."""
    target = FakeItem(
        item_key="SCLPQXKF",
        title="PIV-FlowDiffuser:Transfer-learning-based denoising diffusion models for PIV",
        authors="Zhu et al.",
        year=2025,
        pdf_path=None,  # resolution only; payload build not exercised here
    )
    noise1 = {
        "item_key": "TWFUDLHY",
        "title": "A Physics-informed Diffusion Model for High-fidelity Flow Field Reconstruction",
        "authors": "Shu et al.",
        "year": 2023,
    }
    noise2 = {
        "item_key": "445UQWFB",
        "title": "Conditional neural field latent diffusion model for turbulence",
        "authors": "Du et al.",
        "year": 2024,
    }
    token_hit = {
        "item_key": "SCLPQXKF",
        "title": target.title,
        "authors": target.authors,
        "year": target.year,
    }
    client = PhasedZoteroClient(
        items={"SCLPQXKF": target},
        strict_results=[],                       # strict full-title contains misses
        token_results=[noise1, token_hit, noise2],
    )
    install_client(client)
    item, candidates = tutor._resolve_item(
        "PIV-FlowDiffuser: Transfer-learning-based denoising diffusion models "
        "for particle image velocimetry"
    )
    assert item is not None
    assert item.item_key == "SCLPQXKF"
    # both phases were attempted
    matches = [c["match"] for c in client.advanced_search_calls]
    assert "all" in matches and "any" in matches


def test_resolver_comparable_matches_disambiguation(install_client):
    """When several candidates share comparable token overlap, no auto-select;
    return them for disambiguation."""
    a = {"item_key": "AAAA1111", "title": "Deep learning flow reconstruction method", "year": 2024}
    b = {"item_key": "BBBB2222", "title": "Deep learning flow reconstruction survey", "year": 2023}
    client = PhasedZoteroClient(
        items={},
        strict_results=[],
        token_results=[a, b],  # both overlap "deep","learning","flow","reconstruction" = 4 each
    )
    install_client(client)
    item, candidates = tutor._resolve_item("deep learning flow reconstruction")
    assert item is None
    assert len(candidates) == 2


def test_resolver_tokenized_empty_raises_no_match(install_client):
    client = PhasedZoteroClient(items={}, strict_results=[], token_results=[])
    install_client(client)
    with pytest.raises(ToolError, match="no Zotero item matches"):
        tutor.get_paper_for_tutor("some unfindable paper title here")


def test_resolver_strict_single_match_resolves(install_client, fake_item):
    """A strict full-title contains hit still resolves directly (Strategy 1)."""
    client = PhasedZoteroClient(
        items={fake_item.item_key: fake_item},
        strict_results=[{
            "item_key": fake_item.item_key,
            "title": fake_item.title,
            "authors": fake_item.authors,
            "year": fake_item.year,
        }],
        token_results=[],
    )
    install_client(client)
    item, candidates = tutor._resolve_item(fake_item.title)
    assert item is not None and item.item_key == fake_item.item_key
    # strict phase alone sufficed; tokenized phase not needed
    assert all(c["match"] == "all" for c in client.advanced_search_calls)
