"""Tests for the vision-results cache (#1): re-runs must not re-pay the vision
API for tables already transcribed from unchanged PDFs."""

from unittest.mock import MagicMock, patch

import pytest

from zotpilot.feature_extraction.vision_cache import VisionResultCache


def _spec(pdf, *, table_id="t1", page=1, bbox=(0.0, 0.0, 10.0, 10.0), caption="cap", raw_text="rt", garbled=False):
    from zotpilot.feature_extraction.vision_api import TableVisionSpec
    return TableVisionSpec(
        table_id=table_id, pdf_path=pdf, page_num=page, bbox=bbox,
        raw_text=raw_text, caption=caption, garbled=garbled,
    )


def _resp(*, label="Table 1", success=True):
    from zotpilot.feature_extraction.vision_extract import AgentResponse
    return AgentResponse(
        headers=["A", "B"], rows=[["1", "2"]], footnotes="fn",
        table_label=label, caption="cap", is_incomplete=False, incomplete_reason="",
        raw_shape=(1, 2), parse_success=success, raw_response='{"x":1}',
        recrop_needed=False, recrop_bbox_pct=None,
    )


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "paper.pdf"
    p.write_bytes(b"%PDF-1.7 fake content for hashing")
    return p


class TestKey:
    def test_same_spec_same_key(self, tmp_path, pdf):
        c = VisionResultCache(tmp_path / "vc")
        assert c.content_key(_spec(pdf), "m|compact") == c.content_key(_spec(pdf), "m|compact")

    def test_table_id_does_not_affect_key(self, tmp_path, pdf):
        c = VisionResultCache(tmp_path / "vc")
        # table_id carries volatile doc_key/index — must NOT change the content key
        assert c.content_key(_spec(pdf, table_id="A"), "v") == c.content_key(_spec(pdf, table_id="B"), "v")

    def test_bbox_caption_variant_and_pdf_change_key(self, tmp_path, pdf):
        c = VisionResultCache(tmp_path / "vc")
        base = c.content_key(_spec(pdf), "v1")
        assert base != c.content_key(_spec(pdf, bbox=(0, 0, 11, 10)), "v1")
        assert base != c.content_key(_spec(pdf, caption="other"), "v1")
        assert base != c.content_key(_spec(pdf), "v2")  # model/prompt variant
        other_pdf = tmp_path / "other.pdf"
        other_pdf.write_bytes(b"%PDF different bytes")
        assert base != c.content_key(_spec(other_pdf), "v1")

    def test_unreadable_pdf_yields_none_key(self, tmp_path):
        c = VisionResultCache(tmp_path / "vc")
        assert c.content_key(_spec(tmp_path / "missing.pdf"), "v") is None


class TestStorage:
    def test_put_get_roundtrip(self, tmp_path, pdf):
        c = VisionResultCache(tmp_path / "vc")
        k = c.content_key(_spec(pdf), "v")
        c.put(k, _resp(label="Table 7"))
        got = c.get(k)
        assert got is not None
        assert got.table_label == "Table 7"
        assert got.headers == ["A", "B"]
        assert got.raw_shape == (1, 2)  # tuple restored, not list
        assert got.parse_success is True

    def test_get_missing_and_none_key(self, tmp_path):
        c = VisionResultCache(tmp_path / "vc")
        assert c.get("nope") is None
        assert c.get(None) is None

    def test_corrupt_entry_is_a_miss_not_a_crash(self, tmp_path, pdf):
        c = VisionResultCache(tmp_path / "vc")
        k = c.content_key(_spec(pdf), "v")
        (tmp_path / "vc" / f"{k}.json").write_text("{ truncated", encoding="utf-8")
        assert c.get(k) is None

    def test_schema_drift_is_a_miss(self, tmp_path, pdf):
        import json
        c = VisionResultCache(tmp_path / "vc")
        k = c.content_key(_spec(pdf), "v")
        (tmp_path / "vc" / f"{k}.json").write_text(json.dumps({"unexpected": "shape"}), encoding="utf-8")
        assert c.get(k) is None  # AgentResponse(**d) TypeError → miss


def _api(cache):
    from zotpilot.feature_extraction.vision_api import VisionAPI
    api = VisionAPI.__new__(VisionAPI)
    api._model = "claude-haiku-4-5-20251001"
    api._prompt_mode = "compact"
    api._max_output_tokens = 1536
    api._result_cache = cache
    api._prepare_table = MagicMock(return_value=[])
    api._build_request = MagicMock(side_effect=lambda spec, images: {"custom_id": f"{spec.table_id}__transcriber"})
    api._submit_and_poll = MagicMock(side_effect=lambda reqs: {r["custom_id"]: "RAW" for r in reqs})
    return api


class TestExtractTablesBatchCaching:
    def test_second_run_hits_cache_no_api_call(self, tmp_path, pdf):
        from zotpilot.feature_extraction import vision_api as va
        cache = VisionResultCache(tmp_path / "vc")
        api = _api(cache)
        specs = [_spec(pdf, table_id="d__t0", page=1), _spec(pdf, table_id="d__t1", page=2)]

        with patch.object(va, "parse_agent_response", return_value=_resp()):
            first = api.extract_tables_batch(specs)
        assert api._submit_and_poll.call_count == 1
        assert all(r.parse_success for r in first)

        # Second run: fresh API mock that would explode if called.
        api._submit_and_poll = MagicMock(side_effect=AssertionError("API must not be called on full cache hit"))
        with patch.object(va, "parse_agent_response", return_value=_resp()):
            second = api.extract_tables_batch(specs)
        assert [r.table_label for r in second] == [r.table_label for r in first]

    def test_mixed_hit_miss_only_submits_misses_in_order(self, tmp_path, pdf):
        from zotpilot.feature_extraction import vision_api as va
        cache = VisionResultCache(tmp_path / "vc")
        api = _api(cache)
        s0 = _spec(pdf, table_id="d__t0", page=1)
        s1 = _spec(pdf, table_id="d__t1", page=2)
        with patch.object(va, "parse_agent_response", return_value=_resp()):
            api.extract_tables_batch([s0, s1])  # populate

        s2 = _spec(pdf, table_id="d__t2", page=3)  # new → miss
        api._build_request.reset_mock()
        api._submit_and_poll = MagicMock(side_effect=lambda reqs: {r["custom_id"]: "RAW" for r in reqs})
        with patch.object(va, "parse_agent_response", return_value=_resp(label="Fresh")):
            out = api.extract_tables_batch([s0, s1, s2])

        # only s2 submitted
        assert api._build_request.call_count == 1
        submitted = api._submit_and_poll.call_args[0][0]
        assert [r["custom_id"] for r in submitted] == ["d__t2__transcriber"]
        # order preserved: hits for s0/s1, fresh for s2
        assert len(out) == 3
        assert out[2].table_label == "Fresh"

    def test_failed_parse_is_not_cached(self, tmp_path, pdf):
        from zotpilot.feature_extraction import vision_api as va
        cache = VisionResultCache(tmp_path / "vc")
        api = _api(cache)
        spec = _spec(pdf, table_id="d__t0")
        # _submit_and_poll returns nothing → built response has parse_success=False
        api._submit_and_poll = MagicMock(return_value={})
        with patch.object(va, "parse_agent_response", return_value=_resp(success=False)):
            api.extract_tables_batch([spec])
        # nothing cached → key is a miss on the next run
        assert cache.get(cache.content_key(spec, f"{api._model}|{api._prompt_mode}")) is None


def _dashscope_api(cache):
    from zotpilot.feature_extraction.dashscope_vision_api import DashScopeVisionAPI
    api = DashScopeVisionAPI(api_key="fake-key", result_cache=cache)
    api._prepare_table = MagicMock(return_value=[])
    api._extract_one = MagicMock(side_effect=lambda spec, images: _resp())
    return api


class TestDashScopeCaching:
    def test_second_run_hits_cache_no_request(self, tmp_path, pdf):
        cache = VisionResultCache(tmp_path / "vc")
        api = _dashscope_api(cache)
        specs = [_spec(pdf, table_id="d__t0", page=1), _spec(pdf, table_id="d__t1", page=2)]

        first = api.extract_tables_batch(specs)
        assert api._extract_one.call_count == 2
        assert all(r.parse_success for r in first)

        api._extract_one = MagicMock(side_effect=AssertionError("must not call API on full cache hit"))
        second = api.extract_tables_batch(specs)
        assert [r.table_label for r in second] == [r.table_label for r in first]

    def test_mixed_hit_miss_preserves_order(self, tmp_path, pdf):
        cache = VisionResultCache(tmp_path / "vc")
        api = _dashscope_api(cache)
        s0 = _spec(pdf, table_id="d__t0", page=1)
        s1 = _spec(pdf, table_id="d__t1", page=2)
        api.extract_tables_batch([s0, s1])  # populate

        s2 = _spec(pdf, table_id="d__t2", page=3)  # miss
        api._extract_one = MagicMock(side_effect=lambda spec, images: _resp(label="Fresh"))
        out = api.extract_tables_batch([s0, s1, s2])
        assert api._extract_one.call_count == 1  # only the miss
        assert len(out) == 3 and out[2].table_label == "Fresh"

    def test_failed_parse_not_cached(self, tmp_path, pdf):
        cache = VisionResultCache(tmp_path / "vc")
        api = _dashscope_api(cache)
        spec = _spec(pdf, table_id="d__t0")
        api._extract_one = MagicMock(side_effect=lambda spec, images: _resp(success=False))
        api.extract_tables_batch([spec])
        assert cache.get(cache.content_key(spec, f"{api._model}|{api._prompt_mode}")) is None
